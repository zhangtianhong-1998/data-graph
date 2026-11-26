"""Discover same-batch overlap at definition granularity; only verified rules can run.

Physical and catalog keys are indexed locally. Values, node IDs, author-written
text and support payloads never enter the pattern cards sent to a reviewer.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import EntityFusionRule, FusionPolicyConfig, MergePolicyConfig
from lightgraph.source_values import identity_cell

from .hierarchy_observations import observation_scope
from .identity_authority import _shared_proofs, source_address
from .observed_identity import _dictionary_binding, _node_evidence
from .source_namespaces import source_namespace


MAX_MEMBERS = 24
MAX_DEFINITIONS = 4096
MAX_RECORDS = 250_000
MAX_SUPPORTS = 1_000_000
MAX_ALIAS_OBSERVATIONS = 100_000
MAX_ALIAS_FANOUT = 6
MAX_ALIAS_COMPARISONS = 120_000
MAX_ALIAS_PATTERNS = 12


@dataclass
class OverlapAnalysis:
    patterns: list
    rules_by_pattern: dict
    summary: dict


@dataclass
class _Family:
    owner: str
    name: str
    definition: dict
    contract_fingerprint: str = ''
    by_source: dict = field(default_factory=lambda: defaultdict(set))
    source_registries: dict = field(default_factory=dict)
    by_catalog: dict = field(default_factory=lambda: defaultdict(set))
    observations: dict = field(default_factory=dict)
    roles: set = field(default_factory=set)
    member_roles: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(set)))
    role_descriptors: set = field(default_factory=set)
    property_names: set = field(default_factory=set)
    problems: set = field(default_factory=set)
    dictionary_members: set = field(default_factory=set)
    foreign_roles: set = field(default_factory=set)

    @property
    def names(self):
        return tuple(self.definition.get('key_names') or ())

    def descriptor(self):
        roles = [{'relation_type': relation, 'end': end, 'key_columns': list(columns),
                  'opposite_key_columns': list(peer)} for relation, end, columns, peer in sorted(self.role_descriptors)[:16]]
        return {'contributor_id': self.owner, 'type': self.name, 'key_names': list(self.names),
                'property_names': sorted(self.property_names)[:32], 'roles': roles}


def _columns(value):
    if (not isinstance(value, list) or not 0 < len(value) <= 32
            or not all(isinstance(col, str) and col.strip() for col in value)
            or len(set(value)) != len(value)):
        return ()
    return tuple(value)


def _read_node(family, node):
    names = family.names
    identity = node.get('identity') or {}
    supports = node.get('supports') or []
    if (not names or set(identity) != set(names) or set(node.get('identity_types') or {}) != set(names)
            or not supports or any(identity[n] is None or identity[n] == '' for n in names)):
        family.problems.add('incomplete_typed_identity')
        return
    cells = tuple(identity_cell(node, name) for name in names)
    if any(tag != node['identity_types'][name] for name, (tag, _) in zip(names, cells)):
        family.problems.add('incomplete_typed_identity')
        return
    key = digest(cells)
    if key in family.observations:
        family.problems.add('multiple_members_in_one_projection')
    labels = set()
    distinct_name_column = True
    for support in supports:
        prov = support.get('provenance') or {}
        family.property_names.update((prov.get('property_columns') or {}).keys())
        address = source_address(prov.get('source'))
        cols = _columns(prov.get('key_columns'))
        if not address or len(cols) != len(names):
            family.problems.add('incomplete_source_binding')
            continue
        family.by_source[(*address, cols)].add(key)
        binding_key = (*address, cols)
        marker = prov.get('global_identity_authority') or {}
        registries = set()
        if (marker.get('registry_key_claims_v1') and family.definition.get('identity_scope') == 'global'
                and family.definition.get('identity_domain') == node.get('identity_domain')):
            registries = {v for k, v in _shared_proofs(marker, node['identity_domain'], address, cols) if k == 'registry'}
        if binding_key in family.source_registries:
            family.source_registries[binding_key].intersection_update(registries)
        else:
            family.source_registries[binding_key] = registries
        label = support.get('observed_label') or support.get('label')
        if not isinstance(label, str) or not label.strip() or not prov.get('label_column'):
            family.problems.add('missing_observed_name')
        else:
            labels.add(label)
            if prov['label_column'] in cols or label in (str(v) for v in identity.values()):
                distinct_name_column = False
        if any(support.get(k) for k in ('identity_origins', 'identity_decision_refs', 'inherited_from',
                                        'identity_basis', 'lookup_matches', 'original_key_values')):
            family.problems.add('previous_identity_decision_or_inheritance')
        if support.get('contributor_id', family.owner) != family.owner:
            family.problems.add('unverified_contributor')
        if any(o.get('contributor_id', family.owner) != family.owner for o in support.get('execution_origins', [])):
            family.problems.add('unverified_contributor')
        row = support.get('row_context') or {}
        for col, cell in zip(cols, cells):
            if col in (row.get('values') or {}) and (
                    (row.get('value_types') or {}).get(col) != cell[0]
                    or identity_cell({'identity': row['values'], 'identity_types': row['value_types']}, col) != cell):
                family.problems.add('source_cell_conflict')
        if prov.get('label_column') in (row.get('values') or {}) and row['values'][prov['label_column']] != label:
            family.problems.add('source_cell_conflict')
    if len(labels) != 1 or node.get('label_conflict') or set(node.get('label_variants', [])) - labels:
        family.problems.add('conflicting_labels')
    binding = _dictionary_binding(node, family.definition)
    if binding:
        target, roles, member = binding
        family.by_catalog[target].add(key)
        family.foreign_roles.update(roles)
        if member:
            family.dictionary_members.add(key)
    physical_obs = _node_evidence(node, family.definition)
    catalog_obs = _node_evidence(node, family.definition, dictionary=binding[0]) if binding else None
    if physical_obs is None:
        family.problems.add('unverified_source_or_scope')
    family.observations[key] = (digest(sorted(labels)), digest(physical_obs),
                                digest(catalog_obs) if catalog_obs else None, distinct_name_column and len(labels) == 1,
                                digest(physical_obs[2]) if physical_obs else None)
    return key


def _read_edges(owner, graph, families, identities, relations):
    definitions = {d['type']: d for d in graph.get('definitions', {}).get('relations', [])}
    endpoints = {n['id']: n['type'] for n in graph.get('nodes', [])}
    for edge in graph.get('edges', []):
        relation = definitions.get(edge['type'])
        family_ends = [(end, f) for end in ('source', 'target')
                       if (f := families.get((owner, endpoints.get(edge[end]))))]
        verified_ends = relation and all(endpoints.get(edge[end]) == relation.get(end + '_type')
                                         and edge[end] in identities for end in ('source', 'target'))
        if not verified_ends or not edge.get('supports'):
            for _, f in family_ends:
                f.problems.add('unverified_incident_relation')
            continue
        pair = (identities[edge['source']], identities[edge['target']])
        for support in edge['supports']:
            prov = support.get('provenance') or {}
            address = source_address(prov.get('source'))
            left, right = (_columns(prov.get(end + '_key_columns')) for end in ('source', 'target'))
            if (not address or not left or not right or not support.get('scope_ref') or support.get('inherited_from')
                    or support.get('lookup_matches') or prov.get('source_lookup') or prov.get('target_lookup')):
                for _, f in family_ends:
                    f.problems.add('unverified_incident_relation')
                continue
            relations[(*address, left, right)][owner, edge['type']].add(pair)
            if edge['source'] == edge['target']:
                continue
            semantics = relation.get('original_type') or relation['type']
            witness = support.get('source_row_witness') or {}
            role = digest(semantics, address, left, right, prov.get('predicate'), prov.get('constant_properties'),
                          observation_scope(support), witness.get('snapshot'))
            for end, family in family_ends:
                family.roles.add((end, role))
                neighbor = pair[1] if end == 'source' else pair[0]
                family.member_roles[identities[edge[end]]][end, semantics, role].add(neighbor)
                columns, peer = (left, right) if end == 'source' else (right, left)
                family.role_descriptors.add((edge['type'], end, columns, peer))

def _public_source(binding):
    source, schema, table, columns = binding
    return {'source_id': source, 'schema': schema, 'table': table, 'key_columns': list(columns)}


def _blocked_types(policy):
    rules = (policy.fusion_policy or FusionPolicyConfig()).entity_rules
    return ({r.entity_type for r in rules} | {r.canonical_type for r in rules} | set(policy.shared_entity_types)
            | {name for group in policy.shared_identity_domains.values() for name in group})


def _policy(value):
    return value if isinstance(value, MergePolicyConfig) else MergePolicyConfig.model_validate(value or {})


def _overlapping_members(members, projections):
    # Walk the inverted key index once. Disjoint key populations at the same
    # table address cannot supply evidence for, or veto, each other's rule.
    by_key = defaultdict(list)
    for index, cells in enumerate(projections):
        for cell in cells:
            by_key[cell].append(index)
    visited = set()
    for start in range(len(members)):
        if start in visited:
            continue
        component, overlap, pending = [], set(), [start]
        shared_by = Counter()
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            component.append(index)
            for cell in projections[index]:
                peers = by_key.pop(cell, ())
                if len(peers) > 1:
                    overlap.add(cell)
                    pending.extend(peers)
                    if len(members) > 2:
                        shared_by[tuple(peers)] += 1
        if overlap:
            ordered = sorted(component)
            coverage = []
            if len(ordered) > 2:
                # Group identical posting lists so a heavily repeated combination
                # is expanded into pairs just once. A--B--C may have no A--C keys.
                counts = Counter()
                for peers, count in shared_by.items():
                    for pair in combinations(peers, 2):
                        counts[pair] += count
                coverage = [{'member_indexes': [i, j], 'overlapping_keys': counts[ordered[i], ordered[j]]}
                            for i, j in combinations(range(len(ordered)), 2)]
            yield [members[i] for i in ordered], overlap, coverage


def _group_alias_pairs(sources, pairs, inputs_by_owner):
    """Group an entire clique only if every pair has identical scoped witnesses."""
    adjacent = defaultdict(set)
    for i, j in pairs:
        adjacent[i].add(j)
        adjacent[j].add(i)
    groups = []
    while adjacent:
        pending = [next(iter(adjacent))]
        component = set()
        component_pairs = []
        while pending:
            i = pending.pop()
            if i not in component:
                component.add(i)
                neighbors = adjacent.pop(i)
                pending.extend(neighbors)
                component_pairs.extend((i, j) for j in neighbors if i < j)
        members = sorted(component)
        first, second = members[:2]
        reference = pairs.get((first, second))
        owners = {sources[i][0].owner for i in members}
        compatible = (2 < len(members) <= MAX_ALIAS_FANOUT and len(owners) == len(members)
                      and len({sources[i][0].definition.get('identity_scope') for i in members}) == 1
                      and reference and all(scope for _, scope in reference.values())
                      and not _authority_issues(owners, inputs_by_owner)
                      and all(pairs.get(pair) == reference for pair in combinations(members, 2)))
        if compatible:
            groups.append((members, len(reference)))
        else:
            groups.extend(([i, j], len(pairs[i, j])) for i, j in component_pairs)
    return sorted(groups, key=lambda item: (-item[1], item[0]))


def _cross_table_name_candidates(families, family_rows, read_counts, inputs_by_owner):
    """Bounded inverted index for differently named complete keys. Never issues a rule."""
    sources = [(f, next(iter(f.by_source))) for f in families.values()
               if len(f.by_source) == 1 and not f.problems]
    sources.sort(key=lambda entry: (entry[0].owner, entry[0].name))
    stats = {'status': 'complete', 'high_frequency_keys_suppressed': 0, 'observations_read': 0,
             'comparisons': 0, 'omitted_patterns': 0}
    from .overlap_index import possibly_overlapping
    widths = defaultdict(list)
    for i, (family, _) in enumerate(sources):
        widths[len(family.names)].append((i, family_rows[family.owner, family.name]))
    selected = set()
    for group in widths.values():
        possible = possibly_overlapping(group, 'alias_filter')
        if (len({sources[i][0].names for i in possible}) < 2
                or len({sources[i][1][:3] for i in possible}) < 2):
            continue
        # Raw-key occurrence counts suppress common weak codes even if the
        # other sources disagreed on their labels. Read those postings only
        # after a key-and-name overlap is possible between different schemas.
        key_bits = 0
        for i, row in group:
            if i in possible:
                key_bits |= int(row['key_filter'], 16)
        selected.update(i for i, row in group if key_bits & int(row['key_filter'], 16))
    if sum(family_rows[sources[i][0].owner, sources[i][0].name]['key_count'] for i in selected) > MAX_ALIAS_OBSERVATIONS:
        stats['status'] = 'observation_budget_exceeded'
        return [], stats
    postings = defaultdict(list)
    for i, (family, _) in enumerate(sources):
        if i not in selected:
            continue
        read_counts['alias_sections'] += 1
        aliases = family_rows[family.owner, family.name]['aliases']
        stats['observations_read'] += len(aliases)
        for cell, label, scope in aliases:
            postings_for_key = postings[len(family.names), cell]
            if len(postings_for_key) <= MAX_ALIAS_FANOUT:
                postings_for_key.append((i, label, scope))

    pairs = defaultdict(dict)
    for (_, cell), occurrences in postings.items():
        if len(occurrences) > MAX_ALIAS_FANOUT:
            stats['high_frequency_keys_suppressed'] += 1
            continue
        for (i, left_label, left_scope), (j, right_label, right_scope) in combinations(occurrences, 2):
            if stats['comparisons'] >= MAX_ALIAS_COMPARISONS:
                stats['status'] = 'comparison_budget_exceeded'
                return [], stats
            stats['comparisons'] += 1
            left, left_source = sources[i]
            right, right_source = sources[j]
            if left_label != right_label or left.names == right.names or left_source[:3] == right_source[:3]:
                continue
            pairs[i, j][cell] = (left_label, left_scope if left_scope == right_scope else None)
    qualified = {pair: witnesses for pair, witnesses in pairs.items()
                 if len(witnesses) >= 2 and len({label for label, _ in witnesses.values()}) >= 2}
    choices = _group_alias_pairs(sources, qualified, inputs_by_owner)
    stats['omitted_patterns'] = max(0, len(choices) - MAX_ALIAS_PATTERNS)
    return [([sources[i] for i in members], count) for members, count in choices[:MAX_ALIAS_PATTERNS]], stats


def _authority_issues(owners, inputs_by_owner):
    reasons = set()
    participants = [inputs_by_owner[owner] for owner in sorted(owners)]
    if not source_namespace(participants):
        reasons.add('unverified_source_namespace')
    fingerprints = {c['graph'].get('access_fingerprint') for c in participants}
    if len(fingerprints) != 1 or not all(fingerprints):
        reasons.add('unverified_access_fingerprint')
    return reasons


def _check_group(members, all_owners, basis, key, keys, blocked, inputs_by_owner):
    owners = {f.owner for f in members}
    reasons = _authority_issues(owners, inputs_by_owner)
    reasons.update(problem for f in members for problem in f.problems)
    types = {f.name for f in members}
    if len(owners) != len(members):
        reasons.add('competing_roles_in_one_unit')
    if any(all_owners[f.name] != {f.owner} for f in members) or len(types) != len(members):
        reasons.add('type_used_by_other_projection')
    if types & blocked:
        reasons.add('existing_identity_policy')
    if basis == 'same_logical_keys_across_sources':
        reasons.add('cross_source_identity_unverified')
    if any(len(f.by_source) != 1 for f in members):
        reasons.add('type_has_other_source_bindings')
    if basis == 'observed_catalog_key' and any(f.observations.keys() != keys for f in members):
        reasons.add('partial_dictionary_coverage')
    if basis == 'same_physical_key' and not any(all(cell in f.observations for f in members) for cell in keys):
        reasons.add('no_witness_shared_by_all_projections')
    # Transferring a whole type needs equal identity contracts, including scope
    # and its dimensions. Preserve all additional, possibly future contract fields.
    contracts = set()
    for family in members:
        definition = family.definition
        contracts.add(family.contract_fingerprint)
        if (definition.get('identity_origin', 'row') != 'row' or definition.get('identity_scope') != 'global'
                or definition.get('identity_context_keys') or definition.get('definition_variant')
                or definition.get('original_type') or len(family.name) > 100 or len(family.names) > 32):
            reasons.add('unverified_identity_contract')
    if len(contracts) != 1:
        reasons.add('different_identity_contracts')
    if basis == 'observed_catalog_key':
        foreign_roles = set().union(*(f.foreign_roles for f in members))
        dictionary_keys = set().union(*(f.dictionary_members for f in members))
        if (len(foreign_roles) != 1 or not keys <= dictionary_keys
                or any(f.by_catalog.get(key, set()) != keys for f in members)):
            reasons.add('missing_dictionary_member_or_role')
    elif any(f.roles != members[0].roles for f in members):
        reasons.add('unverified_relation_role')
    elif not members[0].roles and not all(
            f.by_catalog.get(key) == f.observations.keys() == f.dictionary_members for f in members):
        reasons.add('unverified_relation_role')
    if not reasons:
        evidence_slot = 2 if basis == 'observed_catalog_key' else 1
        witnesses, observed_roles, neighbors = {}, {}, {}
        for family in members:
            for cell, observation in family.observations.items():
                if cell not in keys:
                    continue
                signature = (observation[0], observation[evidence_slot])
                around = family.member_roles.get(cell, {})
                for role, peers in around.items():
                    if neighbors.setdefault((cell, role), peers) != peers:
                        reasons.add('incompatible_member_neighbors')
                roles = around.keys()
                if basis == 'observed_catalog_key':
                    roles = {(end, semantics) for end, semantics, _ in roles}
                first = witnesses.setdefault(cell, signature)
                if signature[0] != first[0]:
                    reasons.add('conflicting_labels')
                if roles or basis != 'observed_catalog_key':
                    if observed_roles.setdefault(cell, roles) != roles:
                        reasons.add('incompatible_member_relation_roles')
                if not signature[1] or first[1] != signature[1]:
                    reasons.add('different_source_or_scope')
    return sorted(reasons)


def _rules(members, basis, binding):
    # A physical address bounds an otherwise unlabelled domain to this project
    # batch. Existing domain names are preserved verbatim.
    first = min(members, key=lambda f: (f.name, f.owner))
    domain = first.definition.get('identity_domain') or 'batch.' + digest(basis, binding, first.name)[:32]
    if len(domain) > 100:
        return []
    result = []
    for family in members:
        physical, = family.by_source
        source = _public_source(physical)
        source['key_columns'] = dict(zip(family.names, physical[-1], strict=True))
        result.append(EntityFusionRule(entity_type=family.name, canonical_type=first.name,
                                      identity_domain=domain, key_mapping=dict(zip(first.names, family.names, strict=True)),
                                      source_bindings=[source]))
    return result


def analyze_overlap_patterns(contributors, policy=None):
    """Explicit in-memory analysis for callers that already own the original unit graphs."""
    return analyze_indexed_overlap_patterns(
        [{**c, 'overlap_index': build_overlap_index(c)} for c in contributors], policy)


def build_overlap_index(contributor):
    """Capture the committed unit's structural evidence once, before the merge stage."""
    from .overlap_index import build_index
    return build_index(contributor)


def analyze_indexed_overlap_patterns(contributors, policy=None):
    """Route by the checkpoint directories; never read contributor graphs here."""
    from .overlap_index import family_header, load_family_evidence, possibly_overlapping, relation_binding
    from .source_namespaces import BINDING

    policy = _policy(policy)
    patterns, rules = [], {}
    counts = {'family_sections': 0, 'relation_sections': 0, 'alias_sections': 0}
    summary = {'reported_patterns': 0, 'eligible_patterns': 0, 'omitted_patterns': 0, 'truncated': False,
               'index_format': 1, 'index_directories': {'families': 0, 'relations': 0}, 'index_sections_read': counts}
    result = OverlapAnalysis(patterns, rules, summary)
    if (len(contributors) < 2 or len({c['contributor_id'] for c in contributors}) != len(contributors)
            or any(c.get('kind', 'unit') != 'unit' for c in contributors)):
        summary['reason'] = 'requires_independent_unit_outputs'
        return result
    missing, incompatible = [], []
    for c in contributors:
        index = c.get('overlap_index')
        owner = c['contributor_id']
        if index is None:
            missing.append(owner)
        elif (not isinstance(index, dict) or index.get('format_version') != 1 or index.get('contributor_id') != owner
              or index.get('status') not in ('complete', 'analysis_budget_exceeded')):
            incompatible.append(owner)
    if missing or incompatible:
        summary.update(reason='overlap_index_missing' if missing else 'overlap_index_incompatible',
                       missing_index_contributors=sorted(missing), incompatible_index_contributors=sorted(incompatible))
        return result
    indexes = [c['overlap_index'] for c in contributors]
    if any(i['extends_base_graph'] for i in indexes):
        summary['reason'] = 'requires_independent_unit_outputs'
        return result
    if (any(i['status'] != 'complete' for i in indexes)
            or sum(i['counts']['definitions'] for i in indexes) > MAX_DEFINITIONS):
        summary.update(reason='analysis_budget_exceeded', truncated=True)
        return result
    families, family_rows, all_owners = {}, {}, defaultdict(set)
    relations = defaultdict(dict)
    inputs_by_owner = {}
    for c in sorted(contributors, key=lambda item: item['contributor_id']):
        owner, index = c['contributor_id'], c['overlap_index']
        inputs_by_owner[owner] = {'contributor_id': owner, 'source_namespace': c.get('source_namespace'),
                                  'graph': {'access_fingerprint': index['access_fingerprint'],
                                            BINDING: index['namespace_binding']}}
        summary['index_directories']['families'] += len(index['families'])
        summary['index_directories']['relations'] += len(index['relations'])
        for name in index['type_names']:
            all_owners[name].add(owner)
        for row in index['families']:
            family = family_header(owner, row)
            families[owner, family.name] = family
            family_rows[owner, family.name] = row
        for row in index['relations']:
            relations[relation_binding(row['binding'])][owner, row['type']] = row
    from .overlap_binding_evidence import BindingComparisons
    comparisons = BindingComparisons(family_rows, counts, MAX_SUPPORTS)
    loaded = set()
    peaks = {'member_keys_or_endpoint_pairs': 0, 'neighbor_witnesses': 0, 'skipped_groups': 0}
    summary['index_evidence_budget'] = peaks

    def reserve(records, witnesses=0):
        if (peaks['member_keys_or_endpoint_pairs'] + records > MAX_RECORDS
                or peaks['neighbor_witnesses'] + witnesses > MAX_SUPPORTS):
            peaks['skipped_groups'] += 1
            summary['omitted_patterns'] += 1
            return False
        peaks['member_keys_or_endpoint_pairs'] += records
        peaks['neighbor_witnesses'] += witnesses
        return True

    buckets = defaultdict(list)
    for f in families.values():
        for key in f.by_source:
            buckets['same_physical_key', key].append(f)
        for key in f.by_catalog:
            buckets['observed_catalog_key', key].append(f)
        if family_rows[f.owner, f.name]['key_count']:
            buckets['same_logical_keys_across_sources', f.names].append(f)

    def record(kind, basis, members, sources, overlap, reasons, candidates=(), witness_binding=None, pair_coverage=None,
               overlapping_names=None, evidenced_families=None):
        if len(members) > MAX_MEMBERS or len(sources) > MAX_MEMBERS:
            summary['omitted_patterns'] += 1
            return
        pid = 'pattern_' + digest(kind, basis, members, sources, witness_binding)[:24]
        # Keep archived pattern IDs stable. A shared column spelling does not
        # tell the reviewer which table actually supplied it to each branch.
        binding_counts = None
        if kind == 'entity':
            bindings = [(s['source_id'], s['schema'], s['table'], tuple(s['key_columns'])) for s in sources]
            for m in members:
                owner = m['contributor_id']
                family = families[owner, m['type']]
                m['source_indexes'] = [i for i, binding in enumerate(bindings) if binding in family.by_source]
                for role in m['roles']:
                    own, peer = tuple(role['key_columns']), tuple(role['opposite_key_columns'])
                    left, right = (own, peer) if role['end'] == 'source' else (peer, own)
                    mslot = (owner, role['relation_type'])
                    role['source_indexes'] = [
                        i for i in m['source_indexes']
                        if bindings[i][-1] == own and mslot in relations.get((*bindings[i][:3], left, right), {})
                        and (basis != 'same_physical_key' or sources[i] == witness_binding)]
            if evidenced_families:
                options = [sorted({i for r in m['roles'] for i in r['source_indexes']}) for m in members]
                if sum(bool(o) for o in options) >= 2:
                    binding_counts = comparisons.compare(evidenced_families, options, bindings)
        patterns.append({'pattern_id': pid, 'kind': kind, 'basis': basis, 'members': members, 'sources': sources,
                         **({'binding_comparisons': binding_counts} if binding_counts else {}),
                         **({'witness_binding': witness_binding} if witness_binding else {}),
                         **({'pair_coverage': pair_coverage} if pair_coverage else {}),
                         **({'overlapping_names': overlapping_names} if overlapping_names is not None else {}),
                         'overlapping_keys': overlap, 'reasons': reasons,
                         'eligible_actions': ['apply', 'retain'] if candidates else ['retain']})
        if candidates:
            rules[pid] = candidates

    blocked_types = _blocked_types(policy)
    for (basis, key), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        if len(members) > MAX_MEMBERS or any(len(f.by_source) > MAX_MEMBERS for f in members):
            summary['omitted_patterns'] += 1
            continue
        members = sorted(members, key=lambda f: (f.owner, f.name))
        if basis == 'same_logical_keys_across_sources' and len({b[:3] for f in members for b in f.by_source}) < 2:
            continue
        entries = [((f.owner, f.name), family_rows[f.owner, f.name]) for f in members]
        candidates = possibly_overlapping(entries, 'key_filter')
        if len(candidates) < 2:
            continue
        members = [f for f in members if (f.owner, f.name) in candidates]
        pending = [family_rows[slot] for slot in candidates - loaded]
        if not reserve(sum(row['key_count'] for row in pending), sum(row['witness_count'] for row in pending)):
            continue
        for family in members:
            slot = family.owner, family.name
            if slot not in loaded:
                load_family_evidence(family, family_rows[slot]['evidence'])
                loaded.add(slot)
                counts['family_sections'] += 1
        if basis == 'same_logical_keys_across_sources':
            projections = [f.observations.keys() for f in members]
        elif basis == 'same_physical_key':
            projections = [f.by_source[key] for f in members]
        else:
            projections = [f.by_catalog[key] for f in members]
        for group, overlap, coverage in _overlapping_members(members, projections):
            bindings = sorted({b for f in group for b in f.by_source})
            if basis != 'same_physical_key' and len({b[:3] for b in bindings}) < 2:
                continue
            reasons = _check_group(group, all_owners, basis, key, overlap, blocked_types, inputs_by_owner)
            candidates = [] if reasons else _rules(group, basis, key)
            if not candidates and not reasons:
                reasons.append('rule_representation_limit')
            record('entity', basis, [f.descriptor() for f in group], [_public_source(b) for b in bindings],
                   len(overlap), reasons, candidates, pair_coverage=coverage, evidenced_families=group,
                   witness_binding=None if basis == 'same_logical_keys_across_sources' else _public_source(key))
    aliases, alias_stats = _cross_table_name_candidates(families, family_rows, counts, inputs_by_owner)
    summary['cross_table_key_name_search'] = alias_stats
    summary['omitted_patterns'] += alias_stats['omitted_patterns']
    for peers, count in aliases:
        reasons = _authority_issues({f.owner for f, _ in peers}, inputs_by_owner)
        reasons.add('cross_source_identity_unverified')
        pair_coverage = ([{'member_indexes': [i, j], 'overlapping_keys': count}
                          for i, j in combinations(range(len(peers)), 2)] if len(peers) > 2 else None)
        record('entity', 'same_typed_keys_and_names_across_sources', [f.descriptor() for f, _ in peers],
               [_public_source(binding) for _, binding in peers], count, sorted(reasons), pair_coverage=pair_coverage)
    for (src, schema, table, left, right), pairs in sorted(relations.items()):
        if len(pairs) > MAX_MEMBERS:
            summary['omitted_patterns'] += 1
        elif len(pairs) > 1:
            ordered = sorted(possibly_overlapping(pairs.items(), 'key_filter'))
            if len(ordered) < 2 or not reserve(sum(pairs[m]['pair_count'] for m in ordered)):
                continue
            projected = [{tuple(pair) for pair in pairs[m]['evidence']} for m in ordered]
            counts['relation_sections'] += len(ordered)
            for group, overlap, coverage in _overlapping_members(ordered, projected):
                members = [{'contributor_id': owner, 'type': name, 'key_names': [],
                            'property_names': [], 'roles': []} for owner, name in group]
                source = {'source_id': src, 'schema': schema, 'table': table, 'key_columns': list(left + right),
                          'source_key_columns': list(left), 'target_key_columns': list(right)}
                reasons = _authority_issues({owner for owner, _ in group}, inputs_by_owner)
                reasons.add('preserve_relation_observations')
                record('relation', 'same_directed_source_columns', members, [source], len(overlap), sorted(reasons),
                       pair_coverage=coverage)
    # Existing key, catalog and directed-relation searches take precedence for I/O.
    # A family pair already routed by a stronger pattern needs no second review card.
    from .overlap_name_candidates import different_key_name_candidates
    covered = {frozenset(pair) for p in patterns if p['kind'] == 'entity'
               for pair in combinations(((m['contributor_id'], m['type']) for m in p['members']), 2)}
    name_matches, name_stats = different_key_name_candidates(
        families, family_rows, covered, inputs_by_owner=inputs_by_owner, group_pairs=_group_alias_pairs,
        max_rows=min(MAX_ALIAS_OBSERVATIONS - alias_stats['observations_read'],
                     MAX_RECORDS - peaks['member_keys_or_endpoint_pairs']),
        max_comparisons=MAX_ALIAS_COMPARISONS - alias_stats['comparisons'], fanout=MAX_ALIAS_FANOUT,
        max_patterns=MAX_ALIAS_PATTERNS)
    summary['cross_table_different_key_name_search'] = name_stats
    counts['alias_sections'] += name_stats['alias_sections']
    summary['omitted_patterns'] += name_stats['omitted_patterns']
    for peers, name_count, key_count, coverage in name_matches:
        owners = {f.owner for f, _ in peers}
        reasons = _authority_issues(owners, inputs_by_owner)
        reasons.update(reason for f, _ in peers for reason in f.problems)
        if len(owners) != len(peers):
            reasons.add('competing_roles_in_one_unit')
        reasons.add('cross_code_correspondence_unverified')
        record('entity', 'same_observed_names_with_different_keys', [f.descriptor() for f, _ in peers],
               [_public_source(b) for _, b in peers], key_count, sorted(reasons), overlapping_names=name_count,
               pair_coverage=coverage)
    patterns.sort(key=lambda p: ('apply' not in p['eligible_actions'], p['pattern_id']))
    summary.update(reported_patterns=len(patterns), eligible_patterns=len(rules),
                   truncated=bool(summary['omitted_patterns']) or alias_stats['status'] != 'complete'
                   or name_stats['status'] != 'complete')
    return result


def apply_overlap_decisions(policy, analysis, selected_pattern_ids):
    """Apply only this analysis' verified, source-bound rule bundles atomically."""
    base = _policy(policy)
    if not selected_pattern_ids:
        return base
    selected = set(selected_pattern_ids)
    if len(selected) != len(selected_pattern_ids) or not selected <= analysis.rules_by_pattern.keys():
        raise LightGraphError('invalid_overlap_pattern_decision')
    config = (base.fusion_policy or FusionPolicyConfig()).model_dump(mode='json')
    blocked = _blocked_types(base)
    for pid in sorted(selected):
        bundle = analysis.rules_by_pattern[pid]
        types = {r.entity_type for r in bundle} | {r.canonical_type for r in bundle}
        if blocked & types or len(config['entity_rules']) + len(bundle) > 128:
            raise LightGraphError('overlap_pattern_policy_conflict')
        config['entity_rules'].extend(r.model_dump(mode='json') for r in bundle)
        blocked.update(types)
    return base.model_copy(update={'fusion_policy': FusionPolicyConfig.model_validate(config)})

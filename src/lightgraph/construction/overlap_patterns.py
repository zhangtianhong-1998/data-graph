"""Discover same-batch overlap at definition granularity; only verified rules can run.

Physical and catalog keys are indexed locally. Values, node IDs, author-written
text and support payloads never enter the pattern cards sent to a reviewer.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import EntityFusionRule, FusionPolicyConfig, MergePolicyConfig
from lightgraph.source_values import identity_cell

from .fusion import definition_contract
from .hierarchy_observations import observation_scope
from .identity_authority import source_address
from .observed_identity import _dictionary_binding, _node_evidence
from .source_namespaces import source_namespace


MAX_PATTERNS = 32
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
    by_source: dict = field(default_factory=lambda: defaultdict(set))
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
                                digest(catalog_obs) if catalog_obs else None, node['id'], distinct_name_column and len(labels) == 1)


def _read_edges(owner, graph, families, relations):
    definitions = {d['type']: d for d in graph.get('definitions', {}).get('relations', [])}
    endpoints = {n['id']: n['type'] for n in graph.get('nodes', [])}
    identities = {obs[3]: cell for (cid, _), f in families.items() if cid == owner
                  for cell, obs in f.observations.items()}
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
        if overlap:
            yield [members[i] for i in sorted(component)], overlap


def _cross_table_name_candidates(families):
    """Bounded inverted index for differently named complete keys. Never issues a rule."""
    sources = [(f, next(iter(f.by_source))) for f in families.values()
               if len(f.by_source) == 1 and not f.problems]
    sources.sort(key=lambda entry: (entry[0].owner, entry[0].name))
    stats = {'status': 'complete', 'high_frequency_keys_suppressed': 0, 'comparisons': 0, 'omitted_patterns': 0}
    if sum(len(f.observations) for f, _ in sources) > MAX_ALIAS_OBSERVATIONS:
        stats['status'] = 'observation_budget_exceeded'
        return [], stats
    postings = defaultdict(list)
    for i, (family, _) in enumerate(sources):
        for cell, observation in family.observations.items():
            if not observation[4]:  # A name derived from the key supplies no independent evidence.
                continue
            postings_for_key = postings[len(family.names), cell]
            if len(postings_for_key) <= MAX_ALIAS_FANOUT:
                postings_for_key.append((i, observation[0]))

    pairs = {}
    for occurrences in postings.values():
        if len(occurrences) > MAX_ALIAS_FANOUT:
            stats['high_frequency_keys_suppressed'] += 1
            continue
        for (i, left_label), (j, right_label) in combinations(occurrences, 2):
            if stats['comparisons'] >= MAX_ALIAS_COMPARISONS:
                stats['status'] = 'comparison_budget_exceeded'
                return [], stats
            stats['comparisons'] += 1
            left, left_source = sources[i]
            right, right_source = sources[j]
            if left_label != right_label or left.names == right.names or left_source[:3] == right_source[:3]:
                continue
            count, first_label, multiple_names = pairs.get((i, j), (0, left_label, False))
            pairs[i, j] = (count + 1, first_label, multiple_names or left_label != first_label)
    choices = sorted(((-count, i, j) for (i, j), (count, _, multiple_names) in pairs.items()
                      if count >= 2 and multiple_names))
    stats['omitted_patterns'] = max(0, len(choices) - MAX_ALIAS_PATTERNS)
    return [(sources[i], sources[j], -n) for n, i, j in choices[:MAX_ALIAS_PATTERNS]], stats


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
        contract = definition_contract(definition)
        for name in ('type', 'key_names'):
            contract.pop(name, None)
        contracts.add(canonical(contract))
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
    """Analyze the exact checked input batch. Exclude inherited or cumulative graphs."""
    policy = _policy(policy)
    patterns, rules = [], {}
    summary = {'reported_patterns': 0, 'eligible_patterns': 0, 'omitted_patterns': 0, 'truncated': False}
    result = OverlapAnalysis(patterns, rules, summary)
    if (len(contributors) < 2 or len({c['contributor_id'] for c in contributors}) != len(contributors)
            or any(c.get('kind', 'unit') != 'unit' or c['graph'].get('extends_base_graph') for c in contributors)):
        summary['reason'] = 'requires_independent_unit_outputs'
        return result
    graphs = [c['graph'] for c in contributors]
    records = sum(len(g.get(k, [])) for g in graphs for k in ('nodes', 'edges'))
    definitions = sum(len(g.get('definitions', {}).get(k, [])) for g in graphs for k in ('entities', 'relations'))
    if (records > MAX_RECORDS or definitions > MAX_DEFINITIONS
            or sum(len(r.get('supports', [])) for g in graphs for k in ('nodes', 'edges')
                   for r in g.get(k, [])) > MAX_SUPPORTS):
        summary.update(reason='analysis_budget_exceeded', truncated=True)
        return result

    families, all_owners = {}, defaultdict(set)
    relations = defaultdict(lambda: defaultdict(set))
    inputs_by_owner = {c['contributor_id']: c for c in contributors}
    for item in sorted(contributors, key=lambda c: c['contributor_id']):
        owner, graph = item['contributor_id'], item['graph']
        for definition in graph.get('definitions', {}).get('entities', []):
            name = definition['type']
            family = _Family(owner, name, definition)
            if (owner, name) in families or not _columns(definition.get('key_names')):
                family.problems.add('unverified_identity_contract')
            families[owner, name] = family
            all_owners[name].add(owner)
        for node in graph.get('nodes', []):
            family = families.get((owner, node['type']))
            if family:
                _read_node(family, node)
            else:
                all_owners[node['type']].add(owner)
        _read_edges(owner, graph, families, relations)

    buckets = defaultdict(list)
    for f in families.values():
        for key in f.by_source:
            buckets['same_physical_key', key].append(f)
        for key in f.by_catalog:
            buckets['observed_catalog_key', key].append(f)
        if f.observations:
            buckets['same_logical_keys_across_sources', f.names].append(f)

    def record(kind, basis, members, sources, overlap, reasons, candidates=(), witness_binding=None):
        if len(members) > MAX_MEMBERS or len(sources) > MAX_MEMBERS:
            summary['omitted_patterns'] += 1
            return
        pid = 'pattern_' + digest(kind, basis, members, sources, witness_binding)[:24]
        patterns.append({'pattern_id': pid, 'kind': kind, 'basis': basis, 'members': members, 'sources': sources,
                         **({'witness_binding': witness_binding} if witness_binding else {}),
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
        if basis == 'same_logical_keys_across_sources':
            projections = [f.observations.keys() for f in members]
        elif basis == 'same_physical_key':
            projections = [f.by_source[key] for f in members]
        else:
            projections = [f.by_catalog[key] for f in members]
        for group, overlap in _overlapping_members(members, projections):
            bindings = sorted({b for f in group for b in f.by_source})
            if basis != 'same_physical_key' and len({b[:3] for b in bindings}) < 2:
                continue
            reasons = _check_group(group, all_owners, basis, key, overlap, blocked_types, inputs_by_owner)
            candidates = [] if reasons else _rules(group, basis, key)
            if not candidates and not reasons:
                reasons.append('rule_representation_limit')
            record('entity', basis, [f.descriptor() for f in group], [_public_source(b) for b in bindings],
                   len(overlap), reasons, candidates,
                   witness_binding=None if basis == 'same_logical_keys_across_sources' else _public_source(key))
    aliases, alias_stats = _cross_table_name_candidates(families)
    summary['cross_table_key_name_search'] = alias_stats
    summary['omitted_patterns'] += alias_stats['omitted_patterns']
    for (left, a), (right, b), count in aliases:
        reasons = _authority_issues({left.owner, right.owner}, inputs_by_owner)
        reasons.add('cross_source_identity_unverified')
        record('entity', 'same_typed_keys_and_names_across_sources', [left.descriptor(), right.descriptor()],
               [_public_source(a), _public_source(b)], count, sorted(reasons))
    for (src, schema, table, left, right), pairs in sorted(relations.items()):
        if len(pairs) > MAX_MEMBERS:
            summary['omitted_patterns'] += 1
        elif len(pairs) > 1:
            ordered = sorted(pairs)
            for group, overlap in _overlapping_members(ordered, [pairs[m] for m in ordered]):
                members = [{'contributor_id': owner, 'type': name, 'key_names': [],
                            'property_names': [], 'roles': []} for owner, name in group]
                source = {'source_id': src, 'schema': schema, 'table': table, 'key_columns': list(left + right),
                          'source_key_columns': list(left), 'target_key_columns': list(right)}
                reasons = _authority_issues({owner for owner, _ in group}, inputs_by_owner)
                reasons.add('preserve_relation_observations')
                record('relation', 'same_directed_source_columns', members, [source], len(overlap), sorted(reasons))
    patterns.sort(key=lambda p: ('apply' not in p['eligible_actions'], p['pattern_id']))
    summary['omitted_patterns'] += max(0, len(patterns) - MAX_PATTERNS)
    for p in patterns[MAX_PATTERNS:]:
        rules.pop(p['pattern_id'], None)
    del patterns[MAX_PATTERNS:]
    summary.update(reported_patterns=len(patterns), eligible_patterns=len(rules),
                   truncated=bool(summary['omitted_patterns']) or alias_stats['status'] != 'complete')
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

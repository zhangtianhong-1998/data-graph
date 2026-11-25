"""Validate a merge reviewer's finite, source-bound alignment against frozen observations.

A selected table is a projection address, not a unique-key declaration. Repeated
codes, contradictory names, different roles and unverified cross-table identity
produce counts for the reviewer. Only concrete, corroborated members can move.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re

from lightgraph.contracts import canonical, digest
from lightgraph.runtime.config import ProjectionBinding, VerifiedProjectionRule
from lightgraph.source_values import identity_cell
from .contexts import business_context_identity
from .fusion import definition_contract
from .graph_storage import _graph_digest
from .hierarchy_observations import observation_scope
from .identity_authority import _global_authorities, source_address
from .observed_identity import _node_evidence, _unique_row_projection, contradictory_qualifiers
from .overlap_patterns import _blocked_types
from .projection_access import attach_checkpoint_grant, attach_runtime_grants, common_table_grant
from .source_namespaces import source_namespace


def _address(binding):
    return binding.source_id, binding.schema_name, binding.table, binding.key_columns


class _Inputs:
    """Index only participating checkpoints and reuse their digest across proposals."""
    def __init__(self, inputs):
        self.inputs = {c['contributor_id']: c for c in inputs}
        self.graph_digests = {}
        self.by_owner = {}

    def digest(self, owner):
        if owner not in self.graph_digests:
            self.graph_digests[owner] = _graph_digest(self.inputs[owner]['graph'])
        return self.graph_digests[owner]

    def index(self, owner):
        if owner not in self.by_owner:
            graph = self.inputs[owner]['graph']
            nodes = {node['id']: node for node in graph['nodes']}
            families, incident = defaultdict(list), defaultdict(list)
            for node in graph['nodes']:
                families[node['type']].append(node)
            for edge in graph['edges']:
                for end in ('source', 'target'):
                    incident[edge[end]].append((end, edge))
            self.by_owner[owner] = (nodes, families, incident,
                                    {d['type']: d for d in graph['definitions']['entities']},
                                    {d['type']: d for d in graph['definitions']['relations']})
        return self.by_owner[owner]


def _cells(node, names):
    if (not names or set(node.get('identity', {})) != set(names)
            or set(node.get('identity_types', {})) != set(names)):
        return None
    cells = tuple(identity_cell(node, key) for key in names)
    if any(kind != node['identity_types'][key] or value is None or value == ''
           for key, (kind, value) in zip(names, cells, strict=True)):
        return None
    return cells


def _own_support(support, owner):
    return (support.get('contributor_id', owner) == owner and not any(support.get(k) for k in
            ('inherited_from', 'identity_origins', 'identity_basis', 'identity_decision_refs', 'lookup_matches',
             'original_key_values')) and all(o.get('contributor_id', owner) == owner
             for o in support.get('execution_origins', ())))


def _witnesses(node, binding, index):
    nodes, _, incident, entities, relations = index
    witnessed = set()
    all_roles = set()
    local_roles = set()
    local_witnesses = set()
    known_applicability = True
    for end, edge in incident[node['id']]:
        if edge['source'] == edge['target']:
            continue
        opposite = 'target' if end == 'source' else 'source'
        peer = nodes.get(edge[opposite], {})
        relation = relations.get(edge['type'], {})
        if (relation.get(end + '_type') != node['type'] or relation.get(opposite + '_type') != peer.get('type')
                or not edge.get('supports')):
            return None
        peer_key = _cells(peer, entities.get(peer['type'], {}).get('key_names'))
        if not peer_key:
            return None
        for support in edge['supports']:
            p = support.get('provenance', {})
            own_cols = tuple(p.get(end + '_key_columns', ()))
            peer_cols = tuple(p.get(opposite + '_key_columns', ()))
            source = source_address(p.get('source'))
            if (not own_cols or len(peer_cols) != len(peer_key) or not source or not support.get('scope_ref')
                    or not _own_support(support, binding.contributor_id) or p.get('source_lookup') or p.get('target_lookup')
                    or (support.get('business_context') or {}).get('conflicts')
                    or (support.get('business_context') or {}).get('missing_dimensions')):
                return None
            known_applicability = known_applicability and business_context_identity(support)['status'] != 'unknown'
            meaning = definition_contract(relation)
            for key in ('source_type', 'target_type'):
                meaning.pop(key, None)
            facts = digest(meaning, end, peer_cols, peer_key, observation_scope(support), p.get('predicate'),
                           p.get('constant_properties'), support.get('properties'), support.get('row_context'),
                           support.get('source_row_witness'))
            evidence = digest(source, own_cols, facts)
            # A shared, independently bound document section may establish the
            # domain across these tables. Normalize only the named key slot;
            # other axes, opposite columns, meanings and neighbors stay binding.
            local = facts if (*source, own_cols) == _address(binding) else evidence
            all_roles.add(evidence)
            local_roles.add(local)
            if (edge['type'] == binding.relation_type and end == binding.end
                    and (*source, own_cols) == _address(binding) and peer_cols == binding.opposite_key_columns):
                witnessed.add(evidence)
                local_witnesses.add(local)
    return ((witnessed, all_roles), (local_witnesses, local_roles) if known_applicability else None) if witnessed else None


def _upstream_context(node, index):
    """Keep the complete observed ancestor graph of a declared hierarchy in the comparison."""
    nodes, _, incident, entities, relations = index
    pending, seen, facts, items = [node], set(), set(), []
    while pending:
        child = pending.pop()
        if child['id'] in seen:
            continue
        seen.add(child['id'])
        if len(seen) > 512:
            return None, []
        for end, edge in incident[child['id']]:
            rel = relations.get(edge['type'], {})
            orientation = rel.get('hierarchy_orientation')
            if ((orientation, end) not in (('parent_to_child', 'target'), ('child_to_parent', 'source'))
                    or edge['source'] == edge['target']):
                continue
            parent = nodes[edge['source' if end == 'target' else 'target']]
            anchors = []
            for member in (child, parent):
                keys = _cells(member, entities.get(member['type'], {}).get('key_names'))
                names = {s.get('observed_label') or s.get('label') for s in member.get('supports', ())}
                if not keys or len(names) != 1 or not all(isinstance(n, str) and n.strip() for n in names):
                    return None, []
                anchors.append((keys, names.pop()))
            for support in edge.get('supports', ()):
                p = support.get('provenance') or {}
                if not source_address(p.get('source')) or not support.get('scope_ref'):
                    return None, []
                facts.add(digest(anchors, orientation, rel.get('original_type') or edge['type'],
                                 source_address(p['source']), p.get('source_key_columns'), p.get('target_key_columns'),
                                 observation_scope(support), p.get('predicate'), p.get('constant_properties'),
                                 support.get('row_context'), support.get('source_row_witness')))
            if not edge.get('supports') or len(facts) > 1024:
                return None, []
            items.extend((edge, parent))
            pending.append(parent)
    return frozenset(facts), items


@dataclass
class _Observation:
    node: dict
    label: str | None = None
    reason: str | None = None
    contract: str | None = None
    boundaries: tuple | None = None
    attributes: dict = field(default_factory=dict)
    roles: tuple | None = None
    sections: set = field(default_factory=set)
    ancestors: frozenset | None = None
    ancestor_items: list = field(default_factory=list)


def _conditions(support):
    provenance = support['provenance']
    protected = {}
    properties = support.get('properties') or {}
    for field in ('identity_qualifiers', 'hierarchy_position_roles'):
        for prop, role in (provenance.get(field) or {}).items():
            protected[prop] = [role, properties.get(prop)]
    return canonical([provenance.get('business_context_mapping'), sorted(provenance.get('context_columns') or ()),
                      support.get('row_context'), support.get('source_row_witness'), protected])


def _observation(node, definition, binding, index, own):
    result = _Observation(node)
    labels = {s.get('observed_label') or s.get('label') for s in own}
    if (len(labels) != 1 or not all(isinstance(name, str) and name.strip() for name in labels)
            or node.get('label_conflict') or set(node.get('label_variants', ())) - labels):
        result.reason = 'missing_or_conflicting_names'
        return result
    result.label = labels.pop()
    if (len(own) != len(node['supports']) or not all(_own_support(s, binding.contributor_id) for s in own)
            or any(s['provenance'].get('label_column') in (None, '', *binding.key_columns) for s in own)
            or result.label in (str(value) for value in node['identity'].values())):
        result.reason = 'unverified_name_or_other_bindings'
        return result
    names = definition['key_names']
    if (len(names) != len(binding.key_columns) or node.get('identity_domain') != definition.get('identity_domain')
            or node.get('identity_origin', 'row') != 'row' or definition.get('identity_origin', 'row') != 'row'
            or any(business_context_identity(s)['status'] == 'unknown' for s in own)
            or not _node_evidence(node, definition)):
        result.reason = 'unverified_applicability'
        return result
    if set(definition.get('identity_context_keys') or ()) - set(names):
        result.reason = 'incompatible_identity_contract'
        return result
    contract = definition_contract(definition)
    for key in ('type', 'key_names'):
        contract.pop(key, None)
    contract['identity_context_keys'] = [binding.key_columns[names.index(key)]
                                         for key in definition.get('identity_context_keys') or ()]
    result.contract = canonical([contract, node.get('node_role')])
    physical = _unique_row_projection(node, definition, _address(binding))
    if not physical:
        result.reason = 'conflicting_or_unbound_columns'
        return result
    signature, result.attributes = physical
    result.boundaries = (signature[-1], canonical(node.get('identity_context')),
                         tuple(sorted({_conditions(s) for s in own})))
    if definition.get('identity_scope') == 'global':
        result.sections = {token for kind, token in _global_authorities(node, definition, {}, local_namespace=False)
                           if kind == 'document' and isinstance(token, str) and re.fullmatch(r'[a-f0-9]{64}', token)}
    result.roles = _witnesses(node, binding, index)
    if not result.roles:
        result.reason = 'unverified_relation_role'
        return result
    result.ancestors, result.ancestor_items = _upstream_context(node, index)
    if result.ancestors is None:
        result.reason = 'unverified_ancestor_context'
    return result


def _by_key(binding, data):
    index = data.index(binding.contributor_id)
    _, families, _, entities, _ = index
    by_key = defaultdict(list)
    definition = entities.get(binding.entity_type, {})
    for node in families[binding.entity_type]:
        own = []
        for support in node.get('supports', ()):
            p = support.get('provenance') or {}
            if (source_address(p.get('source')) == _address(binding)[:3]
                    and tuple(p.get('key_columns') or ()) == binding.key_columns):
                own.append(support)
        if own:
            cells = _cells(node, definition.get('key_names'))
            if cells is None:
                # A partially typed competitor may be another copy of a selected value.
                return None
            by_key[cells].append(_observation(node, definition, binding, index, own))
    return by_key


def _grant_covers(member, binding, data, grant):
    scope_columns, allowed_columns = grant
    nodes, _, incident, _, _ = data.index(binding.contributor_id)
    items = [member.node, *member.ancestor_items]
    for end, edge in incident[member.node['id']]:
        opposite = 'target' if end == 'source' else 'source'
        items.extend((edge, nodes[edge[opposite]]))
    address = _address(binding)[:3]
    for item in items:
        if not item.get('supports'):
            return False
        for support in item['supports']:
            p = support.get('provenance') or {}
            if (source_address(p.get('source')) != address or p.get('scope_columns') != list(scope_columns)
                    or not support.get('scope_ref') or not scope_columns and support['scope_ref'] != 'public'
                    or support.get('inherited_from') or support.get('lookup_matches')
                    or p.get('source_lookup') or p.get('target_lookup')):
                return False
            if allowed_columns is not None:
                # A historical projection without a complete read list cannot
                # establish a column-limited grant. Predicates may read more
                # than the returned columns, so leave filtered rows for review.
                columns = p.get('columns')
                if not isinstance(columns, list) or not columns or p.get('predicate'):
                    return False
                used = set(scope_columns)
                for field in ('key_columns', 'source_key_columns', 'target_key_columns', 'alias_columns', 'context_columns'):
                    used.update(p.get(field) or ())
                used.update((p.get('property_columns') or {}).values())
                used.update((p.get('business_context_mapping') or {}).get('columns', {}).values())
                used.update(val for field in ('label_column', 'description_column', 'english_name_column')
                            if (val := p.get(field)))
                used.update((support.get('row_context') or {}).get('values', {}))
                if not used <= set(columns) | set(scope_columns) or not (used | set(columns)) <= allowed_columns:
                    return False
    return True


def _blocked_reason(members, physical):
    if not physical and not set.intersection(*(m.sections for m in members)):
        return 'cross_table_identity_unverified'
    if reason := next((m.reason for m in members if m.reason), None):
        return reason
    if any(m.contract != members[0].contract for m in members):
        return 'incompatible_identity_contract'
    if any(m.boundaries != members[0].boundaries for m in members):
        return 'different_source_or_scope'
    role_slot = 0 if physical else 1
    if any(not m.roles[role_slot] or m.roles[role_slot] != members[0].roles[role_slot] for m in members):
        return 'different_relation_or_neighbor'
    if any(m.ancestors != members[0].ancestors for m in members):
        return 'different_ancestor_context'
    cells = {}
    if (any(cells.setdefault(k, v) != v for m in members for k, v in m.attributes.items())
            or contradictory_qualifiers(m.node for m in members)):
        return 'conflicting_attributes'
    return None


def _verify(pid, bindings, data, blocked):
    counts = Counter()
    report = {'pattern_id': pid, 'status': 'review', 'reason': None, 'overlapping_keys': 0,
              'matched_key_names': 0, 'verified_groups': 0, 'verified_groups_by_basis': {}, 'rejected_keys_by_reason': counts}
    owners = {p.contributor_id for p in bindings}
    if (owners - data.inputs.keys() or not source_namespace([data.inputs[o] for o in owners])
            or any(data.inputs[o].get('kind', 'unit') != 'unit' or data.inputs[o]['graph'].get('extends_base_graph')
                   or (data.inputs[o]['graph'].get('conflict_isolation') or {}).get('coverage') == 'incomplete'
                   for o in owners)):
        report['reason'] = 'unverified_batch_or_namespace'
        return report, {}
    if any(p.graph_digest != data.digest(p.contributor_id) for p in bindings):
        report['reason'] = 'checkpoint_changed'
        return report, {}
    if len({(p.contributor_id, p.entity_type) for p in bindings}) != len(bindings) or any(
            p.entity_type in blocked for p in bindings):
        report['reason'] = 'competing_or_previously_assigned_projection'
        return report, {}
    access = {data.inputs[o]['graph'].get('access_fingerprint') for o in owners}
    axes = defaultdict(set)
    for binding in bindings:
        axes[_address(binding)[:3]].add(binding.key_columns)
    different_axes = any(len(columns) > 1 for columns in axes.values())
    physical = len(axes) == 1 and not different_axes
    equal_access = len(access) == 1 and all(access)
    table_grant = common_table_grant([data.inputs[o] for o in owners], bindings[0]) if physical and not equal_access else None
    tables = [_by_key(p, data) for p in bindings]
    if any(table is None for table in tables):
        report['reason'] = 'incomplete_typed_identity'
        return report, {}
    if not all(tables):
        report['reason'] = 'selected_binding_has_no_observations'
        return report, {}
    shared = set.intersection(*(set(table) for table in tables))
    report['overlapping_keys'] = len(shared)
    groups = {}
    for key in shared:
        entries = [table[key] for table in tables]
        if any(len(peers) != 1 for peers in entries):
            counts['nonunique_member_in_projection'] += 1
            continue
        members = [peers[0] for peers in entries]
        if not members[0].label or any(m.label != members[0].label for m in members):
            counts['missing_or_conflicting_names'] += 1
            continue
        report['matched_key_names'] += 1
        reason = 'different_axes_in_same_table' if different_axes else _blocked_reason(members, physical)
        if reason is None and not equal_access and (table_grant is None or not all(
                _grant_covers(m, p, data, table_grant) for m, p in zip(members, bindings, strict=True))):
            reason = 'unverified_access_fingerprint'
        if reason:
            counts[reason] += 1
            continue
        groups[digest('verified_projection_key_name_v1', key, members[0].label)] = [
            (p.contributor_id, m.node['id']) for p, m in zip(bindings, members, strict=True)]
    if len(groups) > 5000:
        counts['proposal_size_limit'] += len(groups)
        groups = {}
    if groups:
        basis = 'same_physical_projection' if physical else 'shared_versioned_key_section'
        report['verified_groups_by_basis'] = {basis: len(groups)}
        report['access_basis'] = 'unit_fingerprint' if equal_access else 'same_frozen_table_grant'
    report.update(status='verified' if groups else 'review', verified_groups=len(groups),
                  reason='verified_source_observations' if groups else
                  (min(counts, key=lambda k: (-counts[k], k)) if counts else 'no_shared_keys'),
                  rejected_keys_by_reason=dict(sorted(counts.items())))
    return report, groups


def verify_projection_proposals(policy, inputs, patterns, decisions):
    """Compile issued model proposals to frozen, exact-observation rules."""
    data = _Inputs(inputs)
    by_pattern = {p['pattern_id']: p for p in patterns}
    reports, rules = [], list(policy.verified_projection_rules)
    for decision in decisions:
        if decision.get('action') != 'propose':
            continue
        pid = decision['pattern_id']
        pattern = by_pattern[pid]
        bindings = []
        for part in decision['alignment']:
            member = pattern['members'][part['member_index']]
            role = member['roles'][part['role_index']]
            source = pattern['sources'][part['source_index']]
            owner = member['contributor_id']
            bindings.append(ProjectionBinding(contributor_id=owner, graph_digest=data.digest(owner),
                            entity_type=member['type'], source_id=source['source_id'], schema_name=source['schema'],
                            table=source['table'], key_columns=source['key_columns'], relation_type=role['relation_type'],
                            end=role['end'], opposite_key_columns=role['opposite_key_columns']))
        bindings.sort(key=lambda b: (b.contributor_id, b.entity_type))
        report, groups = _verify(pid, bindings, data, _blocked_types(policy))
        reports.append(report)
        if groups:
            rules.append(VerifiedProjectionRule(pattern_id=pid, bindings=tuple(bindings), observation_digests=sorted(groups)))
    if rules == policy.verified_projection_rules:
        return policy, reports
    return policy.model_copy(update={'verified_projection_rules': rules}), reports


def _has_one_input(output, owners, owner, nid):
    if owners[output['id']] != {(owner, nid)}:
        return False
    return all(s.get('contributor_id') == owner and not s.get('identity_decision_refs')
               and all(origin.get('contributor_id') == owner for origin in s.get('execution_origins', ()))
               for s in output['supports'])


def verified_projection_groups(graph, inputs, policy, remaps, stats):
    """Resolve exact originals to their current stage IDs, without widening prior groups."""
    data = _Inputs(inputs)
    stage = {owner: ids for owner, kind, _, ids in remaps if kind == 'unit'}
    owners = defaultdict(set)
    for owner, ids in stage.items():
        for node in data.inputs[owner]['graph']['nodes']:
            if node['id'] in ids:
                owners[ids[node['id']]].add((owner, node['id']))
    outputs = {n['id']: n for n in graph['nodes']}
    rejected = defaultdict(Counter)
    proposals = []
    for rule in policy.verified_projection_rules:
        report, groups = _verify(rule.pattern_id, rule.bindings, data, _blocked_types(policy))
        for token in rule.observation_digests:
            originals = groups.get(token)
            if not originals:
                rejected[rule.pattern_id][report['reason'] if not groups else 'observation_changed'] += 1
                continue
            targets = {stage.get(owner, {}).get(nid): nid for owner, nid in originals}
            ids = tuple(sorted(targets)) if None not in targets else ()
            if not ids or not all(target in outputs for target in ids):
                rejected[rule.pattern_id]['missing_stage_member'] += 1
            elif len(ids) == 1:
                rejected[rule.pattern_id]['already_one_output'] += 1
            elif len(ids) != len(originals) or any(not _has_one_input(outputs[stage[owner][nid]], owners, owner, nid)
                                                   for owner, nid in originals):
                rejected[rule.pattern_id]['overlaps_prior_identity_decision'] += 1
            else:
                proposals.append((ids, (None, rule.pattern_id, targets)))
    competing = Counter(nid for ids, _ in proposals for nid in ids)
    accepted = {}
    for ids, ref in proposals:
        if any(competing[nid] != 1 for nid in ids):
            rejected[ref[1]]['overlapping_proposals'] += 1
        else:
            accepted[ids] = ref
    stats['not_applied_by_pattern'] = rejected
    return accepted

"""Validate a merge reviewer's finite, source-bound alignment against frozen observations.

A selected table is a projection address, not a unique-key declaration. Repeated
codes, contradictory names, different roles and unverified cross-table identity
produce counts for the reviewer. Only concrete, corroborated members can move.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.hierarchy_bindings import mapped_direction
from lightgraph.runtime.config import ProjectionBinding, VerifiedProjectionRule
from lightgraph.source_values import identity_cell
from .contexts import business_context_identity
from .fusion import definition_contract
from .graph_storage import _graph_digest
from .hierarchy_observations import observation_scope
from .identity_authority import _global_authorities, source_address
from .observed_identity import _node_evidence, _unique_row_projection, contradictory_qualifiers
from .overlap_patterns import _blocked_types
from .relation_assertions import interval_union, support_assertion
from .projection_access import (attach_checkpoint_grant, attach_runtime_grants, common_table_grant,
                                checkpoint_observation_grant)
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


def _source_header(node, columns, address, owner, *, include_label):
    """Bind an endpoint to the physical header that supplied its observed keys and name."""
    headers = set()
    for support in node.get('supports', ()):
        p = support.get('provenance') or {}
        if tuple(p.get('key_columns') or ()) != columns or source_address(p.get('source')) != address:
            continue
        fields = (*columns, p.get('label_column')) if include_label else columns
        ordinals = p.get('source_column_ordinals') or {}
        if (not _own_support(support, owner) or not isinstance(ordinals, dict)
                or any(type(v) != int or not 1 <= v <= 16384 for v in ordinals.values())
                or len(set(ordinals.values())) != len(ordinals) or not set(fields) <= ordinals.keys()):
            return None
        headers.add(tuple((c, ordinals[c]) for c in fields))
    return headers.pop() if len(headers) == 1 else None


def _record_sheet_role(rows, cells, headers, node, node_key, peer, peer_key, edge, support, end, binding, meaning):
    reason, qualifications, locator = support_assertion(support)
    if reason:
        return False
    if 'business_context' in edge:
        context = business_context_identity(edge)
        if context['status'] == 'unknown':
            return False
        qualifications['edge_scope'] = context
    qualifications['edge_properties'] = edge.get('properties') or {}
    address, left, right = qualifications.pop('endpoint_bindings')
    own, opposite = (left, right) if end == 'source' else (right, left)
    def header_of(endpoint, columns, include_label):
        key = (binding.contributor_id, endpoint['id'], columns, include_label)
        if key not in headers:
            headers[key] = _source_header(endpoint, columns, address, binding.contributor_id, include_label=include_label)
        return headers[key]

    header = header_of(node, binding.key_columns, True)
    peer_header = header_of(peer, binding.opposite_key_columns, False)
    if not header or header[:-1] != own or peer_header != opposite:
        return False
    # Check declared values against typed cells if the mapping retained them in row_context.
    row = support.get('row_context') or {}
    values = row.get('values') or {}
    types = row.get('value_types') or {}
    if not node_key:
        return False
    frame = dict(zip(own, node_key, strict=True))
    representative = node['supports'][0]
    frame[header[-1]] = ('text', representative.get('observed_label') or representative.get('label'))
    for field, cell in zip(opposite, peer_key, strict=True):
        if frame.setdefault(field, cell) != cell:
            return False
    row_node = {'identity': values, 'identity_types': types}
    try:
        for (column, _), cell in frame.items():
            if column in values and (types[column] != cell[0] or identity_cell(row_node, column) != cell):
                return False
    except (ArithmeticError, TypeError, ValueError, LightGraphError):
        return False
    # Unknown role-specific contract fields, filters, scope and qualifications remain binding.
    role = {k: v for k, v in meaning.items() if k != 'type'}
    record = digest(address, end, header, locator['snapshot'], role, qualifications)
    spans = locator['rows']
    rows[record].extend(spans)
    for (c, ordinal), cell in frame.items():
        for start, stop in spans:
            cells[record, ordinal].append((start, stop, (c, cell)))
            cells[record, c].append((start, stop, (ordinal, cell)))
    return True


def _same_worksheet_roles(members):
    """An Agent-selected ordinary role may vary where the same object is witnessed on the same rows."""
    if not all(m.roles.worksheet for m in members):
        return False
    residual = [m.roles.physical[1] - m.roles.physical[0] for m in members]
    if any(other != residual[0] for other in residual):
        return False
    headers, cells = {}, defaultdict(list)
    first_rows = None
    for member in members:
        rows = defaultdict(list)
        if not all(_record_sheet_role(rows, cells, headers, *record) for record in member.roles.worksheet):
            return False
        row_set = {key: interval_union(intervals) for key, intervals in rows.items()}
        if first_rows is None:
            first_rows = row_set
        elif row_set != first_rows:
            return False
    for spans in cells.values():
        last_end, last_value = 0, None
        for start, end, value in sorted(spans, key=lambda span: span[:2]):
            if start <= last_end and value != last_value:
                return False
            last_end, last_value = max(last_end, end), value
    return True


class _RoleWitnesses(NamedTuple):
    physical: tuple
    projected: tuple | None
    shared_identity: tuple | None
    worksheet: list | None


def _witnesses(node, binding, index, *, compare_shared_identity=False):
    nodes, _, incident, entities, relations = index
    witnessed = set()
    all_roles = set()
    local_roles = set()
    local_witnesses = set()
    registry_roles, registry_witnesses = set(), set()
    sheet_records = []
    node_key = _cells(node, entities[node['type']].get('key_names'))
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
            conditions = (observation_scope(support), p.get('predicate'), p.get('constant_properties'),
                          support.get('properties'), support.get('row_context'))
            facts = digest(meaning, end, peer_cols, peer_key, *conditions, support.get('source_row_witness'))
            evidence = digest(source, own_cols, facts)
            local = facts if (*source, own_cols) == _address(binding) else evidence
            all_roles.add(evidence)
            local_roles.add(local)
            selected = (edge['type'] == binding.relation_type and end == binding.end
                        and (*source, own_cols) == _address(binding) and peer_cols == binding.opposite_key_columns)
            if selected:
                witnessed.add(evidence)
                local_witnesses.add(local)
            ordinary = (relation.get('hierarchy_orientation') in (None, 'none')
                        and not relation.get('acyclic') and not mapped_direction(meaning['type']))
            if selected and sheet_records is not None:
                if (not ordinary or edge.get('status', 'accepted') != 'accepted'
                        or not support.get('source_row_witness')):
                    sheet_records = None
                else:
                    sheet_records.append((node, node_key, peer, peer_key, edge, support, end, binding, meaning))
            if compare_shared_identity:
                registry_role = local
                if selected and ordinary:
                    # An independently verified global key owns this identity. Peers and
                    # their row locators remain on their own edges. Compare the selected
                    # association, its endpoint domain and its qualifications across sources.
                    peer_definition = entities[peer['type']]
                    peer_contract = definition_contract(peer_definition)
                    if peer['type'] == node['type']:
                        peer_contract['type'] = None  # Both are self-referencing entity types.
                    registry_role = digest('association_role_v1', meaning, end, peer_cols, peer_contract,
                                           [kind for kind, _ in peer_key], *conditions)
                registry_roles.add(registry_role)
                if selected:
                    registry_witnesses.add(registry_role)
    if not witnessed:
        return None
    return _RoleWitnesses((witnessed, all_roles),
                          (local_witnesses, local_roles) if known_applicability else None,
                          (registry_witnesses, registry_roles) if known_applicability and compare_shared_identity else None,
                          sheet_records if known_applicability else None)


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
    roles: _RoleWitnesses | None = None
    sections: set = field(default_factory=set)
    registries: set = field(default_factory=set)
    interpretations: set = field(default_factory=set)
    registry_contract: str | None = None
    ancestors: frozenset | None = None
    ancestor_items: list = field(default_factory=list)
    identity_boundary: str | None = None


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
    if definition.get('identity_scope') == 'global':
        authorities = _global_authorities(node, definition, {}, local_namespace=False)
        result.sections = {token for kind, token in authorities if kind == 'document'}
        result.interpretations = {token for kind, token in authorities if kind == 'interpretation'}
        result.registries = {token for kind, token in authorities if kind in {'registry', 'interpretation'}}
    if (len(own) != len(node['supports']) or not all(_own_support(s, binding.contributor_id) for s in own)
            or any(s['provenance'].get('label_column') in (None, '', *binding.key_columns) for s in own)
            or result.label in (str(value) for value in node['identity'].values())):
        result.reason = 'unverified_name_or_other_bindings'
        return result
    names = definition['key_names']
    if (len(names) != len(binding.key_columns) or node.get('identity_domain') != definition.get('identity_domain')
            or node.get('identity_origin', 'row') != 'row' or definition.get('identity_origin', 'row') != 'row'
            or (not (result.sections or result.registries)
                and any(business_context_identity(s)['status'] == 'unknown' for s in own))
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
    result.identity_boundary = canonical(node.get('identity_context'))
    if result.registries:
        # Each dictionary can name the directory and its physical key differently.
        # The registry proof binds the ordered key roles in both dictionaries.
        contract.pop('identity_domain', None)
        contract['identity_context_keys'] = [names.index(key) for key in definition.get('identity_context_keys') or ()]
        result.registry_contract = canonical([contract, node.get('node_role')])
    physical = _unique_row_projection(node, definition, _address(binding))
    if not physical:
        result.reason = 'conflicting_or_unbound_columns'
        return result
    signature, result.attributes = physical
    result.boundaries = (signature[-1], canonical(node.get('identity_context')),
                         tuple(sorted({_conditions(s) for s in own})))
    result.roles = _witnesses(node, binding, index, compare_shared_identity=bool(result.sections or result.registries))
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


def _compare_observations(members, physical, *, independent_grants=False):
    if reason := next((m.reason for m in members if m.reason), None):
        return reason, None
    registry = bool(set.intersection(*(m.registries for m in members)))
    common_identity = registry or bool(set.intersection(*(m.sections for m in members)))
    separate_observations = not physical and independent_grants and common_identity
    if (any(m.contract != members[0].contract for m in members)
            and not (registry and all(m.registry_contract == members[0].registry_contract for m in members))):
        return 'incompatible_identity_contract', None
    boundaries = [m.identity_boundary if separate_observations else m.boundaries for m in members]
    if any(boundary != boundaries[0] for boundary in boundaries):
        return 'different_source_or_scope', None
    basis = 'same_physical_projection' if physical else 'shared_versioned_key_section'
    if not physical and registry and not set.intersection(*(m.sections for m in members)):
        basis = ('shared_business_key_explanation' if set.intersection(*(m.interpretations for m in members))
                 else 'shared_versioned_registry')
    role_slot = 0 if physical else 1
    if not separate_observations and any(not m.roles[role_slot] or m.roles[role_slot] != members[0].roles[role_slot] for m in members):
        shared_role = ((registry or set.intersection(*(m.sections for m in members))) and members[0].roles.shared_identity
                       and all(m.roles.shared_identity == members[0].roles.shared_identity for m in members))
        if not shared_role:
            if not physical or not _same_worksheet_roles(members):
                return 'different_relation_or_neighbor', None
            basis = 'same_worksheet_identity_cells'
    if any(m.ancestors != members[0].ancestors for m in members):
        return 'different_ancestor_context', None
    cells = {}
    if ((not separate_observations and any(cells.setdefault(k, v) != v for m in members for k, v in m.attributes.items()))
            or contradictory_qualifiers(m.node for m in members)):
        return 'conflicting_attributes', None
    return None, basis


def _observation_grant(member, binding, data):
    nodes, _, incident, _, _ = data.index(binding.contributor_id)
    items = [member.node, *member.ancestor_items]
    for end, edge in incident[member.node['id']]:
        items.extend((edge, nodes[edge['target' if end == 'source' else 'source']]))
    return checkpoint_observation_grant(data.inputs[binding.contributor_id], items)


def _observation_mismatch(members, physical):
    return _compare_observations(members, physical)[0]


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
    evidence_combinations, bases, checks = Counter(), Counter(), Counter()
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
        grant_issues = [_observation_grant(m, p, data) for m, p in zip(members, bindings, strict=True)]
        checked_grants = not any(grant_issues)
        mismatch, basis = _compare_observations(members, physical, independent_grants=checked_grants)
        shared_section = bool(set.intersection(*(m.sections for m in members)))
        shared_registry = bool(set.intersection(*(m.registries for m in members)))
        checks[canonical({'source_access': ('verified_per_observation' if checked_grants else
            'same_unit_fingerprint' if equal_access else 'unverified'),
            'access_issues': sorted(set(filter(None, grant_issues))) if not checked_grants and not equal_access else [],
            'identity_evidence': ('physical_projection' if physical else 'shared_identity' if
                                  shared_section or shared_registry else 'missing_shared_identity'),
            'observation_check': mismatch or 'verified',
            'output_visibility': 'preserve_each_support'})] += 1
        if different_axes:
            reason = 'different_axes_in_same_table'
        elif physical:
            reason = mismatch
        else:
            # Count each matched key once. A missing shared proof used to mask
            # every independent observation or access issue on that same key.
            evidence_combinations[shared_section, shared_registry, mismatch or 'observations_agree', equal_access] += 1
            reason = mismatch if shared_registry or shared_section else 'cross_table_identity_unverified'
        if reason is None and not equal_access and not (not physical and checked_grants) and (table_grant is None or not all(
                _grant_covers(m, p, data, table_grant) for m, p in zip(members, bindings, strict=True))):
            reason = 'unverified_access_fingerprint'
        if reason:
            counts[reason] += 1
            continue
        bases[basis] += 1
        groups[digest('verified_projection_key_name_v1', key, members[0].label)] = [
            (p.contributor_id, m.node['id']) for p, m in zip(bindings, members, strict=True)]
    if groups:
        report['verified_groups_by_basis'] = dict(sorted(bases.items()))
        report['access_basis'] = ('unit_fingerprint' if equal_access else
                                 'same_frozen_table_grant' if physical else 'individual_frozen_observation_grants')
    if checks:
        import json
        report['verification_checks'] = [{**json.loads(key), 'keys': count} for key, count in sorted(checks.items())]
    if len(axes) > 1 and not different_axes:
        report['cross_table_matched_key_checks'] = [
            {'shared_key_section_verified': section, **({'shared_registry_key_verified': True} if registry else {}),
             'first_observation_result': observation,
             'unit_fingerprints_match': access, 'keys': count}
            for (section, registry, observation, access), count in sorted(evidence_combinations.items())]
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
            rules.append(VerifiedProjectionRule(pattern_id=pid, bindings=tuple(bindings), observation_digests=sorted(groups),
                                                composition_version=3))
    if rules == policy.verified_projection_rules:
        return policy, reports
    return policy.model_copy(update={'verified_projection_rules': rules}), reports


@dataclass
class ProjectionInputs:
    by_stage: dict[str, dict[str, str]]
    compose: bool
    corroborating_patterns: tuple[str, ...] = ()


def _covered_inputs(output, selected, actual):
    """Every original behind this stage ID must be selected and attributable to its own support."""
    if actual != selected:
        return None
    by_owner = dict(selected)
    if len(by_owner) != len(selected):
        return None  # Two leaves from one contributor need distinct support-level input IDs.
    seen = set()
    for support in output['supports']:
        owner = support.get('contributor_id')
        if (owner not in by_owner or any(support.get(k) for k in (
                'identity_decision_refs', 'identity_decision_members', 'identity_approval_ref', 'inherited_from'))
                or any(o.get('contributor_id') != owner for o in support.get('execution_origins', ()))):
            return None
        seen.add(owner)
    return by_owner if seen == set(by_owner) else None


def verified_projection_groups(graph, inputs, policy, remaps, stats):
    """Resolve approved leaf sets without importing any unreviewed member of a prior group."""
    data = _Inputs(inputs)
    stage = {owner: ids for owner, kind, _, ids in remaps if kind == 'unit'}
    owners = defaultdict(set)
    for owner, ids in stage.items():
        for node in data.inputs[owner]['graph']['nodes']:
            if node['id'] in ids:
                owners[ids[node['id']]].add((owner, node['id']))
    outputs = {n['id']: n for n in graph['nodes']}
    separated = {(item['contributor_id'], nid) for item in inputs
                 for record in item['graph'].get('identity_separations', ()) for nid in record['members']}
    rejected = defaultdict(Counter)
    proposals = defaultdict(list)
    for rule in policy.verified_projection_rules:
        report, groups = _verify(rule.pattern_id, rule.bindings, data, _blocked_types(policy))
        for token in rule.observation_digests:
            originals = groups.get(token)
            if not originals:
                rejected[rule.pattern_id][report['reason'] if not groups else 'observation_changed'] += 1
                continue
            selected = defaultdict(set)
            for owner, nid in originals:
                selected[stage.get(owner, {}).get(nid)].add((owner, nid))
            ids = tuple(sorted(selected)) if None not in selected else ()
            if not ids or not all(target in outputs for target in ids):
                rejected[rule.pattern_id]['missing_stage_member'] += 1
            elif len(ids) == 1:
                rejected[rule.pattern_id]['already_one_output'] += 1
            elif (not separated.isdisjoint(originals)
                  or rule.composition_version == 1 and len(ids) != len(originals)):
                rejected[rule.pattern_id]['overlaps_prior_identity_decision'] += 1
            else:
                inputs_by_stage = {i: _covered_inputs(outputs[i], selected[i], owners[i]) for i in ids}
                if all(inputs_by_stage.values()):
                    proposals[ids].append((rule, inputs_by_stage))
                else:
                    rejected[rule.pattern_id]['overlaps_prior_identity_decision'] += 1
    competing = Counter(nid for ids in proposals for nid in ids)
    accepted = {}
    for ids, proofs in proposals.items():
        # Every proof above covers *all* original occurrences behind these stage IDs.
        # Identical sets corroborate one another. Intersecting sets cannot establish
        # transitivity. Retain the earlier behavior when replaying old policies.
        legacy_overlap = len(proofs) > 1 and any(rule.composition_version < 3 for rule, _ in proofs)
        if legacy_overlap or any(competing[nid] != 1 for nid in ids):
            for rule, _ in proofs:
                rejected[rule.pattern_id]['overlapping_proposals'] += 1
            continue
        rule, inputs_by_stage = min(proofs, key=lambda proof: proof[0].pattern_id)
        also = tuple(sorted({p.pattern_id for p, _ in proofs} - {rule.pattern_id}))
        for pid in also:
            rejected[pid]['same_verified_members'] += 1
        accepted[ids] = (None, rule.pattern_id, ProjectionInputs(inputs_by_stage, rule.composition_version >= 2, also))
    stats['not_applied_by_pattern'] = rejected
    return accepted

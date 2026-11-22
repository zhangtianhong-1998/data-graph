"""Recognize independent global declarations that project the same physical hierarchy."""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass

from lightgraph.contracts import canonical, digest
from lightgraph.source_values import identity_cell
from .contexts import business_context_identity
from .fusion import definition_contract, relation_identity, remap_graph_endpoints


def _address(prov):
    source = prov.get('source') or {}
    address = tuple(source.get(k) for k in ('source_id', 'schema', 'table'))
    return address if all(isinstance(k, str) and k for k in address) else None


def _boundary(support):
    prov = support['provenance']
    context = support.get('business_context') or {}
    if (not support.get('scope_ref') or support.get('inherited_from') or support.get('lookup_matches')
            or context.get('missing_dimensions') or context.get('conflicts')):
        return None
    row = support.get('row_context') or {}
    return canonical([support['scope_ref'], sorted(support.get('required_scope_refs') or []),
                      prov.get('scope_columns', []), business_context_identity(support),
                      row.get('values'), row.get('value_types'), prov.get('predicate')])


@dataclass
class _Member:
    address: tuple
    value: str
    bindings: frozenset
    observations: frozenset
    unit: str


def _physical_properties(support, columns):
    """Position constants can cite a physical key column already present in the witness."""
    prov = support['provenance']
    constants = prov.get('constant_properties') or {}
    columns = set(columns)
    properties = set()
    for name, values in support.get('properties', {}).items():
        constant = constants.get(name)
        if isinstance(constant, str) and constant in columns and values == [constant]:
            continue
        physical = (prov.get('property_columns') or {}).get(name, name)
        properties.add((physical, canonical(values)))
    remaining = {k: v for k, v in constants.items() if not (
        isinstance(v, str) and v in columns and support.get('properties', {}).get(k) == [v])}
    return canonical([sorted(properties), remaining])


def _member(node, definition):
    names = definition.get('key_names') or []
    identity = node.get('identity') or {}
    types = node.get('identity_types') or {}
    if (definition.get('identity_scope') != 'global' or definition.get('identity_context_keys')
            or not definition.get('identity_domain') or node.get('identity_domain') != definition['identity_domain']
            or (node.get('identity_context') or {}).get('status') != 'global'
            or (node.get('identity_context') or {}).get('dimensions')
            or node.get('identity_origin', definition.get('identity_origin', 'row')) != 'row'
            or not names or set(names) != set(identity) or set(names) != set(types)
            or any(identity[k] is None or identity[k] == '' for k in names)):
        return None
    cells = [identity_cell(node, k) for k in names]
    if any(types[k] != cell[0] for k, cell in zip(names, cells)):
        return None
    addresses, units, labels, observations, bindings = set(), set(), set(), set(), set()
    for support in node.get('supports') or []:
        prov = support.get('provenance') or {}
        table = _address(prov)
        keys = tuple(prov.get('key_columns') or [])
        label = support.get('observed_label') or support.get('label')
        boundary = _boundary(support) if prov else None
        if (not table or not prov.get('label_column') or not label or boundary is None
                or len(keys) != len(names) or not all(isinstance(k, str) and k for k in keys)
                or len(set(keys)) != len(keys) or support.get('identity_basis')
                or support.get('original_key_values') is not None):
            return None
        unit = support.get('contributor_id')
        if not unit or any(o.get('contributor_id', unit) != unit for o in support.get('execution_origins', [])):
            return None
        addresses.add(table)
        units.add(unit)
        labels.add(label)
        bindings.add(keys)
        observations.add((keys, prov['label_column'], label, boundary, _physical_properties(support, keys)))
    if len(addresses) != 1 or len(units) != 1 or len(labels) != 1:
        return None
    return _Member(addresses.pop(), canonical(cells), frozenset(bindings), frozenset(observations), units.pop())


def _neighborhoods(graph, nodes, members, relations):
    """Compare parent and child observations in physical coordinates, including reverse encodings."""
    incidents = defaultdict(set)
    blocked = set()
    known = defaultdict(set)
    observations = []
    for edge in graph['edges']:
        endpoints = (edge['source'], edge['target'])
        if not any(n in members for n in endpoints):
            continue
        left, right = (members.get(n) for n in endpoints)
        relation = relations.get(edge['type'], {})
        orientation = relation.get('hierarchy_orientation')
        if (not left or not right or left.address != right.address or nodes[endpoints[0]]['type'] != nodes[endpoints[1]]['type']
                or relation.get('source_type') != nodes[endpoints[0]]['type']
                or relation.get('target_type') != nodes[endpoints[1]]['type']
                or orientation == 'none' or not edge.get('supports')):
            blocked.update(endpoints)
            continue
        for support in edge['supports']:
            prov = support.get('provenance') or {}
            columns = tuple(tuple(prov.get(end + '_key_columns') or []) for end in ('source', 'target'))
            boundary = _boundary(support) if prov else None
            if (_address(prov) != left.address or boundary is None or columns[0] not in left.bindings
                    or columns[1] not in right.bindings
                    or (columns[0] == columns[1] and endpoints[0] != endpoints[1])
                    or prov.get('source_lookup') or prov.get('target_lookup')):
                blocked.update(endpoints)
                continue
            if endpoints[0] == endpoints[1]:
                continue
            fact = (boundary, _physical_properties(support, columns[0] + columns[1]))
            # A declared direction only corroborates projections of this exact
            # physical edge and applicability, never unrelated rows in the table.
            positions = [(col, member.value, member.observations)
                         for col, member in zip(columns, (left, right))]
            pair = (left.address, tuple(sorted(positions, key=lambda pos: pos[0])), fact)
            if orientation in ('parent_to_child', 'child_to_parent'):
                known[pair].add(columns[0 if orientation == 'parent_to_child' else 1])
            observations.append((pair, columns, endpoints, fact))
    missing_orientation = set()
    for pair, columns, endpoints, boundary in observations:
        if len(known[pair]) != 1:
            missing_orientation.update(endpoints)
            continue
        parent, = known[pair]
        for index in (0, 1):
            other = 1 - index
            neighbor = members[endpoints[other]]
            incidents[endpoints[index]].add((columns[index] == parent, columns[index], columns[other],
                                             neighbor.value, neighbor.observations, boundary))
    return incidents, blocked, missing_orientation


def _verified_groups(graph, excluded_types, stats):
    nodes = {n['id']: n for n in graph['nodes']}
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    relations = {r['type']: r for r in graph['definitions']['relations']}
    eligible = [n for n in nodes.values() if n['type'] not in excluded_types and
                definitions.get(n['type'], {}).get('original_type') not in excluded_types and
                definitions.get(n['type'], {}).get('identity_scope') == 'global']
    stats['global_node_inputs'] = len(eligible)
    if not eligible:
        return
    members = {}
    rejected_nodes = Counter()
    physical_groups = defaultdict(set)
    catalog = graph.get('evidence_catalog', {})
    for node in eligible:
        definition = definitions[node['type']]
        refs = definition.get('identity_evidence_refs') or []
        if not refs or any(catalog.get(ref, {}).get('kind') != 'source_probe' for ref in refs):
            rejected_nodes['authoritative_or_unverified_domain'] += 1
            continue
        member = _member(node, definition)
        if member:
            members[node['id']] = member
            for binding in member.bindings:
                physical_groups[member.address, binding, member.value].add(node['id'])
        else:
            rejected_nodes['incomplete_or_incompatible_source_observation'] += 1
    stats['excluded_nodes'] = dict(rejected_nodes)
    stats['verified_source_node_inputs'] = len(members)
    if not any(len(ids) > 1 for ids in physical_groups.values()):
        return
    incidents, invalid_roles, missing_orientation = _neighborhoods(graph, nodes, members, relations)
    blocked = set()
    isolation = graph.get('conflict_isolation') or {}
    incomplete = {c['contributor_id'] for c in isolation.get('contributors_with_gaps', []) if c.get('contributor_id')}
    blocked.update(nid for nid, member in members.items() if member.unit in incomplete)
    for issue in (*isolation.get('details', []), *graph.get('context_cycle_diagnostics', []),
                  *graph.get('unresolved_relations', [])):
        blocked.update(issue.get('node_ids') or [])
        blocked.update(issue.get(k) for k in ('node_id', 'source', 'target'))
        for choices in (issue.get('candidate_endpoints') or {}).values():
            blocked.update(choices)
    # A single key must identify exactly the same set of occurrences at every
    # observed level. Never take a transitive closure across overlapping sets.
    sets = {frozenset(ids) for ids in physical_groups.values() if len(ids) > 1
            and len({members[i].unit for i in ids}) > 1}
    stats['cross_unit_physical_sets'] = len(sets)
    rejected_sets = Counter()
    for ids in sets:
        first = members[next(iter(ids))]
        if ids & blocked:
            reason = 'unresolved_or_incomplete_contribution'
        elif ids & invalid_roles:
            reason = 'incompatible_endpoint_or_role'
        elif ids & missing_orientation:
            reason = 'undeclared_or_conflicting_direction'
        elif any(physical_groups[first.address, k, first.value] != ids for i in ids for k in members[i].bindings):
            reason = 'overlapping_projection_sets'
        elif len({nodes[i]['type'] for i in ids}) != len(ids):
            reason = 'competing_instances_in_one_type'
        elif len({members[i].observations for i in ids}) != 1:
            reason = 'different_node_observations_or_scope'
        elif any(not incidents[i] for i in ids):
            reason = 'no_witnessed_nonself_relationship'
        elif len({frozenset(incidents[i]) for i in ids}) != 1:
            reason = 'different_physical_neighbors_or_edge_scope'
        else:
            yield ids
            continue
        rejected_sets[reason] += 1
    stats['rejected_physical_sets'] = dict(sorted(rejected_sets.items()))


def _compatible_remaps(graph, batches, nodes):
    """Retain groups for which all existing relation types still have a single endpoint contract."""
    owners = {nid: winner for winner, ids in batches.items() for nid in ids}
    while owners:
        pairs = defaultdict(set)
        touched = defaultdict(set)
        ids_by_edge = defaultdict(list)
        rejected = set()
        for edge in graph['edges']:
            old = edge['source'], edge['target']
            new = tuple(owners.get(n, n) for n in old)
            changed = {owners[n] for n in old if n in owners}
            pairs[edge['type']].add(tuple(nodes[n]['type'] for n in new))
            touched[edge['type']].update(changed)
            if new != old and new[0] == new[1] and old[0] != old[1]:
                rejected.update(changed)
            key = relation_identity({**edge, 'source': new[0], 'target': new[1]})
            ids_by_edge[key].append(changed)
        for family, contracts in pairs.items():
            if len(contracts) > 1:
                rejected.update(touched[family])
        for colliding in ids_by_edge.values():
            if len(colliding) > 1:
                rejected.update(set.union(*colliding))
        if not rejected:
            break
        owners = {n: target for n, target in owners.items() if target not in rejected}
    return owners


def _merge_members(winner, batch, definitions):
    for node in batch:
        original = {k: deepcopy(node[k]) for k in ('id', 'type', 'identity', 'identity_types', 'identity_domain',
                    'original_type', 'definition_variant') if k in node}
        for support in node['supports']:
            origin = {**original, 'contributor_id': support['contributor_id']}
            origins = support.setdefault('identity_origins', [])
            if origin not in origins:
                origins.append(origin)
            support['identity_basis'] = 'observed_hierarchy_projection_v1'
            # A composite key may use different logical names in each projection.
            keys = support['provenance']['key_columns']
            names = definitions[node['type']]['key_names']
            support['original_key_values'] = dict(zip(keys, (node['identity'][k] for k in names), strict=True))
    for node in batch:
        if node is winner:
            continue
        winner['supports'].extend(node['supports'])
        for field in ('aliases', 'descriptions', 'english_names', 'provenance'):
            if node.get(field):
                previous = winner.setdefault(field, [])
                seen = {canonical(v) for v in previous}
                for value in node[field]:
                    token = canonical(value)
                    if token not in seen:
                        seen.add(token)
                        previous.append(value)
        for key, values in node.get('properties', {}).items():
            dest = winner.setdefault('properties', {}).setdefault(key, [])
            seen = {canonical(v) for v in dest}
            for value in values:
                if canonical(value) not in seen:
                    dest.append(value)
                    seen.add(canonical(value))


def reconcile_hierarchy_projections(graph, *, excluded_types=(), diagnostics=None):
    """Collapse verified source replays in a fresh unit merge; return removed node count."""
    nodes = {n['id']: n for n in graph['nodes']}
    stats = {} if diagnostics is None else diagnostics
    batches = {min(ids, key=lambda i: (nodes[i]['type'], i)): ids
               for ids in _verified_groups(graph, excluded_types, stats)}
    stats['groups_with_matching_roles_and_scope'] = len(batches)
    owners = _compatible_remaps(graph, batches, nodes)
    remap = {nid: winner for nid, winner in owners.items() if nid != winner}
    stats['accepted_groups'] = len(set(remap.values()))
    if not remap:
        return 0
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    for winner in sorted(set(remap.values())):
        batch = [nodes[i] for i in sorted(batches[winner])]
        _merge_members(nodes[winner], batch, definitions)
    changed_edges = [e for e in graph['edges'] if e['source'] in remap or e['target'] in remap]
    remap_graph_endpoints(graph, remap, self_contained=True, record_fusion=True)
    graph['nodes'] = [n for n in graph['nodes'] if n['id'] not in remap]
    graph['edges'].sort(key=lambda e: e['id'])
    relations = {d['type']: d for d in graph['definitions']['relations']}
    changed_relations = {}
    for edge in changed_edges:
        rel = relations[edge['type']]
        changed_relations[edge['type']] = rel
        for end in ('source', 'target'):
            rel[end + '_type'] = nodes[edge[end]]['type']
    for relation in changed_relations.values():
        if relation.get('definition_variant'):
            relation['definition_variant']['contract_digest'] = digest(definition_contract(relation))
    for edge in graph['edges']:
        if edge.get('definition_variant') and edge['type'] in changed_relations:
            edge['definition_variant'] = deepcopy(relations[edge['type']]['definition_variant'])
    retired_types = {nodes[i]['type'] for i in remap}
    used_types = {n['type'] for n in graph['nodes']}
    used_types.update(rel.get(end) for rel in relations.values() for end in ('source_type', 'target_type'))
    graph['definitions']['entities'] = [d for d in graph['definitions']['entities']
                                        if d['type'] not in retired_types or d['type'] in used_types]
    # Suggestions are not identity edges. Remap residual suggestions and
    # discard those that now describe a single visible object.
    candidates = []
    for group in graph.get('candidate_identity_merges', []):
        ids = sorted({remap.get(n, n) for n in group['node_ids']})
        if group.get('match_basis') == 'same_typed_identity' and any(n in remap for n in group['node_ids']):
            ids = [i for i in ids if nodes[i]['type'] == group.get('entity_type')
                   and nodes[i]['identity'] == group.get('identity')]
        if len(ids) > 1:
            group['node_ids'] = ids
            if 'entity_types' in group:
                group['entity_types'] = sorted({nodes[i]['type'] for i in ids})
            candidates.append(group)
    graph['candidate_identity_merges'] = candidates
    return len(remap)

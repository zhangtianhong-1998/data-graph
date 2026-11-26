"""Replay explicit identity decisions against one immutable construction graph."""
from collections import defaultdict
from copy import deepcopy
from itertools import combinations

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.source_values import original_key_types
from .context_topology import detect_context_cycles
from .contexts import business_context_identity
from .fusion import definition_conflict_diagnostics, remap_graph_endpoints
from .hierarchy_replay import _update_endpoint_contracts
from .observed_identity import contradictory_qualifiers


def candidate_key(base_id, candidate):
    return 'pc_' + digest(base_id, candidate.get('reason'), sorted(set(candidate['node_ids'])))[:40]


class Decisions:
    def __init__(self, records):
        self.owner = {}
        self.groups = defaultdict(set)
        self.different = set()
        for decision in records:
            if decision['status'] != 'active' or decision['verdict'] != 'same':
                continue
            group = self.expanded(decision['members'])
            owner = min(group)
            for member in group:
                self.groups.pop(self.owner.get(member, member), None)
            self.groups[owner] = group
            self.owner.update(dict.fromkeys(group, owner))
        for decision in records:
            if decision['status'] != 'active' or decision['verdict'] != 'different':
                continue
            owners = self.representatives(decision['members'])
            if len(owners) < len(decision['members']):
                raise LightGraphError('resolution_cannot_link_conflict')
            self.different.update(combinations(owners, 2))

    def expanded(self, members):
        return {original for member in members
                for original in self.groups.get(self.owner.get(member, member), {member})}

    def representatives(self, members):
        return sorted({self.owner.get(member, member) for member in members})

    def resolved(self, members):
        return all(pair in self.different for pair in combinations(self.representatives(members), 2))


def check_review_scope(raw, visible, members, *, merge):
    """A publication-wide edit must not change a hidden observation or disclose another member."""
    ids = set(members)
    def touched(graph):
        return {item['id']: item for field in ('nodes', 'edges', 'unresolved_relations')
                for item in graph.get(field, []) if item['id'] in ids or item.get('source') in ids
                or item.get('target') in ids or ids.intersection(item.get('node_ids', []))}
    authorized = touched(visible)
    affected = touched(raw)
    for key, item in affected.items():
        if key not in authorized or len(item.get('supports', ())) != len(authorized[key].get('supports', ())):
            raise LightGraphError('resolution_scope_conflict')

    def audience(support):
        scopes = frozenset({support['scope_ref'], *support.get('required_scope_refs', [])})
        return scopes, canonical(business_context_identity(support))

    # A permanent edit is visible in every filtered version of this graph.
    # Confirming identity cannot carry a decision based on privileged rows into
    # a view that can read only one of the original objects or its witnesses.
    node_audiences = set()
    for nid in ids:
        conditions = {audience(s) for s in affected[nid]['supports']}
        if len(conditions) != 1:
            raise LightGraphError('resolution_scope_conflict')
        node_audiences.update(conditions)
    if merge and len(node_audiences) != 1:
        raise LightGraphError('resolution_scope_conflict')
    if any(audience(s) not in node_audiences for item in affected.values() for s in item['supports']):
        raise LightGraphError('resolution_scope_conflict')


def _record_local_key(node, key_names, support):
    """Preserve this observation's ordered physical key before changing its owner."""
    provenance = support.get('provenance') or {}
    columns = provenance.get('key_columns')
    address = provenance.get('source') or {}
    if (not address.get('source_id') or not address.get('table') or not isinstance(columns, list)
            or not columns or not all(isinstance(c, str) and c for c in columns) or len(set(columns)) != len(columns)):
        return
    row = support.get('row_context') or {}
    values, types = row.get('values') or {}, row.get('value_types') or {}
    if all(c in (provenance.get('context_columns') or []) and values.get(c) is not None and types.get(c)
           for c in columns):
        observed = {c: values[c] for c in columns}
        kinds = {c: types[c] for c in columns}
    elif (not support.get('identity_origins') and key_names and len(key_names) == len(columns)
          and set(key_names) == set(node.get('identity', {}))
          and all(node['identity'][k] is not None and node.get('identity_types', {}).get(k) for k in key_names)):
        observed = {c: node['identity'][k] for c, k in zip(columns, key_names)}
        kinds = {c: node['identity_types'][k] for c, k in zip(columns, key_names)}
    elif isinstance(support.get('original_key_values'), dict) and set(support['original_key_values']) == set(columns):
        # A previous fusion may have already changed this member's logical key.
        # Only its pre-existing witnesses can recover the original typed values.
        observed = support['original_key_values']
        kinds = original_key_types(observed, support.get('identity_origins') or [])
        if not kinds:
            return
    else:
        return
    if ('original_key_values' not in support or canonical(support['original_key_values']) == canonical(observed)):
        support.setdefault('original_key_values', observed)
        support.setdefault('original_key_types', kinds)


def project(raw, decisions, base_id):
    graph = deepcopy(raw)
    nodes = {node['id']: node for node in graph['nodes']}
    state = Decisions(decisions)
    inherited = raw.get('identity_separations', [])
    if inherited:
        from .identity_separations import retain_separations
        retain_separations(graph, inherited)
        inherited = graph.get('identity_separations', [])
        if any(len(state.representatives(row['members'])) < 2 for row in inherited):
            raise LightGraphError('resolution_cannot_link_conflict')
    groups = state.groups
    if groups:
        owners = state.owner
        remap = {n: owner for n, owner in owners.items() if owner != n}
        touched = {edge['type'] for edge in graph['edges']
                   if edge['source'] in remap or edge['target'] in remap}
        relations = {d['type']: d for d in graph['definitions']['relations']}
        for edge in graph['edges']:
            if edge['type'] not in touched:
                continue
            source, target = edge['source'], edge['target']
            definition = relations.get(edge['type'], {})
            if (definition.get('source_type'), definition.get('target_type')) != (nodes[source]['type'], nodes[target]['type']):
                raise LightGraphError('resolution_relation_conflict')
            if (source != target and owners.get(source, source) == owners.get(target, target)
                    and not definition.get('allow_self_links')):
                raise LightGraphError('resolution_hierarchy_conflict')
        originals = [{k: edge[k] for k in ('id', 'type', 'source', 'target')} for edge in graph['edges']]
        for edge in graph['edges']:
            endpoints = {end: edge[end] for end in ('source', 'target') if edge[end] in owners}
            if endpoints:
                for support in edge['supports']:
                    original_ends = support.setdefault('identity_decision_endpoints', {})
                    for end, member in endpoints.items():
                        original_ends.setdefault(end, {})[base_id] = member
        remap_graph_endpoints(graph, remap, self_contained=True, record_fusion=True)
        for definition in graph['definitions']['relations']:
            if definition['type'] in touched and definition.get('acyclic'):
                edges = [e for e in graph['edges'] if e['type'] == definition['type']]
                check = detect_context_cycles(edges)
                if not check['complete'] or check['cycle_edge_ids']:
                    raise LightGraphError('resolution_hierarchy_conflict')
        decision_refs = defaultdict(list)
        for decision in decisions:
            if decision['status'] == 'active' and decision['verdict'] == 'same':
                decision_refs[owners[decision['members'][0]]].append(decision['decision_id'])
        key_names = {e['type']: e.get('key_names', []) for e in graph['definitions']['entities']}
        for winner_id, members in groups.items():
            winner = nodes[winner_id]
            if len({nodes[n].get('node_role') for n in members}) != 1:
                raise LightGraphError('resolution_type_conflict')
            # A dictionary/metadata column is a physical object with its own address.
            if any(nodes[n].get('identity_origin') == 'physical_column' or nodes[n].get('node_role') == 'metadata_column'
                   for n in members):
                raise LightGraphError('resolution_type_conflict')
            batch = [nodes[n] for n in sorted(members)]
            if contradictory_qualifiers(batch):
                raise LightGraphError('resolution_attribute_conflict')
            supports = []
            for node in batch:
                origin = {k: deepcopy(node[k]) for k in (
                    'id', 'type', 'identity', 'identity_types', 'identity_domain', 'original_type',
                    'definition_variant', 'unit_id', 'unit_original_id') if k in node}
                for support in node['supports']:
                    _record_local_key(node, key_names.get(node['type']), support)
                    support.setdefault('identity_decision_members', {})[base_id] = node['id']
                    origins = support.setdefault('identity_origins', [])
                    member_origin = dict(origin)
                    if support.get('contributor_id'):
                        member_origin['contributor_id'] = support['contributor_id']
                    if member_origin not in origins:
                        origins.append(member_origin)
                    support['identity_decision_refs'] = sorted(set(support.get('identity_decision_refs', []))
                                                               | set(decision_refs[winner_id]))
                    supports.append(support)
            winner['supports'] = supports
            for field in ('aliases', 'descriptions', 'english_names', 'provenance', 'scopes'):
                if any(node.get(field) for node in batch):
                    values = {canonical(v): v for node in batch for v in node.get(field, [])}
                    winner[field] = [values[k] for k in sorted(values)]
            properties = defaultdict(dict)
            for node in batch:
                for name, values in node.get('properties', {}).items():
                    properties[name].update({canonical(value): value for value in values})
            winner['properties'] = {name: [values[k] for k in sorted(values)] for name, values in properties.items()}
            for member in members - {winner_id}:
                del nodes[member]
        graph['nodes'] = list(nodes.values())
        _update_endpoint_contracts(graph, nodes, touched)
        # A decision equates objects only. Keep both relationship observations even
        # when remapping their endpoints yields identical structural edge IDs.
        edge_ids = {}
        occupied = {edge['id'] for edge in graph['edges']}
        siblings = defaultdict(list)
        for original, edge in zip(originals, graph['edges'], strict=True):
            siblings[edge['id']].append((original, edge))
        for key, records in siblings.items():
            records.sort(key=lambda pair: (key != pair[0]['id'], pair[0]['id']))
            for index, (old, edge) in enumerate(records):
                if index:
                    counter = 0
                    while True:
                        identifier = 'r_' + digest('human_identity_parallel', key, old['id'], counter)
                        if identifier not in occupied:
                            break
                        counter += 1
                    edge['id'] = identifier
                    occupied.add(identifier)
                edge_ids[old['id']] = edge['id']
                if any(edge[k] != v for k, v in old.items()):
                    origin = {'edge_id': old['id'], 'type': old['type'], 'source': old['source'], 'target': old['target']}
                    for support in edge['supports']:
                        origins = support.setdefault('endpoint_origins', [])
                        if origin not in origins:
                            origins.append(deepcopy(origin))
        for key in ('context_cycle_diagnostics', 'candidate_column_alignments'):
            for item in graph.get(key, []):
                if 'edge_ids' in item:
                    item['edge_ids'] = sorted({edge_ids.get(e, e) for e in item['edge_ids']})
                if 'node_ids' in item:
                    item['node_ids'] = state.representatives(item['node_ids'])
        graph['definition_conflicts'] = definition_conflict_diagnostics(graph)
    graph['candidate_identity_merges'] = [
        {**group, 'candidate_id': candidate_key(base_id, group), 'node_ids': state.representatives(group['node_ids'])}
        for group in raw.get('candidate_identity_merges', []) if not state.resolved(group['node_ids'])]
    if state.different or inherited:
        from .identity_separations import capture_separations, retain_separations
        recorded = capture_separations(graph, state.different)
        retain_separations(graph, [*inherited, *recorded])
    return graph

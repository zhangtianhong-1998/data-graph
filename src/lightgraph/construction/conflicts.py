"""Quarantine observed label conflicts without inventing identity repairs.

Only complete projections whose sole failure is a node-local name conflict can
publish a subset. Resource, source, schema and topology failures remain blockers.
The original candidate is immutable and retains every counterexample.
"""
from copy import deepcopy

from lightgraph.errors import LightGraphError
from lightgraph.source_values import persisted_identity_key


def label_conflict_ids(diagnostics):
    if not diagnostics or any(d.get('code') != 'same_identity_conflicting_label'
            or not isinstance(d.get('node_id'), str) for d in diagnostics):
        return None
    return {d['node_id'] for d in diagnostics}


def subset_available(validation):
    return validation is not None and not validation.passed and label_conflict_ids(validation.diagnostics) is not None


def exclude_nodes(graph, blocked):
    """Remove whole identities and their incident edges; preserve all other supports."""
    if not blocked:
        return graph
    nodes = [n for n in graph.get('nodes', []) if n['id'] not in blocked]
    ids = {n['id'] for n in nodes}
    if graph.get('extends_base_graph'):
        edges = [e for e in graph.get('edges', []) if e['source'] not in blocked and e['target'] not in blocked]
    else:
        edges = [e for e in graph.get('edges', []) if e['source'] in ids and e['target'] in ids]
    return {**graph, 'nodes': nodes, 'edges': edges}


def isolate_validation(graph, diagnostics):
    blocked = label_conflict_ids(diagnostics)
    observed = {n['id'] for n in graph.get('nodes', [])}
    if not blocked or not blocked <= observed:
        raise LightGraphError('graph_validation_required')
    clean = exclude_nodes(graph, blocked)
    if not clean['nodes']:
        raise LightGraphError('no_publishable_conflict_free_nodes')
    clean['conflict_isolation'] = {'policy': 'isolate_label_conflicts_v1',
        'node_ids': sorted(blocked), 'nodes': len(blocked),
        'edge_ids': sorted(e['id'] for e in graph.get('edges', [])
                           if e['source'] in blocked or e['target'] in blocked),
        'edges': len(graph.get('edges', [])) - len(clean['edges']),
        'candidate_retained': True, 'coverage': 'incomplete'}
    return clean


def isolate_publications(graphs):
    """Isolate shared label conflicts across all active contributors, without favouring old data.

    A quarantined identity cannot be reintroduced by another draft. Incompatible
    identities/types are still rejected by the normal merger, never quarantined.
    All unfiltered publication snapshots remain available in the history.
    """
    blocked = set()
    labels, identities = {}, {}
    for graph in graphs:
        blocked.update(graph.get('conflict_isolation', {}).get('node_ids', []))
        for node in graph.get('nodes', []):
            fixed = (node['type'], persisted_identity_key(node), node.get('identity_origin', 'row'))
            if node['id'] in identities and identities[node['id']] != fixed:
                raise LightGraphError('published_graph_identity_conflict')
            identities[node['id']] = fixed
            if node.get('identity_origin') == 'physical_column':
                continue
            supports = node.get('supports')
            supports = supports if supports is not None else [{'scope_ref': 'legacy', 'label': node.get('label')}]
            for support in supports:
                if support.get('label') is None:
                    continue
                key = (node['id'], support.get('scope_ref', 'legacy'))
                values = labels.setdefault(key, set())
                values.add(support['label'])
                if len(values) > 1:
                    blocked.add(node['id'])
    clean = [exclude_nodes(g, blocked) for g in graphs]
    removed_edges = {e['id'] for g in graphs for e in g.get('edges', [])
                     if e['source'] in blocked or e['target'] in blocked}
    removed_edges.update(e for g in graphs for e in g.get('conflict_isolation', {}).get('edge_ids', []))
    return clean, {'policy': 'isolate_label_conflicts_v1', 'node_ids': sorted(blocked),
        'edge_ids': sorted(removed_edges),
        'nodes': len(blocked), 'edges': len(removed_edges), 'candidate_retained': True,
        'coverage': 'incomplete' if blocked else 'complete'}


def conflict_details(graph, diagnostics, scopes):
    """Return observed names/keys only from the caller's authorized supports, offline."""
    from .access import filter_graph_scopes
    ids = {d.get('node_id') for d in diagnostics if d.get('code') == 'same_identity_conflicting_label'}
    relevant = {**graph, 'nodes': [n for n in graph.get('nodes', []) if n['id'] in ids], 'edges': []}
    visible = filter_graph_scopes(relevant, scopes)
    details = []
    for node in visible['nodes']:
        names = {}
        for support in node.get('supports', []):
            label = support.get('label')
            if label is None:
                continue
            origin = support.get('provenance', {})
            source = {'scope': support['scope_ref'], 'source': origin.get('source'),
                'mapping': origin.get('mapping'), 'key_columns': origin.get('key_columns', []),
                'label_column': origin.get('label_column')}
            entries = names.setdefault(label, [])
            if source not in entries:
                entries.append(source)
        details.append({'node_id': node['id'], 'entity_type': node['type'],
            'identity': deepcopy(node.get('identity', {})), 'identity_types': node.get('identity_types', {}),
            'names': [{'label': label, 'sources': sources} for label, sources in sorted(names.items())]})
    return {'basis': 'retained_validation_snapshot', 'requires_database': False,
            'nodes': details, 'count': len(details)}

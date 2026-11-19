"""Versioned descriptive statistics over a previously authorized graph view."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from lightgraph.contracts import digest
from ._semantica_algorithms import label_propagation, pagerank


ALGORITHM_VERSION = 'semantica_graph_analytics_v1'


def compute_graph_analytics(visible_graph: dict[str, Any]) -> dict[str, Any]:
    """Describe the visible topology. Communities are statistical groupings, not inferred edges.

    Each distinct directed pair carries unit weight, regardless of its number
    of relation types or evidence records. PageRank preserves direction and
    self-loops. Communities and normalized degree use distinct nonself neighbors.
    Callers own snapshot/permission selection and the persistence of this result.
    """
    raw_nodes = visible_graph.get('nodes', [])
    ids = sorted({node['id'] for node in raw_nodes if isinstance(node.get('id'), str) and node['id']})
    positions = {nid: i for i, nid in enumerate(ids)}
    outbound = [set() for _ in ids]
    adjacent = [set() for _ in ids]
    incoming = [0] * len(ids)
    edge_types = Counter()
    missing_endpoints = 0
    for edge in visible_graph.get('edges', []):
        source, target = positions.get(edge.get('source')), positions.get(edge.get('target'))
        if source is None or target is None:
            missing_endpoints += 1
            continue
        edge_types[edge.get('type') or edge.get('relation') or 'unknown'] += 1
        if target in outbound[source]:
            continue
        outbound[source].add(target)
        incoming[target] += 1
        if source != target:
            adjacent[source].add(target)
            adjacent[target].add(source)

    scores, scoring = pagerank([sorted(group) for group in outbound])
    labels, clustering = label_propagation([sorted(group) for group in adjacent])
    members = defaultdict(list)
    for nid, label in zip(ids, labels):
        members[label].append(nid)
    community_ids = {label: 'community_' + digest(group) for label, group in members.items()}

    nodes = [{'id': nid, 'community_id': community_ids[labels[i]], 'pagerank': scores[i],
              'in_degree': incoming[i], 'out_degree': len(outbound[i]),
              'degree_centrality': len(adjacent[i]) / max(len(ids) - 1, 1)}
             for i, nid in enumerate(ids)]
    nodes.sort(key=lambda n: (-n['pagerank'], n['id']))
    communities = sorted(({'id': community_ids[label], 'size': len(group)}
                          for label, group in members.items()), key=lambda c: (-c['size'], c['id']))
    skipped_nodes = len(raw_nodes) - len(ids)
    complete = not (missing_endpoints or skipped_nodes) and scoring['converged'] and clustering['converged']
    return {
        'version_id': visible_graph.get('version_id'), 'algorithm_version': ALGORITHM_VERSION,
        'status': 'completed' if complete else 'completed_with_gaps',
        'counts': {'nodes': len(ids), 'edges': sum(edge_types.values()),
                   'directed_pairs': sum(map(len, outbound)), 'communities': len(communities),
                   'isolated_nodes': sum(1 for i in range(len(ids)) if not outbound[i] and not incoming[i]),
                   'skipped_node_records': skipped_nodes, 'edges_missing_endpoints': missing_endpoints},
        'relation_counts': dict(sorted(edge_types.items())),
        'algorithms': {'community': clustering, 'pagerank': scoring,
                       'degree_centrality': {'method': 'distinct_nonself_neighbors_over_n_minus_one'}},
        'nodes': nodes, 'communities': communities,
    }

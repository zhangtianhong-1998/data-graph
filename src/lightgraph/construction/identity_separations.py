"""Carry explicit cannot-link decisions only while both published observations still match."""
from collections import defaultdict
from copy import deepcopy
from itertools import combinations

from lightgraph.contracts import digest
from .fusion import deduplicate_supports, definition_contract


def _support_signatures(item):
    # The next publication may have filled observation IDs and execution
    # origins. Compare the same normalized, complete observations on both sides.
    normalized = {**item, 'supports': deepcopy(item.get('supports', []))}
    deduplicate_supports(normalized)
    return sorted(digest(s) for s in normalized['supports'])


def _fingerprints(graph, pairs):
    members = {nid for pair in pairs for nid in pair}
    relations = {d['type']: definition_contract(d) for d in graph.get('definitions', {}).get('relations', [])}
    definitions = {d['type']: d for d in graph.get('definitions', {}).get('entities', [])}
    incident = defaultdict(list)
    for field in ('edges', 'unresolved_relations'):
        for edge in graph.get(field, []):
            ends = {edge.get('source'), edge.get('target'), *edge.get('node_ids', [])} & members
            if ends:
                witness = digest(field, edge['id'], edge.get('type'), edge.get('source'), edge.get('target'),
                                 edge.get('candidate_endpoints'), relations.get(edge.get('type')),
                                 _support_signatures(edge))
                for nid in ends:
                    incident[nid].append(witness)
    return {node['id']: digest(
        {k: node[k] for k in ('type', 'identity', 'identity_types', 'identity_domain',
                               'identity_context', 'identity_origin', 'node_role') if k in node},
        definitions.get(node['type']), _support_signatures(node),
        sorted(incident[node['id']]))
        for node in graph.get('nodes', []) if node['id'] in members}


def capture_separations(graph, pairs):
    if not pairs:
        return []
    signatures = _fingerprints(graph, pairs)
    return [{'members': list(pair), 'fingerprints': [signatures[n] for n in pair]} for pair in sorted(pairs)
            if all(n in signatures for n in pair)]


def retain_separations(graph, records):
    if not records:
        return
    prior = {(tuple(item['members']), tuple(item['fingerprints'])) for item in records}
    captured = capture_separations(graph, {pair for pair, _ in prior})
    kept = [row for row in captured if (tuple(row['members']), tuple(row['fingerprints'])) in prior]
    if kept:
        graph['identity_separations'] = kept
        separated = {tuple(row['members']) for row in kept}
        graph['candidate_identity_merges'] = [g for g in graph.get('candidate_identity_merges', [])
            if len(g.get('node_ids', [])) < 2 or any(pair not in separated
                for pair in combinations(sorted(set(g['node_ids'])), 2))]
    else:
        graph.pop('identity_separations', None)

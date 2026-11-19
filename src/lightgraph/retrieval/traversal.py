"""Find a compact chain of individually evidenced relationships in an authorized graph."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from lightgraph.construction.contexts import business_context_identity, normalize_context_selection
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from .path_evidence import FANOUT, MAX_EXPANSIONS, MAX_STATES, MAX_WITNESS_PAIRS, evidence_preview, path_node
from .retriever import _join_contexts


@dataclass
class _Chain:
    ids: tuple
    edges: tuple
    node_observations: tuple
    edge_observations: tuple
    context: dict
    via_count: int


def _context_witnesses(item):
    """One visible witness per distinct applicability suffices for this edge or node."""
    seen = set()
    for observation in item.get('observations', ()):
        context = business_context_identity(observation)
        key = canonical(context)
        if key not in seen:
            seen.add(key)
            yield context, observation


def retrieve_traversal(retriever, query, scopes, *, seed_ids, business_context=None, direction='both',
                       relation_types=(), start_id=None, target_id=None, target_kind=None, via_ids=(),
                       max_depth=12, max_nodes=80, max_edges=150, slots=()):
    """Visit every required anchor on one simple path, preserving each edge's own source.

    A traversal may cross distinct records of the same source. Their contexts
    must be compatible. Verifying a shared source tuple is a separate operation.
    """
    if (direction not in {'both', 'incoming', 'outgoing'} or not 1 <= max_depth <= 64
            or len(seed_ids) > 32 or len(via_ids) > 32 or max_nodes < 1 or max_edges < 0):
        raise LightGraphError('invalid_retrieval_options')
    selection = normalize_context_selection(business_context)
    cohorts = retriever.authorized_cohorts(scopes, selection)
    anchors = set(seed_ids)
    required = anchors | set(via_ids)
    if start_id:
        required.add(start_id)
    endpoints = required | ({target_id} if target_id else set())
    nodes = retriever.index.load_authorized_nodes(endpoints, scopes=cohorts)
    detail = {'paths': [], 'direction': direction, 'uncovered_seed_ids': sorted(anchors.intersection(nodes))}
    reasons = set()
    answer = {'query': query, 'scopes': sorted(set(scopes)), 'business_context': selection,
              'slots': list(slots), 'nodes': [], 'edges': [], 'bridge_nodes': [],
              'connected_components': [], 'truncated': False, 'truncation_reasons': [], 'traverse': detail}
    if not anchors or not endpoints.issubset(nodes):
        detail['status'] = 'no_visible_seed' if not anchors.intersection(nodes) else 'no_compatible_path'
        return answer
    if target_id and target_kind and nodes[target_id]['kind'] != target_kind:
        detail['status'] = 'no_compatible_path'
        return answer

    starts = [start_id] if start_id else sorted(anchors)
    agenda = deque()
    max_length = min(max_depth, max_nodes - 1, max_edges)
    has_destination = bool(target_kind or target_id or len(required) > 1)
    best = None
    pages = {}
    states = work = 0
    via_positions = {nid: i for i, nid in enumerate(via_ids)}

    def next_via(nid, position):
        if nid not in via_positions:
            return position
        return position + 1 if position == via_positions[nid] else None

    # The work budget includes attempted joins of incompatible observations.
    for nid in starts:
        position = next_via(nid, 0)
        if position is None:
            continue
        for context, observation in _context_witnesses(nodes[nid]):
            if states >= MAX_STATES:
                reasons.add('path_state_budget_reached')
                break
            agenda.append(_Chain((nid,), (), (observation,), (), context, position))
            states += 1

    while agenda:
        path = agenda.popleft()
        current = path.ids[-1]
        reaches_goal = (required.issubset(path.ids) and path.via_count == len(via_ids)
                        and (target_id is None or current == target_id)
                        and (target_kind is None or nodes[current]['kind'] == target_kind))
        if path.edges and reaches_goal:
            if has_destination:
                best = path
                break
            if best is None or len(path.edges) > len(best.edges):
                best = path
        if current == target_id and len(path.ids) > 1:
            continue
        if len(path.edges) >= max_length:
            reasons.add('depth_limit' if max_length == max_depth else 'output_limit')
            continue
        if states >= MAX_STATES or work >= MAX_WITNESS_PAIRS:
            reasons.add('path_search_limit')
            continue
        if current not in pages:
            if len(pages) >= MAX_EXPANSIONS:
                reasons.add('adjacency_budget_reached')
                continue
            page = retriever.index.expand_neighborhood(
                [current], scopes=cohorts, hops=1, direction=direction, relation_types=relation_types,
                max_nodes=FANOUT + 1, max_edges=FANOUT, max_per_rel=FANOUT, preferred_neighbor_ids=sorted(endpoints))
            pages[current] = page['edges']
            nodes.update(page['nodes'])
            reasons.update(page['truncation_reasons'])
        for edge in pages[current]:
            nxt = edge['target'] if edge['source'] == current else edge['source']
            position = next_via(nxt, path.via_count)
            if nxt in path.ids or position is None:
                continue
            if states >= MAX_STATES or work >= MAX_WITNESS_PAIRS:
                reasons.add('path_search_limit')
                break
            for edge_ctx, edge_obs in _context_witnesses(edge):
                work += 1
                if work >= MAX_WITNESS_PAIRS or states >= MAX_STATES:
                    reasons.add('path_search_limit')
                    break
                joined = _join_contexts(path.context, edge_ctx)
                if joined is None:
                    continue
                for node_ctx, node_obs in _context_witnesses(nodes[nxt]):
                    work += 1
                    if work >= MAX_WITNESS_PAIRS or states >= MAX_STATES:
                        reasons.add('path_search_limit')
                        break
                    context = _join_contexts(joined, node_ctx)
                    if context is not None:
                        agenda.append(_Chain((*path.ids, nxt), (*path.edges, edge),
                            (*path.node_observations, node_obs), (*path.edge_observations, edge_obs), context, position))
                        states += 1
    if best:
        reason = 'anchors_connected' if has_destination else 'path_selected'
        if target_id or target_kind:
            reason = 'target_reached'
        for nid, obs in zip(best.ids, best.node_observations):
            terms = [s['surface'] for s in slots if any(c['node_id'] == nid for c in s['candidates'])]
            answer['nodes'].append(path_node(nodes[nid], evidence_preview(obs), terms, nid in anchors))
        for edge, obs in zip(best.edges, best.edge_observations):
            answer['edges'].append({**{key: edge[key] for key in ('id', 'relation', 'source', 'target')},
                'observations': [evidence_preview(obs)], 'evidence_refs': obs.get('evidence_refs', []),
                'source_tables': obs.get('source_tables', [])})
        detail['paths'].append({'node_ids': list(best.ids), 'edge_ids': [e['id'] for e in best.edges],
            'business_context': best.context, 'stop_reason': reason, 'path_semantics': 'individually_supported_edges',
            'observed_path': False, 'context_status': 'unverified' if best.context['status'] == 'unknown' else 'compatible'})
        detail['uncovered_seed_ids'] = []
        answer['connected_components'] = [list(best.ids)]
    detail['status'] = 'found' if best else 'incomplete' if reasons else 'no_compatible_path'
    answer.update(truncated=bool(reasons), truncation_reasons=sorted(reasons))
    return answer

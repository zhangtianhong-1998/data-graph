"""Select one evidence-compatible directed path for a drilldown query."""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import re

from lightgraph.construction.contexts import normalize_context_selection
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from .retriever import _join_contexts, _join_rows, _observation_groups, _row_witnesses


# Search limits are separate from the size of the answer. A wide frontier or
# many incompatible row witnesses must not use an unbounded number of SQL calls.
_MAX_EXPANSIONS = 128
_MAX_WITNESS_PAIRS = 8192
_MAX_STATES = 2048
_FANOUT = 48


@dataclass
class _Path:
    ids: tuple
    edges: tuple
    node_witnesses: tuple
    edge_witnesses: tuple
    context: dict
    rows: dict
    terms: frozenset


def _terms(value):
    text = str(value or '').casefold()
    terms = set(re.findall(r'[a-z0-9_]+', text))
    for word in re.findall(r'[\u3400-\u9fff]+', text):
        terms.update(word[i:i + 2] for i in range(max(1, len(word) - 1)))
    return terms


def _matched(query_terms, witness, node):
    text = ' '.join(str(witness.get(key) or '') for key in ('label', 'description', 'english_name'))
    aliases = ' '.join(str(value) for value in witness.get('aliases') or ())
    keys = ' '.join(str(value) for value in node['identity'].values())
    return query_terms.intersection(_terms(' '.join([text, aliases, keys])))


def _evidence_preview(witness):
    """Large attribute maps and execution histories are available via exact-ID lookup."""
    if len(canonical(witness).encode('utf-8')) <= 2048:
        return witness
    refs = witness.get('evidence_refs') or []
    return {'projection': 'reference_only', 'observation_id': witness.get('observation_id'),
            'label': str(witness.get('label') or '')[:256], 'business_context': witness.get('business_context'),
            'evidence_refs': refs[:8], 'evidence_ref_count': len(refs),
            'source_tables': (witness.get('source_tables') or [])[:8]}


def _path_node(node, witness, slots, is_seed):
    # The index's display name/description may already aggregate several
    # authorized business contexts. Render the particular witness used here.
    return {'id': node['id'], 'kind': node['kind'], 'identity': node['identity'],
            'display_name': witness.get('label') or node['id'],
            'source_tables': witness.get('source_tables', []), 'description': witness.get('description'),
            'observations': [witness], 'is_seed': is_seed, 'matched_slots': slots, 'is_bridge': False}


def retrieve_drilldown(retriever, query, scopes, business_context=None, *, seed_ids=(), relation_types=(),
                       direction='outgoing', target_id=None, max_depth=12, max_nodes=80, max_edges=150,
                       field='all', method='hybrid', top_n=20, target_kind=None, hierarchies=None):
    """Find a compact directed chain, preferring target and anchor coverage.

    Bounds apply to explored states, adjacency reads, path length and returned
    records. For the same anchor coverage, a completed path beats a partial frontier.
    """
    if (direction not in {'outgoing', 'incoming'} or not 1 <= max_depth <= 64
            or max_nodes < 1 or max_edges < 0 or len(seed_ids) > 32):
        raise LightGraphError('invalid_retrieval_options')
    selection = normalize_context_selection(business_context)
    cohorts = retriever.authorized_cohorts(scopes, business_context)
    ids = list(dict.fromkeys(seed_ids))
    slots = []
    reasons = set()
    if not ids and query.strip():
        hits = retriever.retrieve(query, scopes=scopes, business_context=business_context,
                                  hops=0, top_p_seeds=1, max_nodes=32, field=field, method=method, top_n=top_n)
        slots = hits['slots']
        ids = [n['id'] for n in hits['nodes'] if n['is_seed']]
        reasons.update(hits['truncation_reasons'])
    seeds = retriever.index.load_authorized_nodes(ids, scopes=cohorts)
    seed_set = set(seeds)
    nodes = dict(seeds)
    query_terms = _terms(query)
    queue = []
    adjacency = {}
    best = None
    best_rank = None
    equally_ranked = set()
    stop = None
    states = pairs = 0
    limit = min(max_depth, max_nodes - 1, max_edges)
    answer = {'query': query, 'scopes': sorted(set(scopes)), 'business_context': selection, 'slots': slots,
              'nodes': [], 'edges': [], 'bridge_nodes': [], 'connected_components': []}
    details = {'paths': [], 'direction': 'parent_to_child' if hierarchies is not None else direction,
               'uncovered_seed_ids': sorted(seeds)}

    preferred = set(seeds)
    if target_id:
        preferred.add(target_id)
    elif seed_ids and query.strip():
        # A path hint can distinguish a sibling before the fanout budget is
        # applied. A lexical match never adds an anchor or licenses an edge.
        hints = retriever.index.search_lexical(query, scopes=cohorts, field='all', limit=8)
        preferred.update(hit['node_id'] for hit in hints if hit['match_type'] in {'exact_code', 'exact_name', 'exact_description'})

    def neighbors(nid, traversal):
        page = retriever.index.expand_neighborhood(
            [nid], scopes=cohorts, hops=1, max_nodes=_FANOUT + 1, max_edges=_FANOUT,
            max_per_rel=_FANOUT, relation_types=relation_types, direction=traversal,
            preferred_neighbor_ids=sorted(preferred), hierarchies=hierarchies)
        nodes.update(page['nodes'])
        return page

    traversal = direction if hierarchies is None else 'both'
    starts = seeds
    backwards = False
    if target_id and seeds and hierarchies is None:
        targets = retriever.index.load_authorized_nodes([target_id], scopes=cohorts)
        if not targets:
            starts = {}
        else:
            # In a hierarchy the requested leaf usually has one parent. Start
            # there to avoid exhausting the forward fanout at the root. If the
            # reverse frontier is itself clipped, keep the original direction.
            reverse = 'incoming' if direction == 'outgoing' else 'outgoing'
            page = neighbors(target_id, reverse)
            if not page['truncated']:
                traversal, starts, backwards = reverse, targets, True
                nodes.update(targets)
                adjacency[target_id] = (page['edges'], False)

    def consider(path, reason):
        nonlocal best, best_rank, stop
        if (target_id or target_kind) and reason not in {'target_reached', 'level_reached'}:
            return
        # Bounded shortest path among equally relevant, equally complete matches.
        score = (-len(seed_set.intersection(path.ids)), reason not in {'target_reached', 'level_reached', 'leaf'},
                 -len(path.terms), -len(_matched(query_terms, path.node_witnesses[-1], nodes[path.ids[-1]])),
                 len(path.edges))
        path_key = (path.ids, tuple(edge['id'] for edge in path.edges))
        rank = (score, path_key)
        if best_rank is None or score < best_rank[0]:
            equally_ranked.clear()
        if best_rank is None or score <= best_rank[0]:
            equally_ranked.add(path_key)
        if best_rank is None or rank < best_rank:
            best, best_rank, stop = path, rank, reason

    def enqueue(path):
        nonlocal states
        if backwards and path.ids[-1] in seed_set:
            proof = _Path(path.ids[::-1], path.edges[::-1], path.node_witnesses[::-1],
                          path.edge_witnesses[::-1], path.context, path.rows, path.terms)
            consider(proof, 'target_reached')
            if seed_set.issubset(path.ids):
                return
        elif target_kind and len(path.ids) > 1 and nodes[path.ids[-1]]['kind'] == target_kind:
            consider(path, 'level_reached')
            return
        elif not backwards and path.ids[-1] == target_id:
            consider(path, 'target_reached')
            return
        if states >= _MAX_STATES:
            reasons.add('path_state_budget_reached')
            consider(path, 'search_limit')
            return
        # Favor anchors and query terms. Without a specific target, explore
        # promising branches down to a leaf before spending the budget on peers.
        depth = len(path.edges) if target_id else -len(path.edges)
        priority = (-len(seed_set.intersection(path.ids)), -len(path.terms), depth, states)
        heapq.heappush(queue, (priority, path))
        states += 1

    for nid in sorted(starts):
        for context, observations in _observation_groups(starts[nid]):
            witness = max(observations, key=lambda obs: len(_matched(query_terms, obs, nodes[nid])))
            enqueue(_Path((nid,), (), (witness,), (), context, _row_witnesses([witness]),
                          frozenset(_matched(query_terms, witness, nodes[nid]))))
    while queue:
        _, path = heapq.heappop(queue)
        current = path.ids[-1]
        if current not in adjacency:
            if len(adjacency) >= _MAX_EXPANSIONS:
                reasons.add('adjacency_budget_reached')
                consider(path, 'search_limit')
                continue
            page = neighbors(current, traversal)
            reasons.update(page['truncation_reasons'])
            adjacency[current] = (page['edges'], page['truncated'])
        edges, capped = adjacency[current]
        extended = cycle = depth_limited = False
        for edge in edges:
            nid = edge['target'] if edge['source'] == current else edge['source']
            for edge_ctx, edge_obs in _observation_groups(edge):
                pairs += 1
                if pairs > _MAX_WITNESS_PAIRS:
                    break
                ctx = _join_contexts(path.context, edge_ctx)
                rows = _join_rows(path.rows, _row_witnesses(edge_obs))
                if ctx is None or rows is None:
                    continue
                for node_ctx, node_obs in _observation_groups(nodes[nid]):
                    pairs += 1
                    if pairs > _MAX_WITNESS_PAIRS:
                        break
                    joined = _join_contexts(ctx, node_ctx)
                    witnesses = _join_rows(rows, _row_witnesses(node_obs))
                    if joined is None or witnesses is None:
                        continue
                    if nid in path.ids:
                        cycle = True
                        continue
                    if len(path.edges) >= limit:
                        depth_limited = True
                        break
                    extended = True
                    witness = max(node_obs, key=lambda obs: len(_matched(query_terms, obs, nodes[nid])))
                    enqueue(_Path((*path.ids, nid), (*path.edges, edge), (*path.node_witnesses, witness),
                                  (*path.edge_witnesses, edge_obs[0]), joined, witnesses,
                                  path.terms.union(_matched(query_terms, witness, nodes[nid]))))
                if pairs > _MAX_WITNESS_PAIRS or depth_limited:
                    break
            if pairs > _MAX_WITNESS_PAIRS or depth_limited:
                break
        if pairs > _MAX_WITNESS_PAIRS:
            reasons.add('witness_budget_reached')
            consider(path, 'search_limit')
            for _, pending in queue:
                consider(pending, 'search_limit')
            break
        if depth_limited:
            reason = 'depth_limit' if limit == max_depth else 'output_limit'
            reasons.add(reason)
            consider(path, reason)
        elif not extended:
            reason = 'search_limit' if capped else 'cycle' if cycle else 'leaf'
            consider(path, reason)

    if best:
        for nid, witness in zip(best.ids, best.node_witnesses):
            matched = [s['surface'] for s in slots if any(c['node_id'] == nid for c in s['candidates'])]
            answer['nodes'].append(_path_node(nodes[nid], _evidence_preview(witness), matched, nid in seed_set))
        for edge, witness in zip(best.edges, best.edge_witnesses):
            witness = _evidence_preview(witness)
            answer['edges'].append({**{k: edge[k] for k in ('id', 'relation', 'source', 'target')},
                                    'observations': [witness], 'evidence_refs': witness.get('evidence_refs', []),
                                    'source_tables': witness.get('source_tables', [])})
        details['paths'].append({'node_ids': list(best.ids), 'edge_ids': [e['id'] for e in best.edges],
                                 'business_context': best.context, 'stop_reason': stop,
                                 'context_status': 'unverified' if best.context['status'] == 'unknown' else 'compatible',
                                 'path_semantics': 'condition_compatible_evidence_chain', 'observed_path': False})
        details['uncovered_seed_ids'] = sorted(seed_set.difference(best.ids))
        answer['connected_components'] = [list(best.ids)]
    details.update(status='found' if best else 'no_visible_seed' if not seeds else 'target_not_reached',
                   expanded_nodes=len(adjacency), equally_ranked_paths=len(equally_ranked))
    return {**answer, 'drilldown': details, 'truncated': bool(reasons), 'truncation_reasons': sorted(reasons)}

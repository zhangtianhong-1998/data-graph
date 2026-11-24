"""Answer one strict hierarchy step, keeping the contributing published assertions separate."""
from collections import defaultdict
import re

from lightgraph.construction.contexts import business_context_identity, normalize_context_selection
from lightgraph.contracts import canonical
from .dimensional import empty_answer
from .intent import _new_plan, _resolve_text
from .path_evidence import MAX_WITNESS_PAIRS, evidence_preview, path_node
from .retriever import _join_contexts
from .traversal import _context_witnesses


_QUESTION = re.compile(r'(?:请问\s*)?(.+?)\s*的\s*直接(上|下)级(?:科目|节点)?(?:有哪(?:些|几个)|是什么|是谁)?[？?。]?')


def hierarchy_question(text):
    match = _QUESTION.fullmatch(text.strip())
    if match:
        return match[1].strip(), 'children' if match[2] == '下' else 'parents'
    return None


def _observed_identity(node, witness):
    if not witness.get('identity_decision_refs'):
        return node['identity']
    keys = (witness.get('provenance') or {}).get('key_columns') or []
    values = (witness.get('source_identity') or {}).get('values') or []
    return dict(zip(keys, values)) if len(keys) == len(values) else {}


def _compatible_endpoints(seeds, edge_context, neighbors, remaining):
    for seed_context, seed in seeds:
        remaining -= 1
        if remaining < 0:
            break
        joined = _join_contexts(seed_context, edge_context)
        if joined is None:
            continue
        for neighbor_context, neighbor in neighbors:
            remaining -= 1
            if remaining < 0:
                return None, remaining
            if _join_contexts(joined, neighbor_context) is not None:
                return (seed, neighbor), remaining
    return None, remaining


def retrieve_hierarchy(retriever, request, scopes, *, term, direction):
    selection = normalize_context_selection(request.business_context)
    cohorts = retriever.authorized_cohorts(scopes, selection)
    candidates, capped = [], False
    if request.node_id:
        candidates = [{'node_id': n['id'], 'kind': n['kind']} for n in
                      retriever.index.load_authorized_nodes([request.node_id], scopes=cohorts).values()]
    elif term:
        candidates, capped = _resolve_text(retriever.index, term, cohorts)
    slot = {'text': term or request.node_id, 'match': 'node_id' if request.node_id else 'exact',
            'role': None, 'candidates': candidates}
    plan = _new_plan(selection, request.query, slot['match'])
    plan.update(intent='hierarchy', conditions=[slot], candidate_limit=capped)
    answer = empty_answer(request.query, scopes, plan)
    detail = {'status': 'no_visible_seed', 'direction': direction, 'anchor_id': None, 'items': []}
    answer['hierarchy'] = detail
    if len(candidates) > 1:
        visible = retriever.index.load_authorized_nodes([c['node_id'] for c in candidates], scopes=cohorts)
        for c in candidates:
            node = visible[c['node_id']]
            witness = next((obs for _, obs in _context_witnesses(node, [slot])), None)
            if witness is not None:
                c['display_name'] = witness.get('label') or term
                c['identity'] = _observed_identity(node, witness)
        detail['status'] = 'ambiguous'
        plan.update(intent='clarify', unresolved=['有多个同名对象，请用完整编码或节点编号指定。'])
    if len(candidates) != 1:
        if capped:
            answer.update(truncated=True, truncation_reasons=['candidate_limit_reached'])
        return answer

    anchor = candidates[0]['node_id']
    detail['anchor_id'] = anchor
    declared, mapped = retriever.index.hierarchy_directions(cohorts, anchor=anchor)
    if direction == 'parents':
        def reverse(directions):
            return {key: 'incoming' if flow == 'outgoing' else 'outgoing' for key, flow in directions.items()}
        declared, mapped = reverse(declared), reverse(mapped)
    page = retriever.index.expand_neighborhood([anchor], scopes=cohorts, hops=1, hierarchies=declared,
        mapped_hierarchies=mapped, max_nodes=request.max_nodes, max_edges=request.max_edges,
        max_per_rel=request.max_edges)
    nodes = page['nodes']
    seed_witnesses = list(_context_witnesses(nodes[anchor], [slot]))
    if not seed_witnesses:
        return answer
    proven_nodes = {}
    cited_contexts = set()

    def cite_node(nid, witness):
        key = nid, canonical(business_context_identity(witness))
        if key in cited_contexts:
            return
        cited_contexts.add(key)
        preview = evidence_preview(witness)
        if nid in proven_nodes:
            proven_nodes[nid]['observations'].append(preview)
        else:
            is_seed = nid == anchor
            node = path_node(nodes[nid], preview, [slot['text']] if is_seed else [], is_seed)
            node['identity'] = _observed_identity(nodes[nid], witness)
            proven_nodes[nid] = node

    grouped = defaultdict(list)
    reasons = set(page['truncation_reasons'])
    # Multiple source assertions can establish the same answer independently.
    # A name on one business context cannot establish a different context's edge.
    remaining = MAX_WITNESS_PAIRS
    for edge in page['edges']:
        other = edge['target'] if edge['source'] == anchor else edge['source']
        witnesses = list(_context_witnesses(nodes[other]))
        observations = defaultdict(list)
        for obs in edge['observations']:
            observations[canonical(business_context_identity(obs))].append(obs)
        supported = []
        for group in observations.values():
            match, remaining = _compatible_endpoints(seed_witnesses, business_context_identity(group[0]),
                                                      witnesses, remaining)
            if remaining < 0:
                reasons.add('witness_budget_reached')
                break
            if match is not None:
                supported.extend(group)
                cite_node(anchor, match[0])
                cite_node(other, match[1])
        if supported:
            grouped[other].append(edge['id'])
            answer['edges'].append({**{k: edge[k] for k in ('id', 'relation', 'source', 'target')},
                'observations': supported,
                'evidence_refs': sorted({r for obs in supported for r in obs['evidence_refs']}),
                'source_tables': sorted({t for obs in supported for t in obs['source_tables']})})
        if remaining < 0:
            break
    if not proven_nodes:
        cite_node(anchor, seed_witnesses[0][1])
    answer['nodes'] = list(proven_nodes.values())
    detail['items'] = [{'node_id': nid, 'edge_ids': ids} for nid, ids in sorted(grouped.items())]
    detail['status'] = 'incomplete' if reasons else 'found' if grouped else 'empty'
    answer.update(connected_components=[[anchor, *sorted(grouped)]], truncated=bool(reasons),
                  truncation_reasons=sorted(reasons))
    return answer

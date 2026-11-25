"""Answer one strict hierarchy step, keeping the contributing published assertions separate."""
from collections import defaultdict
import re

from lightgraph.construction.contexts import business_context_identity, normalize_context_selection
from lightgraph.contracts import canonical
from .dimensional import empty_answer
from .intent import _new_plan, _resolve_scope, _resolve_text
from .path_evidence import MAX_WITNESS_PAIRS, evidence_preview, grounds_slot, path_node
from .retriever import _join_contexts
from .traversal import _context_witnesses


_QUESTION = re.compile(r'(?:请问\s*)?(.+?)\s*的\s*直接(上|下)级([^，。？?]*?)(?:有哪(?:些|几个)|是什么|是谁)?[？?。]?')


def hierarchy_question(text):
    match = _QUESTION.fullmatch(text.strip())
    if match:
        return match[1].strip(), 'children' if match[2] == '下' else 'parents', match[3].strip()
    return None


def _observed_identity(node, witness):
    if not witness.get('identity_decision_refs'):
        return node['identity']
    keys = (witness.get('provenance') or {}).get('key_columns') or []
    values = (witness.get('source_identity') or {}).get('values') or []
    return dict(zip(keys, values)) if len(keys) == len(values) else {}


def _describe_candidate(candidate, node, slot):
    """Describe the observations that matched this term within the caller's scope."""
    bindings = set()
    for obs in node.get('observations', []):
        if not grounds_slot(node, obs, slot):
            continue
        if 'identity' not in candidate:
            candidate['identity'] = _observed_identity(node, obs)
        if obs.get('label') and not candidate.get('display_name'):
            candidate['display_name'] = obs['label']
        prov = obs.get('provenance') or {}
        source = prov.get('source') or {}
        address = tuple(source.get(k) for k in ('source_id', 'schema', 'table'))
        keys = prov.get('key_columns')
        if (all(isinstance(part, str) and part for part in address) and isinstance(keys, list)
                and keys and all(isinstance(key, str) and key for key in keys)):
            bindings.add((*address, tuple(keys)))
    if bindings:
        candidate['source_bindings'] = [
            {'source_id': sid, 'schema': schema, 'table': table, 'key_columns': list(keys)}
            for sid, schema, table, keys in sorted(bindings)[:5]]
        candidate['source_bindings_truncated'] = len(bindings) > 5


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


def retrieve_hierarchy(retriever, request, scopes, *, term, direction, requested_kind=None):
    selection = normalize_context_selection(request.business_context)
    issues = []
    if request.mode == 'auto':
        selection, remainder, issues = _resolve_scope(retriever, scopes, selection, [term], [])
        if remainder != term:
            visible = retriever.authorized_cohorts(scopes, selection)
            anchor = remainder.strip()
            # Prefer a literal object whose name starts with 中/在. Only remove
            # scope grammar if the full remaining phrase has no visible match.
            if anchor and not _resolve_text(retriever.index, anchor, visible)[0]:
                anchor = re.sub(r'^\s*(?:在\s+)?[中里的内]?[，,\s]*', '', anchor).strip()
            term = anchor or term
    cohorts = retriever.authorized_cohorts(scopes, selection)
    kind = None
    if requested_kind and requested_kind != '节点':
        kinds = [k for k in retriever.index.authorized_kinds(cohorts) if k.casefold() == requested_kind.casefold()]
        if len(kinds) == 1:
            kind = kinds[0]
        else:
            issues.append('请指定图中可见的目标类型：“' + requested_kind + '”。')
    if issues:
        plan = _new_plan(selection, request.query, 'hierarchy')
        plan.update(intent='clarify', unresolved=issues)
        return empty_answer(request.query, scopes, plan)
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
            _describe_candidate(c, visible[c['node_id']], slot)
        detail['status'] = 'ambiguous'
        plan.update(intent='clarify', unresolved=['匹配到多个对象。请核对类型、业务标识和来源后选择查询起点。'])
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
    if kind:
        def targets(directions):
            return {k: flow for k, flow in directions.items() if k[2 if flow == 'outgoing' else 0] == kind}
        declared, mapped = targets(declared), targets(mapped)
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
                'observations': [evidence_preview(obs) for obs in supported],
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

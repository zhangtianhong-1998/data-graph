"""Connect exact operands using only visible published relationships."""
from collections import defaultdict, deque
from itertools import zip_longest

from .dimensional import empty_answer
from .path_evidence import MAX_STATES, PathCollection
from .store_sqlite import normalize_term
from .traversal import retrieve_traversal


def _candidate_chains(links, max_depth):
    """Prefer compact chains covering distinct operands; retain shorter alternatives."""
    neighbors = defaultdict(set)
    roles = {}
    for row in links:
        left, right = (row['lt'], row['src']), (row['rt'], row['dst'])
        neighbors[left].add(right)
        neighbors[right].add(left)
        roles[left], roles[right] = row['sk'], row['dk']
    agenda = deque((start,) for start in sorted(neighbors))
    candidates = set()
    states = len(agenda)
    while agenda and states < MAX_STATES:
        path = agenda.popleft()
        if len(path) > 1:
            candidates.add(min(path, tuple(reversed(path))))
        if len(path) - 1 >= max_depth:
            continue
        terms = {term for term, _ in path}
        ids = {nid for _, nid in path}
        for term, nid in sorted(neighbors[path[-1]]):
            if term not in terms and nid not in ids:
                agenda.append((*path, (term, nid)))
                states += 1
                if states >= MAX_STATES:
                    break
    # At the budget boundary, already discovered chains remain eligible.
    for path in agenda:
        if len(path) > 1:
            candidates.add(min(path, tuple(reversed(path))))
    # Siblings with the same role can be numerous. Explore each distinct
    # interpretation before another path with the same sequence of types.
    by_length = defaultdict(lambda: defaultdict(list))
    for path in sorted(candidates):
        signature = tuple((term, roles[(term, nid)]) for term, nid in path)
        by_length[len(path)][signature].append(path)
    ranked = []
    for length in sorted(by_length, reverse=True):
        for batch in zip_longest(*by_length[length].values()):
            ranked.extend(path for path in batch if path)
    return ranked, bool(agenda)


def retrieve_associations(retriever, request, scopes, plan, truncation):
    """Provide alternative relationship paths without claiming the operands share a fact row."""
    slots = plan['conditions']
    if (len(slots) < 2 or plan.get('point_only') or plan['unbound_mentions']
            or plan.get('unverified_predicates') or plan.get('excluded_terms')
            or any(s.get('match') != 'exact' for s in slots)):
        return None
    surface = {normalize_term(s['text']): s['text'] for s in slots}
    cohorts = retriever.authorized_cohorts(scopes, plan['business_context'])
    link_limit = 96
    proof_limit = 16
    links = retriever.index.exact_association_links(surface, scopes=cohorts, limit=link_limit + 1, truncation=truncation)
    if not links:
        return None
    paths, more_states = _candidate_chains(links[:link_limit], request.max_depth)
    result = PathCollection(request)
    if more_states or len(links) > link_limit:
        result.truncation.add('association_candidate_limit')
    choices = []
    covered_subpaths = set()
    attempts = 0
    full_path_attempts = proof_limit - min(4, sum(len(p) == 2 for p in paths))
    for path in paths:
        if path in covered_subpaths:
            continue
        if attempts >= proof_limit:
            result.truncation.add('association_candidate_limit')
            break
        if len(path) > 2 and attempts >= full_path_attempts:
            result.truncation.add('association_candidate_limit')
            continue
        attempts += 1
        ids = [nid for _, nid in path]
        proof = retrieve_traversal(retriever, request.query, scopes, seed_ids=ids, start_id=ids[0],
            via_ids=ids[1:-1], target_id=ids[-1], route_node_ids=ids, max_depth=len(ids) - 1,
            slots=[{'surface':surface[term], 'candidates':[{'node_id':nid}]} for term, nid in path],
            max_nodes=request.max_nodes, max_edges=request.max_edges, business_context=plan['business_context'])
        evidence = proof['traverse']['paths']
        if not evidence:
            result.truncation.update(proof['truncation_reasons'])
            continue
        nodes = {n['id']: n for n in proof['nodes']}
        for term, nid in path:
            nodes[nid]['matched_slots'] = [surface[term]]
        if not result.add(proof):
            continue
        matched = [surface[term] for term, _ in path]
        choices.append({**evidence[0], 'matched_terms': matched})
        for start in range(len(path) - 1):
            for end in range(start + 2, len(path) + 1):
                part = path[start:end]
                covered_subpaths.add(min(part, tuple(reversed(part))))
    if not choices:
        truncation.update(result.truncation)
        return None
    unresolved = list(plan['unresolved'])
    if plan['candidate_limit'] or any(len(s['candidates']) > 1 for s in slots):
        unresolved.append('问句中有同名对象，请结合返回路径确认所指对象。')
    answer = empty_answer(request.query, scopes, {**plan, 'strategy': 'association_paths', 'unresolved': unresolved})
    answer.update(nodes=list(result.nodes.values()), edges=list(result.edges.values()), association_paths=choices,
                  connected_components=[sorted(c) for c in result.components], truncated=bool(result.truncation),
                  truncation_reasons=sorted(result.truncation))
    return answer

"""Ground a requested grouping in published, scoped relationship paths."""
from __future__ import annotations

import re

from .dimensional import empty_answer
from .intent import _ACTION, _EDGE_DIRECTIONS, _NEG, _PATH_OPERATION, _PREDICATE
from .retriever import is_literal_span
from .path_evidence import PathCollection, evidence_preview, grounds_slot, path_node, witness_names, matches_operand
from .semantic_grounding import link_mentions
from .store_sqlite import normalize_term
from .traversal import retrieve_traversal


# Only grammatical operators live here. Category and subject names come from
# the question. The model can also supply both spans without these templates.
_PREFIX = re.compile(r'(?P<operator>按|分各个|分|各个|各)(?P<axis>[^，。？?、的]{1,60}?)(?:的|统计|汇总)')
_SUFFIX = re.compile(r'(?P<subject>.+?)\s*(?:按(?P<cn>[^，。？?、]{1,60}?)(?:分组|分类|汇总)|'
                     r'\s+(?:grouped by|broken down by)\s+(?P<en>[\w -]{1,60}))', re.I)
_DISTRIBUTION = re.compile(r'(?:给我看看|请展示)?(?P<subject>[^，。？?、]+?)在不同(?P<axis>[^，。？?、]{1,60}?)的分布')
_VALUE_REQUEST = re.compile(r'(?:各(?:自)?|分别)?(?:是多少|有多少|为多少)$')


def grouping_spans(focus, slots):
    """Keep the subject, grouping phrase and any additional filter text separate."""
    focus = focus.strip().rstrip('？?。').strip()
    if not focus or any(c in focus for c in '，。！？?；;\n'):
        return None
    # A request such as "the size of each ..." can still name a graph object
    # that contains an operator as ordinary text. Preserve recognized names.
    protected = [m.span() for slot in slots for m in re.finditer(re.escape(slot['text']), focus, re.I)]
    distribution = _DISTRIBUTION.fullmatch(focus)
    if distribution and not any(left <= distribution.end('subject') < right for left, right in protected):
        return distribution['subject'], distribution['axis'], []
    suffix = _SUFFIX.fullmatch(focus)
    if suffix:
        start = suffix.end('subject')
        if not any(left <= start < right for left, right in protected):
            return suffix['subject'].strip(), (suffix['cn'] or suffix['en']).strip(), []
    for op in re.finditer(r'按|分各个|分|各个|各', focus):
        if any(start <= op.start() < end for start, end in protected):
            continue
        match = _PREFIX.match(focus, op.start())
        if match:
            remainder = focus[match.end():]
            subject = _VALUE_REQUEST.sub('', remainder).strip()
            axis = match['axis'].strip()
            if subject and axis and not _VALUE_REQUEST.fullmatch(axis):
                prefix = focus[:op.start()].strip()
                if match['operator'] == '分' and match.group().endswith('的'):
                    # Bare 分 also occurs inside nouns. Require independent
                    # syntax or a grounded preceding operand at this boundary.
                    follows_operand = any(end == op.start() for _, end in protected)
                    if len(axis) < 2 or (prefix and not follows_operand):
                        continue
                return subject, axis, [prefix] if prefix else []
    return None


def _contains_axis(names, axis):
    word = normalize_term(axis)
    return any(is_literal_span(name, *match.span()) for name in names
               for match in re.finditer(re.escape(word), name))


def retrieve_grouping(retriever, request, scopes, plan):
    """Show evidenced associations as choices; never infer aggregates or row co-occurrence."""
    slots = plan['conditions']
    grammar = grouping_spans(plan['focus'], slots)
    spans = plan.get('grouping_spans') or grammar
    if not spans or plan.get('scope_unresolved') or plan.get('excluded_terms'):
        return None
    subject, axis, filters = spans
    # A grouped path cannot discharge a requested predicate, hierarchy,
    # direction or traversal. Preserve those operations' normal planners.
    syntax = plan['focus']
    for literal in sorted({subject, axis}, key=len, reverse=True):
        syntax = syntax.replace(literal, ' ')
    if (plan.get('operation') == 'traverse' or plan.get('relation_types') or plan.get('drill_role')
            or _NEG.search(syntax) or _PATH_OPERATION.search(syntax)
            or _ACTION.search(syntax) or any(pattern.search(syntax) for pattern in _EDGE_DIRECTIONS.values())
            or _PREDICATE.search(axis)):
        return None
    # When the complete question is accounted for independently and both
    # parsers agree on the operands, grammatical wording such as "group by"
    # cannot introduce an additional restriction. Keep model-declared filters.
    if grammar != (subject, axis, []):
        filters = list(dict.fromkeys([*filters, *plan.get('unverified_predicates', [])]))
    bound = next((s for s in slots if normalize_term(s['text']) == normalize_term(subject)), None)
    if not bound or not bound['candidates'] or len(axis) > 120:
        return None
    seeds = [c['node_id'] for c in bound['candidates']]
    cohorts = retriever.authorized_cohorts(scopes, plan['business_context'])
    # Filter by adjacency before limiting candidates: the exact label may
    # belong to a separate catalog while nearby axes use more specific labels.
    collection = PathCollection(request)
    reasons = collection.truncation
    limit = 16
    search = dict(scopes=cohorts, limit=limit + 1, max_depth=request.max_depth,
                  subject_term=subject if bound.get('match') == 'exact' else None, truncation=reasons)
    routes = retriever.index.grouping_routes(seeds, axis, **search)
    if not routes and not reasons:
        suggestions, = link_mentions(retriever, [axis], cohorts,
                                     paraphrases={axis: plan.get('grouping_names', [])})
        related = [item['node_id'] for item in suggestions['candidates']]
        if related:
            routes = retriever.index.grouping_routes(seeds, axis, suggested_ids=related, **search)
    choices = []
    if len(routes) > limit:
        reasons.add('grouping_candidate_limit')
    for route in routes[:limit]:
        seed, target, bridge = route['seed'], route['target'], route['bridge']
        chain = [seed, *([bridge] if bridge else []), target]
        proof = retrieve_traversal(retriever, request.query, scopes, seed_ids=[seed], start_id=seed,
            business_context=plan['business_context'], via_ids=chain[1:-1], target_id=target,
            max_depth=min(2, request.max_depth), max_nodes=request.max_nodes, max_edges=request.max_edges,
            route_node_ids=chain, slots=[{'surface': subject, 'source': bound.get('match', 'exact'),
                'candidates': [c for c in bound['candidates'] if c['node_id'] == seed]}])
        paths = proof['traverse']['paths']
        if not paths:
            reasons.update(proof['truncation_reasons'])
            continue
        # Preserve complete alternatives under the caller's total output budget.
        route_nodes = {n['id']: n for n in proof['nodes']}
        target_node = route_nodes[target]
        axis_names = [normalize_term(target_node['kind'])] if route['rank'] == 2 else witness_names(target_node)
        if route['rank'] < 3 and not _contains_axis(axis_names, axis):
            continue
        if bound.get('match') == 'exact' and not matches_operand(route_nodes[seed], normalize_term(subject)):
            continue
        if not collection.add(proof):
            continue
        choices.append({**paths[0], 'subject_id': seed, 'target_id': target,
                        'match': ('published_name', 'published_name', 'published_type', 'semantic_candidate')[route['rank']]})
    # A weak bare prefix can be the first syllable of a noun. If nothing in
    # the visible graph supports that reading, keep the original interpretation.
    if (not choices and not reasons and not plan.get('grouping_spans') and plan['focus'].startswith('分' + axis + '的')
            and not _VALUE_REQUEST.search(plan['focus']) and not any(s['text'] == axis for s in slots)):
        return None
    # Unbound terms from the original planner may include the grouping syntax
    # itself. Preserve every residual filter verbatim, even when it names a node.
    message = f'请确认“{axis}”采用哪种分组口径。以下是图中已有的关联，分组适用性仍待核实。'
    if choices and len(seeds) > 1:
        message = f'“{subject}”对应多个对象。请根据下列路径确认所指对象和“{axis}”的分组口径。'
    if not choices:
        message = f'识别到分组条件“{axis}”，在当前范围和查询上限内没有核实相应路径。'
    issues = [message, *[f'尚未核实限定条件“{text}”。' for text in filters]]
    filter_slots = [slot for slot in slots if slot is not bound and any(slot['text'] in text for text in filters)]
    plan = {**plan, 'intent': 'clarify', 'strategy': 'grouping_paths', 'unresolved': issues,
            'conditions': [bound, *filter_slots], 'unbound_mentions': [{'text': text, 'reason': 'unverified_condition'} for text in filters],
            'suggestions': [], 'unverified_predicates': list(filters)}
    answer = empty_answer(request.query, scopes, plan)
    detail = {'subject': subject, 'axis': axis, 'paths': choices, 'filters': filters,
              'searched_depth': min(2, request.max_depth), 'value_status': 'not_computed', 'grouping_applicability': 'unverified', 'path_semantics': 'published_associations'}
    visible_nodes, visible_edges = list(collection.nodes.values()), list(collection.edges.values())
    answer.update(grouping=detail, truncated=bool(reasons), truncation_reasons=sorted(reasons))
    if filters or not choices:
        # A region or other independent filter can be a confirmed object even
        # when no visible record jointly supports it and the grouping path.
        partial_slots = ([bound] if len(seeds) == 1 else []) + [
            slot for slot in filter_slots if len(slot['candidates']) == 1]
        ids = [slot['candidates'][0]['node_id'] for slot in partial_slots]
        partial_nodes = retriever.index.load_authorized_nodes(ids, scopes=cohorts)
        retained = set(collection.nodes)
        for slot, nid in zip(partial_slots, ids):
            if nid not in retained and nid in partial_nodes:
                if len(visible_nodes) >= request.max_nodes:
                    reasons.add('output_limit')
                    break
                node = partial_nodes[nid]
                witness = next((obs for obs in node['observations'] if grounds_slot(node, obs, slot)), None)
                if witness is not None:
                    visible_nodes.append(path_node(node, evidence_preview(witness), [slot['text']], True))
                    retained.add(nid)
        answer.update(truncated=bool(reasons), truncation_reasons=sorted(reasons))
        answer['available_context'] = {'status': 'partial', 'nodes': visible_nodes, 'edges': visible_edges,
                                       'witnesses': [], 'passages': [], 'truncated': bool(reasons)}
    else:
        answer.update(nodes=visible_nodes, edges=visible_edges, connected_components=[sorted(c) for c in collection.components])
    return answer

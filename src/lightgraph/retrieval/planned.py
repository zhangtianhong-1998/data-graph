"""Compose scoped retrieval primitives according to the grounded user request."""
from .dimensional import empty_answer, retrieve_dimensional
from .grouping import retrieve_grouping
from .drilldown import retrieve_drilldown
from .traversal import retrieve_traversal
from .intent import build_plan, _parent_roles, _residuals


def _candidate_path(retriever, request, scopes, cohorts, plan, context):
    """Verify one conditional interpretation against the same rows as independent anchors."""
    bound = plan['conditions']
    gaps = plan['unbound_mentions']
    if (not bound or not gaps or plan['candidate_limit'] or plan.get('unverified_predicates') or context.get('truncated')
            or any(len(s['candidates']) != 1 for s in bound)
            or any(gap['reason'] != 'related' for gap in gaps)):
        return None
    proposed = {item['text']: item for item in plan['suggestions'] if item.get('reason') == 'related'}
    slots = list(bound)
    assumptions = []
    for gap in gaps:
        options = proposed.get(gap['text'], {}).get('candidates', [])
        # Competing names or types remain suggestions. Graph topology cannot
        # adjudicate which interpretation of the user's words is correct.
        if len(options) != 1 or options[0].get('evidence') != 'vector_neighbor':
            return None
        candidate = options[0]
        assumptions.append({'text': gap['text'], 'node_id': candidate['node_id'],
                            'display_name': candidate['display_name'], 'kind': candidate['kind']})
        slots.append({'text': gap['text'], 'role': candidate['kind'], 'candidates': [candidate]})
    if len({s['role'] for s in slots}) != len(slots):
        return None

    level = plan['requested_level']
    if plan['parser'] == 'index' and _residuals(plan['focus'], slots, level):
        return None
    role = plan['drill_role']
    if level:
        parents = _parent_roles(retriever.index, cohorts, slots, level)
        if len(parents) != 1 or (role and role not in parents):
            return None
        role = next(iter(parents))
    provisional = {**plan, 'conditions': slots, 'drill_role': role}
    evidence = retrieve_dimensional(retriever, request.query, scopes, provisional,
        max_depth=request.max_depth, max_nodes=request.max_nodes, max_edges=request.max_edges,
        direction=request.direction, relation_types=request.relation_types)
    detail = evidence['dimensional']
    if (detail['status'] != 'matched' or evidence['truncated'] or detail['multiple_bindings']
            or (level and not detail['level_node_ids'])
            or any(w['business_context']['status'] == 'unknown' for w in detail['witnesses'])):
        return None
    for collection, limit in [('nodes', request.max_nodes), ('edges', request.max_edges)]:
        if len({row['id'] for row in context[collection] + evidence[collection]}) > limit:
            return None
    return {'assumptions': assumptions, 'unverified_terms': [g['text'] for g in gaps],
            'nodes': evidence['nodes'], 'edges': evidence['edges'], 'witnesses': detail['witnesses'],
            'requested_level': level, 'level_node_ids': detail['level_node_ids']}


def _available_context(retriever, request, scopes, plan, answer=None):
    """Report what can be cited without claiming the complete question was answered."""
    answer = answer if answer is not None else empty_answer(request.query, scopes, plan)
    context = {'status': 'no_confirmed_match', 'nodes': [], 'edges': [], 'passages': [], 'witnesses': []}
    answer['available_context'] = context
    if plan.get('scope_unresolved'):
        context['status'] = 'scope_unresolved'
        return answer
    cohorts = retriever.authorized_cohorts(scopes, plan['business_context'])
    bound = [slot for slot in plan['conditions'] if len(slot['candidates']) == 1]
    ids = [slot['candidates'][0]['node_id'] for slot in bound][:request.max_nodes]
    labels = retriever.index.authorized_labels(ids, scopes=cohorts)
    context['nodes'] = [{'id': nid, 'display_name': label} for nid, label in labels.items()]
    # A verified intersection of a subset has its own witnesses. Keep its
    # graph outside the main nodes/edges even when it fits all output budgets.
    co_observation = not (plan.get('operation') == 'traverse' or request.mode == 'traverse')
    if co_observation and len({s['role'] for s in bound}) > 1 and not answer.get('dimensional'):
        subset = {**plan, 'conditions': bound, 'requested_level': None, 'drill_role': None}
        verified = retrieve_dimensional(retriever, request.query, scopes, subset,
                                        max_nodes=request.max_nodes, max_edges=request.max_edges)
        if verified['dimensional']['status'] == 'matched':
            context.update(nodes=verified['nodes'], edges=verified['edges'],
                           witnesses=verified['dimensional']['witnesses'], truncated=verified['truncated'])
    gaps = plan['unbound_mentions']
    context['passages'] = retriever.index.source_passages(
        [i['text'] for i in gaps], scopes=cohorts, limit=min(8, request.max_nodes))
    if context['nodes']:
        context['status'] = 'partial'
        candidate = _candidate_path(retriever, request, scopes, cohorts, plan, context) if co_observation else None
        if candidate:
            context['candidate_path'] = candidate
    elif context['passages']:
        context['status'] = 'source_text_only'
    elif any(i['reason'] == 'unavailable' for i in gaps):
        context['status'] = 'search_unavailable'
    return answer


def _planned_traversal(retriever, request, scopes, plan):
    query = request.query
    limits = {'max_nodes': request.max_nodes, 'max_edges': request.max_edges}
    slots = plan['conditions']
    if (not slots or plan['candidate_limit'] or any(len(s['candidates']) != 1 for s in slots)):
        plan.update(intent='clarify')
        plan['unresolved'].append('请明确路径要经过的节点。')
        return _available_context(retriever, request, scopes, plan)
    filters = set(plan.get('relation_types') or ())
    if filters and request.relation_types:
        filters &= set(request.relation_types)
        if not filters:
            plan.update(intent='clarify')
            plan['unresolved'].append('指定的关系类型与问题中的路径要求没有交集。')
            return _available_context(retriever, request, scopes, plan)
    else:
        filters.update(request.relation_types)
    direction = plan.get('direction') or 'both'
    if request.mode == 'traverse' and request.direction != 'both':
        if direction not in {'both', request.direction}:
            plan.update(intent='clarify')
            plan['unresolved'].append('指定的方向与问题中的路径要求不一致。')
            return _available_context(retriever, request, scopes, plan)
        direction = request.direction
    targets = ((request.target_id, plan.get('target_id')), (request.target_kind, plan.get('requested_level')))
    if any(explicit and parsed and explicit != parsed for explicit, parsed in targets):
        plan.update(intent='clarify')
        plan['unresolved'].append('指定的终点与问题中的路径要求不一致。')
        return _available_context(retriever, request, scopes, plan)
    answer = retrieve_traversal(retriever, query, scopes, business_context=plan['business_context'],
        seed_ids=[s['candidates'][0]['node_id'] for s in slots], start_id=plan.get('start_id'),
        via_ids=plan.get('via_ids', ()), target_kind=request.target_kind or plan.get('requested_level'),
        target_id=request.target_id or plan.get('target_id'), direction=direction, relation_types=sorted(filters),
        max_depth=request.max_depth, slots=[{'surface': s['text'], 'role_hint': s['role'],
            'candidates': s['candidates'], 'source': s.get('match', 'exact')} for s in slots], **limits)
    answer['plan'] = plan
    if not answer['traverse']['paths']:
        return _available_context(retriever, request, scopes, plan, answer)
    return answer


def retrieve_planned(retriever, request, scopes):
    query = request.query
    plan = build_plan(retriever, scopes, query, request.business_context,
                      [c.model_dump(exclude_none=True) for c in request.conditions],
                      request.drill_role, force_joint=request.mode == 'dimensional', force_traverse=request.mode == 'traverse')
    intent = plan['intent']
    limits = {'max_nodes': request.max_nodes, 'max_edges': request.max_edges}
    if request.mode == 'auto' and (plan.get('grouping_spans') or intent in {'clarify', 'search', 'dimensional'}):
        grouped = retrieve_grouping(retriever, request, scopes, plan)
        if grouped is not None:
            return grouped
    if intent == 'clarify':
        return _available_context(retriever, request, scopes, plan)
    if intent == 'traverse':
        return _planned_traversal(retriever, request, scopes, plan)
    if intent == 'overview':
        from .overview import retrieve_overview
        return retrieve_overview(retriever, query, scopes, plan, **limits)
    if intent == 'dimensional':
        answer = retrieve_dimensional(retriever, query, scopes, plan, max_depth=request.max_depth,
                                      direction=request.direction, relation_types=request.relation_types, **limits)
        if answer['dimensional']['status'] == 'no_common_fact':
            return _available_context(retriever, request, scopes, plan, answer)
        return answer
    selection = plan['business_context']
    if intent == 'drilldown':
        cohorts = retriever.authorized_cohorts(scopes, selection)
        hierarchies = retriever.index.authorized_hierarchies(cohorts)
        ids = {c['node_id'] for s in plan['conditions'] for c in s['candidates']}
        if len(ids) != 1 or not hierarchies:
            plan.update(intent='clarify')
            plan['unresolved'].append('需要唯一的起点及可确认方向的层级关系。')
            return _available_context(retriever, request, scopes, plan)
        answer = retrieve_drilldown(retriever, plan['focus'], scopes, selection, seed_ids=sorted(ids),
            hierarchies=hierarchies,
            target_kind=plan['requested_level'], max_depth=request.max_depth, **limits)
    else:
        slots = plan['conditions']
        candidates = [c['node_id'] for s in slots for c in s['candidates']]
        residual = _residuals(plan['focus'], slots, plan['requested_level']) if plan['parser'] == 'index' else []
        if slots and not residual:
            single_anchor = len(slots) == len(candidates) == 1
            hops = int(single_anchor and not plan['point_only'])
            if single_anchor:
                plan['strategy'] = 'semantic_node' if slots[0].get('match') == 'semantic' else 'exact_node'
                if hops:
                    plan['strategy'] = 'bounded_neighborhood'
            else:
                plan['strategy'] = 'matched_nodes'
            answer = retriever.retrieve(query, resolved_conditions=slots, scopes=scopes, business_context=selection,
                                        hops=hops, max_per_rel=3,
                                        max_nodes=min(12, request.max_nodes) if hops else request.max_nodes,
                                        max_edges=min(16, request.max_edges) if hops else request.max_edges)
        else:
            plan['unbound_mentions'].extend({'text': text, 'reason': 'unparsed'} for text in (residual or [plan['focus']]))
            return _available_context(retriever, request, scopes, plan)
    return {**answer, 'query': query, 'plan': plan}

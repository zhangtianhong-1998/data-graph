"""Resolve archived entity decisions back to their exact named physical projections."""
from collections import defaultdict


SOURCE_FIELDS = ('source_id', 'schema', 'table', 'key_columns')
ROLE_FIELDS = ('relation_type', 'end', 'opposite_key_columns')


def _pick(row, fields):
    return {key: row[key] for key in fields if key in row}


def _indexed(items, index):
    if type(index) is not int or not 0 <= index < len(items):
        raise ValueError('审查归档引用了不存在的成员、来源或角色。')
    return items[index]


def _by_pattern(rows):
    result = {}
    for row in rows:
        pid = row['pattern_id']
        if pid in result:
            raise ValueError('审查归档含重复模式记录。')
        result[pid] = row
    return result


def archived_entity_routes(review):
    """Return all *listed* entity cards. Verification counts always describe an entire proposal."""
    cards = _by_pattern(review['patterns'])
    decisions = _by_pattern(review['decisions'])
    checks = _by_pattern(review.get('proposal_checks', []))
    if (decisions.keys() | checks.keys()) - cards.keys():
        raise ValueError('裁决或核实记录未找到对应的归档模式。')
    model = review.get('model_review', {})
    attempts = defaultdict(list)
    for number, batch in enumerate(model.get('batches', []), 1):
        for pid in batch['pattern_ids']:
            attempts[pid].append({'attempt': batch.get('attempt', number),
                                  **_pick(batch, ('parent_attempt', 'status', 'reason'))})
    complete_batch_list = 'batches' in model and model.get('submitted_patterns') == len(attempts)
    routes = []
    for pid, card in cards.items():
        if card['kind'] != 'entity':
            continue
        sources = [_pick(source, SOURCE_FIELDS) for source in card['sources']]
        members = []
        for member in card['members']:
            # Older cards may not attribute the listed sources to individual members.
            indexes = member.get('source_indexes')
            for index in indexes or []:
                _indexed(sources, index)
            members.append({'contributor_id': member['contributor_id'], 'entity_type': member['type'],
                            'key_names': member['key_names'], 'source_indexes': indexes,
                            'roles': [_pick(role, (*ROLE_FIELDS, 'key_columns')) for role in member.get('roles', [])]})
        decision = decisions.get(pid, {})
        action = decision.get('action')
        selected = []
        if action == 'propose':
            for part in decision['alignment']:
                member = _indexed(members, part['member_index'])
                source = _indexed(sources, part['source_index'])
                role = _indexed(member['roles'], part['role_index'])
                if ((member['source_indexes'] is not None and part['source_index'] not in member['source_indexes'])
                        or source['key_columns'] != role['key_columns']):
                    raise ValueError('归档提议的来源列与所选成员或角色不符。')
                selected.append({**_pick(part, ('member_index', 'source_index', 'role_index')),
                                 **_pick(member, ('contributor_id', 'entity_type')), **source, **_pick(role, ROLE_FIELDS)})
            if len({item['member_index'] for item in selected}) != len(selected) or len(selected) < 2:
                raise ValueError('归档提议没有选择至少两个不同成员。')
        failure = (model.get('failed_by_pattern_id') or {}).get(pid)
        if action:
            outcome = 'valid_decision'
        elif pid in attempts or failure:
            outcome = 'submitted_without_valid_decision'
        elif pid in model.get('oversized_pattern_ids', ()) or model.get('model_calls') == 0 or complete_batch_list:
            outcome = 'listed_not_submitted'
        else:
            outcome = 'submission_unverified'
        check = checks.get(pid)
        routes.append({'pattern_id': pid, 'basis': card['basis'], 'eligible_actions': card['eligible_actions'],
                       'reasons': card['reasons'], 'overlapping_keys': card['overlapping_keys'],
                       'pair_coverage': card.get('pair_coverage', []),
                       'sources': sources, 'members': members, 'action': action, 'selected_bindings': selected,
                       'submission': outcome, 'attempts': attempts.get(pid, []), 'failure': failure,
                       'proposal_check': None if check is None else _pick(check, (
                           'status', 'reason', 'overlapping_keys', 'matched_key_names', 'verified_groups',
                           'verified_groups_by_basis', 'rejected_keys_by_reason', 'cross_table_matched_key_checks',
                           'applied_groups', 'collapsed_nodes', 'not_applied_by_reason'))})
    return routes


def _signature(binding):
    return (binding['contributor_id'], binding['entity_type'], binding['source_id'], binding['schema'],
            binding['table'], tuple(binding['key_columns']))


def _role(binding):
    return binding['relation_type'], binding['end'], tuple(binding['opposite_key_columns'])


def _sides(bindings):
    expected = defaultdict(set)
    for item in bindings:
        fields = set(ROLE_FIELDS).intersection(item)
        if fields and len(fields) != len(ROLE_FIELDS):
            raise ValueError('限定关系角色时需要关系类型、方向和另一端的键列。')
        expected[_signature(item)].add(_role(item) if fields else None)
    return expected


def _matching_indexes(route, side):
    offered, selected = set(), set()
    for index, member in enumerate(route['members']):
        for source_index in member['source_indexes'] or ():
            source = route['sources'][source_index]
            roles = side.get(_signature({**member, **source}), set())
            if roles and (None in roles or any(_role(role) in roles for role in member['roles']
                                               if role['key_columns'] == source['key_columns'])):
                offered.add(index)
    for named in route['selected_bindings']:
        roles = side.get(_signature(named), set())
        if roles and (None in roles or _role(named) in roles):
            selected.add(named['member_index'])
    # A recorded selection is exact evidence even on a card predating source_indexes.
    return offered | selected, selected


def match_pair_routes(routes, left_bindings, right_bindings):
    """Match distinct members' full bindings. Empty means no match among the resolvable listed cards."""
    left, right = _sides(left_bindings), _sides(right_bindings)
    matches = []
    for route in routes:
        offered_a, selected_a = _matching_indexes(route, left)
        offered_b, selected_b = _matching_indexes(route, right)
        if not any(a != b for a in offered_a for b in offered_b):
            continue
        action = route['action']
        if action == 'propose':
            selected_pair = any(a != b for a in selected_a for b in selected_b)
            status = 'selected_both_bindings' if selected_pair else 'selected_other_binding'
        elif action == 'merge':
            status = 'type_wide_merge'
        elif action:
            status = 'reviewed_without_alignment'
        else:
            status = route['submission']
        matches.append({'pattern_id': route['pattern_id'], 'status': status, 'action': action,
                        'left_member_indexes': sorted(offered_a), 'right_member_indexes': sorted(offered_b),
                        'left_selected_member_indexes': sorted(selected_a), 'right_selected_member_indexes': sorted(selected_b)})
    return matches

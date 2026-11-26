from copy import deepcopy
from pathlib import Path

import pytest

from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.tests.test_batch_overlap_patterns import unit


@pytest.fixture
def api(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    import fusion_evidence_routes
    return fusion_evidence_routes


def _fixture():
    # A shared spelling of a value or a column gives neither source ownership nor role orientation.
    contributors = [unit('east', 'SalesArea', 'district', table='sales'),
                    unit('west', 'TargetArea', 'district', table='targets')]
    card, = [p for p in analyze_overlap_patterns(contributors).patterns
             if p['kind'] == 'entity' and p['basis'] == 'same_logical_keys_across_sources']
    content = {'patterns': [card], 'decisions': [], 'model_review': {'batches': []}, 'proposal_checks': []}
    alignment, sides = [], []
    for index, member in enumerate(card['members']):
        source_index, = member['source_indexes']
        role_index = next(i for i, r in enumerate(member['roles']) if r['end'] == 'source')
        alignment.append({'member_index': index, 'source_index': source_index, 'role_index': role_index})
        role = member['roles'][role_index]
        sides.append({**card['sources'][source_index], 'contributor_id': member['contributor_id'],
                      'entity_type': member['type'], 'relation_type': role['relation_type'], 'end': role['end'],
                      'opposite_key_columns': role['opposite_key_columns']})
    content['decisions'] = [{'pattern_id': card['pattern_id'], 'action': 'propose', 'alignment': alignment}]
    return content, sides


def test_exact_route_survives_retry_and_rejects_lookalike_axis_owner_and_type(api):
    review, (left, right) = _fixture()
    pid = review['patterns'][0]['pattern_id']
    review['model_review'].update(submitted_patterns=1, batches=[
        {'attempt': 1, 'pattern_ids': [pid], 'status': 'failed', 'reason': 'model_response_truncated'},
        {'attempt': 2, 'parent_attempt': 1, 'pattern_ids': [pid], 'status': 'reviewed'}])
    review['proposal_checks'] = [{'pattern_id': pid, 'reason': 'cross_table_identity_unverified',
                                  'matched_key_names': 2, 'raw_model_reply': 'must not copy'}]
    review['patterns'][0]['sources'][0]['examples'] = ['not a schema field']
    route, = api.archived_entity_routes(review)
    assert route['submission'] == 'valid_decision' and len(route['attempts']) == 2
    assert route['proposal_check'] == {'reason': 'cross_table_identity_unverified', 'matched_key_names': 2}
    assert 'examples' not in route['sources'][0]
    match, = api.match_pair_routes([route], [left], [right])
    assert match['status'] == 'selected_both_bindings'
    for substitute in ({'contributor_id': right['contributor_id']}, {'table': right['table']},
                       {'key_columns': ['alternate_code']}, {'entity_type': right['entity_type']},
                       {'end': 'target'}, {'schema': 'private'}):
        assert not api.match_pair_routes([route], [{**left, **substitute}], [right])
    assert not api.match_pair_routes([route], [left], [left])


def test_an_offered_binding_selected_differently_is_not_a_rejected_key(api):
    review, (left, right) = _fixture()
    card = review['patterns'][0]
    alternate = {**card['sources'][0], 'table': 'another_projection'}
    card['sources'].append(alternate)
    card['members'][0]['source_indexes'].append(len(card['sources']) - 1)
    review['decisions'][0]['alignment'][0]['source_index'] = len(card['sources']) - 1
    route, = api.archived_entity_routes(review)
    match, = api.match_pair_routes([route], [left], [right])
    assert match['status'] == 'selected_other_binding' and match['left_selected_member_indexes'] == []
    also = {**left, 'table': 'another_projection'}
    assert api.match_pair_routes([route], [also], [right])[0]['status'] == 'selected_both_bindings'


def test_absent_member_source_ownership_and_absent_attempts_stay_unknown(api):
    review, (left, right) = _fixture()
    review['decisions'] = []
    base, = api.archived_entity_routes(review)
    assert api.match_pair_routes([base], [left], [right])[0]['status'] == 'submission_unverified'
    review['model_review'].update(submitted_patterns=0, model_calls=0)
    assert api.archived_entity_routes(review)[0]['submission'] == 'listed_not_submitted'
    card = review['patterns'][0]
    del card['members'][0]['source_indexes']
    assert api.match_pair_routes(api.archived_entity_routes(review), [left], [right]) == []
    review['model_review'].update(model_calls=1, submitted_patterns=1, batches=[
        {'pattern_ids': [card['pattern_id']], 'status': 'failed', 'reason': 'model_response_truncated'}])
    assert api.archived_entity_routes(review)[0]['submission'] == 'submitted_without_valid_decision'


def test_legacy_selection_without_source_indexes_is_still_locatable_and_invalid_index_fails(api):
    review, (left, right) = _fixture()
    for member in review['patterns'][0]['members']:
        member.pop('source_indexes')
    routes = api.archived_entity_routes(review)
    assert api.match_pair_routes(routes, [left], [right])[0]['status'] == 'selected_both_bindings'
    invalid = deepcopy(review)
    invalid['decisions'][0]['alignment'][0]['member_index'] = -1
    with pytest.raises(ValueError, match='不存在'):
        api.archived_entity_routes(invalid)

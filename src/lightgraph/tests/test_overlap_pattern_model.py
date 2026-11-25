"""Exercise the project model adapter through the real SDK with a local HTTP transport."""
import json

import httpx
import pytest

from lightgraph.construction.overlap_review import review_overlap_patterns
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig


def configured(**settings):
    model = {'mode': 'in_environment', 'base_url': 'https://project-model.example/v1',
             'model': 'configured-model', 'api_key_env': 'LIGHTGRAPH_OVERLAP_TEST_KEY', **settings}
    return RunConfig(sources=[], model=model)


def profile(number=1, *, eligible=True):
    return {'pattern_id': f'pattern_{number:024x}', 'kind': 'entity', 'basis': 'same_physical_key',
            'members': [{'contributor_id': 'u1', 'type': 'dimension', 'key_names': ['item_code'],
                         'property_names': ['alias', 'measurement'],
                         'roles': [{'relation_type': 'item_belongs_to_group', 'end': 'source',
                                    'key_columns': ['id'], 'opposite_key_columns': ['group_code'],
                                    'raw_values': ['PRIVATE_ROLE']}],
                         'raw_node': 'PRIVATE_NODE'}],
            'sources': [{'source_id': 'warehouse', 'schema': 'public', 'table': 'dim_item',
                         'key_columns': ['id'], 'actual_value': 'PRIVATE_VALUE'}], 'overlapping_keys': 1275,
            'reasons': [], 'eligible_actions': ['apply', 'retain'] if eligible else ['retain'],
            'samples': ['PRIVATE_SAMPLE']}


def respond(content, finish='stop'):
    return httpx.Response(200, json={'id': 'completion', 'object': 'chat.completion', 'created': 1,
        'model': 'configured-model', 'choices': [{'index': 0, 'finish_reason': finish,
        'message': {'role': 'assistant', 'content': content}}]})


def setup_provider(monkeypatch, handler):
    requests = []

    def dispatch(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda: httpx.MockTransport(dispatch))
    return requests


def test_one_project_request_for_whole_patterns_and_structural_metadata_only(monkeypatch):
    patterns = [profile(1), profile(2, eligible=False)]
    actions = ['merge', 'review']

    def reply(request):
        assert request.url == 'https://project-model.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer session-secret'
        assert b'PRIVATE_' not in request.content and b'session-secret' not in request.content
        payload = json.loads(request.content)
        assert 'tools' not in payload
        assert payload['response_format']['json_schema']['strict']
        issued = json.loads(payload['messages'][1]['content'])['patterns']
        assert len(issued) == 2 and issued[0]['overlapping_keys'] == 1275
        descriptor = issued[0]['members'][0]
        assert descriptor['property_names'] == ['alias', 'measurement']
        assert descriptor['roles'] == [{'relation_type': 'item_belongs_to_group', 'end': 'source',
                                        'key_columns': ['id'], 'opposite_key_columns': ['group_code']}]
        return respond(json.dumps({'decisions': [
            {'pattern_id': item['pattern_id'], 'action': action} for item, action in zip(issued, actions)]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns, api_key='session-secret')
    assert result['status'] == 'reviewed' and result['model_calls'] == len(requests) == 1
    assert result['reviewed_patterns'] == 2 and result['omitted_patterns'] == 0
    assert result['selected_pattern_ids'] == [patterns[0]['pattern_id']]
    assert result['decisions'][1]['action'] == 'review'


def alignment_card():
    pattern = profile(7, eligible=False)
    pattern['basis'] = 'same_typed_keys_and_names_across_sources'
    pattern['reasons'] = ['cross_source_identity_unverified']
    pattern['members'][0]['source_indexes'] = [0]
    pattern['members'].append({'contributor_id': 'u2', 'type': 'sales_axis', 'key_names': ['axis_code'],
                               'source_indexes': [1], 'roles': [{'relation_type': 'axis_in_catalog', 'end': 'source',
                                  'key_columns': ['axis'], 'opposite_key_columns': ['catalog_id']}]})
    pattern['sources'].append({'source_id': 'warehouse', 'schema': 'public', 'table': 'sales',
                               'key_columns': ['axis']})
    return pattern


def test_agent_proposes_a_bounded_alignment_for_a_retain_only_pattern(monkeypatch):
    pattern = alignment_card()

    def reply(request):
        assert b'PRIVATE_' not in request.content
        issued, = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        assert issued['eligible_actions'] == ['retain']
        assert [member['source_indexes'] for member in issued['members']] == [[0], [1]]
        alignment = [{'member_index': i, 'source_index': i, 'role_index': 0} for i in (1, 0)]
        return respond(json.dumps({'decisions': [{'pattern_id': issued['pattern_id'], 'action': 'propose',
                                                  'alignment': alignment}]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), [pattern], api_key='key')
    assert result['model_calls'] == len(requests) == result['reviewed_patterns'] == 1
    assert result['selected_pattern_ids'] == []
    assert result['decisions'] == [{'pattern_id': pattern['pattern_id'], 'action': 'propose',
        'alignment': [{'member_index': i, 'source_index': i, 'role_index': 0} for i in (0, 1)]}]


@pytest.mark.parametrize('failure', [
    'bounds', 'wrong_owner', 'wrong_role', 'key', 'arity', 'direction', 'relation', 'singleton',
    'duplicate_member', 'duplicate_projection', 'boolean_handle', 'string_handle', 'raw_value', 'merge_with_alignment',
])
def test_invalid_model_alignment_grants_no_decision(monkeypatch, failure):
    pattern = alignment_card()
    alignment = [{'member_index': i, 'source_index': i, 'role_index': 0} for i in (0, 1)]
    decision = {'pattern_id': pattern['pattern_id'], 'action': 'propose', 'alignment': alignment}
    if failure == 'bounds':
        alignment[1]['member_index'] = -1
    elif failure == 'wrong_owner':
        pattern['members'][1]['roles'][0]['key_columns'] = ['id']
        alignment[1]['source_index'] = 0
    elif failure == 'wrong_role':
        alignment[1]['role_index'] = 1
    elif failure == 'key':
        pattern['sources'][1]['key_columns'] = ['different_key']
    elif failure == 'arity':
        pattern['members'][1]['key_names'].append('context')
        pattern['members'][1]['roles'][0]['key_columns'].append('context')
        pattern['sources'][1]['key_columns'].append('context')
    elif failure == 'direction':
        pattern['members'][1]['roles'][0]['end'] = 'target'
    elif failure == 'relation':
        pattern['kind'] = 'relation'
    elif failure == 'singleton':
        alignment.pop()
    elif failure == 'duplicate_member':
        alignment[1] = alignment[0]
    elif failure == 'duplicate_projection':
        pattern['members'][1].update(contributor_id='u1', type='dimension')
    elif failure == 'boolean_handle':
        alignment[1]['member_index'] = True
    elif failure == 'string_handle':
        alignment[1]['member_index'] = '1'
    elif failure == 'raw_value':
        alignment[1]['accept_values'] = ['some-row']
    elif failure == 'merge_with_alignment':
        decision['action'] = 'merge'
        pattern['eligible_actions'] = ['apply', 'retain']

    setup_provider(monkeypatch, lambda _: respond(json.dumps({'decisions': [decision]})))
    result = review_overlap_patterns(configured(), [pattern], api_key='key')
    assert result['status'] == 'fallback' and result['failed_patterns'] == 1
    assert result['decisions'] == [] and result['selected_pattern_ids'] == []


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'unissued', 'new_rule', 'ineligible',
                                      'malformed', 'duplicate_json_key', 'truncated', 'server', 'timeout'])
def test_unusable_batch_grants_nothing_and_performs_one_attempt(monkeypatch, failure):
    patterns = [profile(1), profile(2, eligible=False)]
    decisions = [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in patterns]
    body = {'decisions': decisions}
    if failure == 'missing':
        decisions.pop()
    elif failure == 'duplicate':
        decisions.append(decisions[0])
    elif failure == 'unissued':
        decisions[0]['pattern_id'] = profile(100)['pattern_id']
    elif failure == 'new_rule':
        decisions[0]['node_ids'] = ['n_secret']
    elif failure == 'ineligible':
        decisions[1]['action'] = 'merge'

    def reply(_request):
        if failure == 'server':
            return httpx.Response(503)
        if failure == 'timeout':
            raise httpx.ReadTimeout('simulation')
        if failure == 'malformed':
            return respond('not JSON')
        if failure == 'duplicate_json_key':
            return respond('{"decisions":[],"decisions":[]}')
        return respond(json.dumps(body), 'length' if failure == 'truncated' else 'stop')

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns, api_key='secret')
    assert result['status'] == 'fallback' and result['model_calls'] == len(requests) == 1
    assert result['selected_pattern_ids'] == [] and result['reviewed_patterns'] == 0


@pytest.mark.parametrize('settings,extra,reason', [
    ({'mode': 'metadata_only'}, {}, 'model_not_configured'),
    ({'max_calls': 0}, {}, 'model_call_budget_exhausted'),
    ({'base_url': 'http://external.invalid/v1'}, {}, 'insecure_model_endpoint'),
    ({}, {'api_key': ''}, 'model_credentials_unavailable'),
    ({}, {'remaining_seconds': 0}, 'wall_time_budget_exhausted'),
    ({'max_prompt_bytes': 100}, {}, 'model_prompt_budget_exhausted'),
])
def test_unavailable_and_over_budget_have_zero_calls(monkeypatch, settings, extra, reason):
    monkeypatch.setenv('LIGHTGRAPH_OVERLAP_TEST_KEY', 'env-secret')
    requests = setup_provider(monkeypatch, lambda _request: pytest.fail('unexpected request'))
    result = review_overlap_patterns(configured(**settings), [profile()], **extra)
    assert result['reason'] == reason and result['model_calls'] == len(requests) == 0
    assert result['selected_pattern_ids'] == []


def test_bounds_reviewed_profile_count_and_preserves_configured_endpoint_key(monkeypatch):
    patterns = [profile(i) for i in range(40)]
    monkeypatch.setenv('LIGHTGRAPH_OVERLAP_TEST_KEY', 'env-secret')

    def reply(request):
        assert request.headers['Authorization'] == 'Bearer env-secret'
        items = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        return respond(json.dumps({'decisions': [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in items]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns)
    assert result['status'] == 'partial' and result['reviewed_patterns'] == 32
    assert result['omitted_patterns'] == 8 and result['reason'] == 'pattern_count_limit'
    assert result['model_calls'] == len(requests) == 8
    assert [pid for batch in result['batches'] for pid in batch['pattern_ids']] == [
        p['pattern_id'] for p in patterns[:32]]


def test_transport_cancellation_during_review_propagates_without_decision(monkeypatch):
    cancelled = False

    def check():
        if cancelled:
            raise LightGraphError('run_cancelled')

    def reply(_request):
        nonlocal cancelled
        cancelled = True
        return respond(json.dumps({'decisions': [{'pattern_id': profile()['pattern_id'], 'action': 'merge'}]}))

    requests = setup_provider(monkeypatch, reply)
    dispatched = []
    with pytest.raises(LightGraphError, match='run_cancelled'):
        review_overlap_patterns(configured(), [profile()], api_key='key', check_cancelled=check,
                                on_model_call=lambda: dispatched.append(1))
    assert len(requests) == len(dispatched) == 1
    with pytest.raises(LightGraphError, match='run_cancelled'):
        review_overlap_patterns(configured(), [profile()], api_key='key', check_cancelled=check)
    assert len(requests) == 1


def test_oversized_pattern_does_not_hide_later_complete_relation(monkeypatch):
    oversized, relation = profile(1), profile(2, eligible=False)
    oversized['sources'][0]['table'] = 'a' * 70000
    relation['kind'] = 'relation'
    relation['basis'] = 'same_directed_source_columns'
    relation['sources'][0].update(source_key_columns=['child_code'], target_key_columns=['parent_code'])

    def reply(request):
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        assert len(issued) == 1
        source = issued[0]['sources'][0]
        assert source['source_key_columns'] == ['child_code'] and source['target_key_columns'] == ['parent_code']
        return respond(json.dumps({'decisions': [{'pattern_id': relation['pattern_id'], 'action': 'keep'}]}))

    setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), [oversized, relation], api_key='project-key')
    assert result['reviewed_patterns'] == result['omitted_patterns'] == 1


@pytest.mark.parametrize('size', [22, 32])
def test_complete_batches_survive_one_truncated_batch(monkeypatch, size):
    patterns = [profile(i) for i in range(size)]
    def reply(request):
        packet = json.loads(request.content)
        items = json.loads(packet['messages'][1]['content'])['patterns']
        assert 1 <= len(items) <= 4
        assert packet['max_tokens'] == 8192
        decisions = [{'pattern_id': item['pattern_id'], 'action': 'merge'} for item in items]
        return respond(json.dumps({'decisions': decisions}), 'length' if items[0]['pattern_id'] == patterns[4]['pattern_id'] else 'stop')

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns[::-1], api_key='secret')
    assert result['model_calls'] == len(requests) == (size + 3) // 4
    assert result['status'] == 'partial' and result['reason'] == 'model_response_truncated'
    assert result['failed_patterns'] == 4 and result['submitted_patterns'] == size
    assert result['reviewed_patterns'] == size - 4 and result['omitted_patterns'] == 0
    assert result['selected_pattern_ids'] == [p['pattern_id'] for p in patterns[:4] + patterns[8:]]
    assert result['batches'][1] == {'pattern_ids': [p['pattern_id'] for p in patterns[4:8]],
        'output_token_limit': 8192, 'status': 'failed', 'reason': 'model_response_truncated'}


@pytest.mark.parametrize('model_calls,node_calls,limit', [(1, 3, 1), (None, 2, 2)])
def test_call_and_output_limits_bound_transport(monkeypatch, model_calls, node_calls, limit):
    def reply(request):
        payload = json.loads(request.content)
        items = json.loads(payload['messages'][1]['content'])['patterns']
        assert len(items) == 1 and payload['max_tokens'] == 1024
        return respond(json.dumps({'decisions': [{'pattern_id': items[0]['pattern_id'], 'action': 'keep'}]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(max_calls=model_calls, max_output_tokens=1024),
                                     [profile(i) for i in range(8)], api_key='key', max_calls=node_calls)
    assert len(requests) == result['model_calls'] == result['submitted_patterns'] == limit
    assert result['failed_patterns'] == 0 and result['omitted_patterns'] == 8 - limit
    assert result['reason'] == 'model_call_budget_exhausted' and result['status'] == 'partial'


def test_split_prompt_byte_budget_keeps_later_patterns(monkeypatch):
    patterns = [profile(i) for i in range(7)]
    for item in patterns:
        item['sources'][0]['table'] = 'large_structural_field_' * 100

    def reply(request):
        assert len(request.content) <= 8100
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        return respond(json.dumps({'decisions': [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in issued]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(max_prompt_bytes=8100), patterns, api_key='key')
    assert result['status'] == 'reviewed' and result['submitted_patterns'] == len(patterns)
    assert result['oversized_pattern_ids'] == [] and result['omitted_patterns'] == 0
    assert len(requests) > 2


def test_all_batches_share_one_workflow_deadline(monkeypatch):
    from lightgraph.discovery import sdk_transport
    from lightgraph.errors import BudgetExceeded

    dispatch = sdk_transport.request_completion
    budgets = []
    def limited(config, budget, *args):
        budgets.append(budget)
        if len(budgets) == 2:
            raise BudgetExceeded('wall_time_budget_exhausted')
        assert budget.remaining_seconds() > 60 and config.timeout_seconds == 10
        return dispatch(config, budget, *args)
    monkeypatch.setattr(sdk_transport, 'request_completion', limited)
    def reply(request):
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        return respond(json.dumps({'decisions': [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in issued]}))
    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(timeout_seconds=10), [profile(i) for i in range(10)],
                                     api_key='key', remaining_seconds=90)
    assert len(budgets) == 2 and budgets[0] is budgets[1]
    assert result['status'] == 'partial' and result['reason'] == 'wall_time_budget_exhausted'
    assert result['reviewed_patterns'] == 4 and result['failed_patterns'] == 0
    assert result['omitted_patterns'] == 6 and result['model_calls'] == len(requests) == 1

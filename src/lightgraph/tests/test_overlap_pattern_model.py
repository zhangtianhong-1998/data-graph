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

    setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns)
    assert result['status'] == 'reviewed' and result['reviewed_patterns'] == 32
    assert result['omitted_patterns'] == 8 and result['model_calls'] == 1


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

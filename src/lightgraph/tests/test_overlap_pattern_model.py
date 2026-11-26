"""Exercise the project model adapter through the real SDK with a local HTTP transport."""
import json
from copy import deepcopy

import httpx
import pytest

from lightgraph.construction.overlap_review import review_overlap_patterns
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import MergePolicyConfig, RunConfig
from lightgraph.tests.test_batch_overlap_patterns import unit


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


def test_only_unambiguous_members_can_use_compact_proposals(monkeypatch):
    card = alignment_card()
    other = profile(8, eligible=False)

    def reply(request):
        cards = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        choices = next(p['unique_binding_members'] for p in cards if p['pattern_id'] == card['pattern_id'])
        assert choices == [0, 1]
        return respond(json.dumps({'decisions': [
            {'pattern_id': card['pattern_id'], 'action': 'propose', 'member_indexes': choices},
            {'pattern_id': other['pattern_id'], 'action': 'review', 'alignment': []}]}))

    setup_provider(monkeypatch, reply)
    report = review_overlap_patterns(configured(), [card, other], api_key='key')
    assert report['decisions'] == [
        {'pattern_id': card['pattern_id'], 'action': 'propose', 'alignment': [
            {'member_index': n, 'source_index': n, 'role_index': 0} for n in (0, 1)]},
        {'pattern_id': other['pattern_id'], 'action': 'review'}]
    assert report['retry_batches'] == report['failed_patterns'] == 0


def test_retry_only_the_invalid_proposal_once_and_keep_other_valid_decisions(monkeypatch):
    card = alignment_card()
    card['members'][1]['roles'].append(dict(card['members'][1]['roles'][0], relation_type='second_role'))
    other = profile(8)

    def reply(request):
        cards = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        proposals = []
        for p in cards:
            if p['pattern_id'] == other['pattern_id']:
                proposals.append({'pattern_id': p['pattern_id'], 'action': 'keep', 'alignment': []})
            else:
                assert 1 not in p.get('unique_binding_members', [])
                proposals.append({'pattern_id': p['pattern_id'], 'action': 'propose', 'member_indexes': [0, 1]})
        return respond(json.dumps({'decisions': proposals}))

    requests = setup_provider(monkeypatch, reply)
    report = review_overlap_patterns(configured(), [card, other], api_key='key')
    assert len(requests) == 2 and report['batches'][1]['pattern_ids'] == [card['pattern_id']]
    assert report['submitted_patterns'] == 2 and report['reviewed_patterns'] == report['failed_patterns'] == 1
    assert report['decisions'] == [{'pattern_id': other['pattern_id'], 'action': 'keep'}]


@pytest.mark.parametrize('failure', [
    'bounds', 'wrong_owner', 'wrong_role', 'role_at_another_table', 'key', 'arity', 'direction', 'relation', 'singleton',
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
    elif failure == 'role_at_another_table':
        pattern['members'][1]['roles'][0]['source_indexes'] = [0]
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


@pytest.mark.parametrize('failure', ['duplicate', 'unissued', 'new_rule', 'ineligible',
                                      'malformed', 'duplicate_json_key', 'server', 'timeout'])
def test_bad_cards_and_transport_failures_do_not_grant_invalid_decisions(monkeypatch, failure):
    patterns = [profile(1), profile(2, eligible=False)]
    decisions = [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in patterns]
    body = {'decisions': decisions}
    if failure == 'duplicate':
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
        return respond(json.dumps(body))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns, api_key='secret')
    isolated = failure in {'duplicate', 'unissued', 'new_rule', 'ineligible'}
    assert result['status'] == ('partial' if isolated else 'fallback')
    assert result['model_calls'] == len(requests) == (2 if isolated or failure in {'server', 'timeout'} else 1)
    assert result['selected_pattern_ids'] == [] and result['reviewed_patterns'] == int(isolated)
    assert result['failed_patterns'] == (1 if isolated else 2)


def test_complete_unique_answers_survive_an_omitted_pattern_and_its_retry(monkeypatch):
    cards = [profile(i) for i in (1, 2)]
    submitted = []

    def reply(request):
        batch = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        submitted.append([p['pattern_id'] for p in batch])
        return respond(json.dumps({'decisions': [{'pattern_id': batch[0]['pattern_id'],
            'action': 'review', 'member_indexes': [0, 1]}]}))

    setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), cards, api_key='key')
    a, b = [p['pattern_id'] for p in cards]
    assert submitted == [[a, b], [b]]
    assert result['recovered_patterns'] == 1 and result['failed_patterns'] == 0
    assert result['decisions'] == [{'pattern_id': pid, 'action': 'review'} for pid in (a, b)]


def test_malformed_proposal_cannot_discard_a_sibling_or_overrun_the_budget(monkeypatch):
    cards = [alignment_card(), alignment_card()]
    cards[1]['pattern_id'] = profile(8)['pattern_id']
    good, bad = [p['pattern_id'] for p in cards]
    setup_provider(monkeypatch, lambda _: respond(json.dumps({'decisions': [
        {'pattern_id': good, 'action': 'propose', 'member_indexes': [0, 1]},
        {'pattern_id': bad, 'action': 'propose', 'alignment': [{'member_index': 0}, {'member_index': 1}]}]})))
    result = review_overlap_patterns(configured(max_calls=1), cards, api_key='key')
    assert [d['pattern_id'] for d in result['decisions']] == [good]
    assert result['batches'][0]['invalid_proposals'] == {bad: 'invalid_proposal_shape'}
    assert result['failed_by_pattern_id'] == {bad: 'invalid_decisions'}
    assert result['model_calls'] == 1 and result['submitted_patterns'] == 2 and result['omitted_patterns'] == 0


@pytest.mark.parametrize('case', ['unique_pair', 'third_member', 'two_roles', 'two_sources', 'extra_field'])
def test_bare_propose_can_only_mean_one_pair_of_bindings(monkeypatch, case):
    card = alignment_card()
    decision = {'pattern_id': card['pattern_id'], 'action': 'propose'}
    if case == 'third_member':
        card['members'].append(dict(card['members'][0], contributor_id='third'))
    elif case == 'two_roles':
        card['members'][1]['roles'].append(dict(card['members'][1]['roles'][0], relation_type='other_role'))
    elif case == 'two_sources':
        card['sources'].append(dict(card['sources'][1], table='other_table'))
        card['members'][1]['source_indexes'].append(2)
    elif case == 'extra_field':
        decision['identity_domain'] = 'any'
    setup_provider(monkeypatch, lambda _: respond(json.dumps({'decisions': [decision]})))
    report = review_overlap_patterns(configured(), [card], api_key='key')
    if case == 'unique_pair':
        assert report['decisions'] == [{**decision, 'alignment': [
            {'member_index': n, 'source_index': n, 'role_index': 0} for n in (0, 1)]}]
    else:
        assert report['failed_patterns'] == 1 and report['decisions'] == []


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
    assert result['submitted_patterns'] == 0 and result['omitted_patterns'] == result['total_patterns'] == 1
    assert result['selected_pattern_ids'] == []


def test_all_indexed_axes_can_reach_review_but_a_last_ranked_proposal_still_needs_evidence(monkeypatch):
    contributors = []
    for owner in ('actuals', 'targets'):
        families = [unit(f'{owner}_{i}', f'Dimension{i}', f'key{i}', table=owner, column=f'code{i}', role=f'uses{i}')
                    for i in range(66)]
        contributor, *others = families
        contributor['contributor_id'] = owner
        graph = contributor['graph']
        for other in others:
            for rows in ('nodes', 'edges'):
                graph[rows].extend(other['graph'][rows])
            for definitions in ('entities', 'relations'):
                graph['definitions'][definitions].extend(other['graph']['definitions'][definitions])
        contributors.append(contributor)
    analysis = analyze_overlap_patterns(contributors)
    patterns = analysis.patterns
    assert len(patterns) == 66 and analysis.summary['omitted_patterns'] == 0
    assert all(p['basis'] == 'same_logical_keys_across_sources' and p['eligible_actions'] == ['retain']
               for p in patterns)
    monkeypatch.setenv('LIGHTGRAPH_OVERLAP_TEST_KEY', 'env-secret')

    def reply(request):
        assert request.headers['Authorization'] == 'Bearer env-secret'
        items = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        assert 1 <= len(items) <= 4
        decisions = []
        for p in items:
            decision = {'pattern_id': p['pattern_id'], 'action': 'keep'}
            if p['pattern_id'] == patterns[-1]['pattern_id']:
                assert 'propose' in p['review_actions'] and 'merge' not in p['review_actions']
                decision.update(action='propose', alignment=[{'member_index': i, 'source_index': m['source_indexes'][0],
                    'role_index': next(j for j, r in enumerate(m['roles']) if r['end'] == 'source')}
                    for i, m in enumerate(p['members'])])
            decisions.append(decision)
        return respond(json.dumps({'decisions': decisions}))

    requests = setup_provider(monkeypatch, reply)
    skipped = review_overlap_patterns(configured(mode='metadata_only'), patterns)
    assert skipped['reason'] == 'model_not_configured' and skipped['model_calls'] == len(requests) == 0
    assert skipped['omitted_patterns'] == 66 and skipped['submitted_patterns'] == 0
    result = review_overlap_patterns(configured(), patterns)
    assert result['status'] == 'reviewed' and result['reviewed_patterns'] == result['submitted_patterns'] == 66
    assert result['omitted_patterns'] == 0 and result['selected_pattern_ids'] == []
    assert result['model_calls'] == len(requests) == 17
    assert [pid for batch in result['batches'] for pid in batch['pattern_ids']] == [p['pattern_id'] for p in patterns]
    policy, (check,) = verify_projection_proposals(MergePolicyConfig(), contributors, patterns, result['decisions'])
    assert check['pattern_id'] == patterns[-1]['pattern_id'] and check['verified_groups'] == 0
    assert not policy.verified_projection_rules


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
    result = review_overlap_patterns(configured(max_prompt_bytes=65000), [oversized, relation], api_key='project-key')
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
    result = review_overlap_patterns(configured(max_output_tokens=8192), patterns[::-1], api_key='secret')
    assert result['model_calls'] == len(requests) == (size + 3) // 4 + 4
    assert result['status'] == 'partial' and result['reason'] == 'model_response_truncated'
    assert result['failed_patterns'] == 1 and result['submitted_patterns'] == size
    assert result['failed_by_pattern_id'] == {patterns[4]['pattern_id']: 'model_response_truncated'}
    assert result['reviewed_patterns'] == size - 1 and result['omitted_patterns'] == 0
    assert result['selected_pattern_ids'] == [p['pattern_id'] for p in patterns[:4] + patterns[5:]]
    assert result['batches'][1] == {'pattern_ids': [p['pattern_id'] for p in patterns[4:8]],
        'attempt': 2, 'parent_attempt': None,
        'output_token_limit': 8192, 'status': 'failed', 'reason': 'model_response_truncated',
        'finish_reason': 'length', 'reasoning_tokens': None, 'retryable': False}
    assert result['retry_batches'] == 4 and result['truncated_batches'] == 3 and result['recovered_patterns'] == 3


@pytest.mark.parametrize('settings,case,limits,status', [
    ({'max_output_tokens': 10000}, 'length', [10000], 'fallback'),
    ({'max_output_tokens': 50000}, 'length', [50000], 'fallback'),
    ({'max_output_tokens': 10000, 'max_calls': 1}, 'length', [10000], 'fallback'),
    ({'max_output_tokens': 6000}, 'length', [6000], 'fallback'),
    ({'max_output_tokens': 16000, 'context_window_tokens': 16001}, 'length', [], 'skipped'),
    ({'max_output_tokens': 10000, 'max_response_bytes': 500}, 'bytes', [10000], 'fallback'),
    ({'max_output_tokens': 10000}, 'refused', [10000], 'fallback'),
])
def test_singleton_uses_the_configured_budget_without_silent_provider_parameter_changes(
        monkeypatch, settings, case, limits, status):
    pid = profile()['pattern_id']
    def reply(request):
        if case == 'bytes':
            return respond('x' * 2000)
        if case == 'refused':
            return respond('', 'content_filter')
        return respond(json.dumps({'decisions': [{'pattern_id': pid, 'action': 'merge'}]}), 'length')
    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(**settings), [profile()], api_key='key')
    assert [json.loads(req.content)['max_tokens'] for req in requests] == limits
    assert [b['output_token_limit'] for b in result['batches']] == limits
    assert result['status'] == status and result['selected_pattern_ids'] == []
    assert result['recovered_patterns'] == result['expanded_output_retries'] == 0
    assert result['submitted_patterns'] == result['failed_patterns'] == len(limits)
    if 'context_window_tokens' in settings:
        assert result['reason'] == 'model_prompt_budget_exhausted' and result['omitted_patterns'] == 1


@pytest.mark.parametrize('call_limit,expected_status,reviewed,recovered,failed,omitted', [
    (1, 'fallback', 0, 0, 4, 28),
    (8, 'partial', 12, 0, 20, 0),
    (18, 'reviewed', 32, 20, 0, 0),
])
def test_recovers_five_truncated_batches_or_accounts_for_each_pattern_at_budget(
        monkeypatch, call_limit, expected_status, reviewed, recovered, failed, omitted):
    patterns = [profile(i) for i in range(32)]
    def reply(request):
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        truncated = len(issued) == 4 and issued[0]['pattern_id'] < patterns[20]['pattern_id']
        decisions = [{'pattern_id': p['pattern_id'], 'action': 'merge' if truncated else 'review'} for p in issued]
        body = respond(json.dumps({'decisions': decisions}), 'length' if truncated else 'stop').json()
        body['usage'] = {'prompt_tokens': 91, 'completion_tokens': 8192, 'total_tokens': 8283,
                         'completion_tokens_details': {'reasoning_tokens': 8130, 'other': 'PRIVATE'}}
        return httpx.Response(200, json=body)

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), patterns, api_key='key', max_calls=call_limit)
    assert result['model_calls'] == len(requests) == call_limit and result['status'] == expected_status
    assert result['submitted_patterns'] + result['omitted_patterns'] == len(patterns)
    assert result['submitted_patterns'] == result['reviewed_patterns'] + result['failed_patterns']
    assert (result['reviewed_patterns'], result['recovered_patterns'], result['failed_patterns'],
            result['omitted_patterns']) == (reviewed, recovered, failed, omitted)
    assert result['selected_pattern_ids'] == [] and all(d['action'] == 'review' for d in result['decisions'])
    assert all(b['token_usage'] == {'prompt_tokens': 91, 'completion_tokens': 8192, 'reasoning_tokens': 8130}
               for b in result['batches'])
    assert all(b['reasoning_tokens'] == 8130 and b['finish_reason'] == (
        'length' if b['reason'] == 'model_response_truncated' else 'stop') for b in result['batches'])
    for batch in result['batches'][8:]:
        parent = result['batches'][batch['parent_attempt'] - 1]
        assert parent['reason'] == 'model_response_truncated'
        assert set(batch['pattern_ids']) < set(parent['pattern_ids'])
    assert result['reason'] == ('pattern_decisions_validated' if not failed else 'model_call_budget_exhausted')


def test_zero_key_two_member_pattern_asks_for_review_and_exposes_only_pair_counts(monkeypatch):
    card = alignment_card()
    card.update(basis='same_observed_names_with_different_keys', overlapping_keys=0, overlapping_names=17,
                pair_coverage=[{'member_indexes': [0, 1], 'overlapping_keys': 0, 'keys': ['PRIVATE_1']}])
    def reply(request):
        assert b'PRIVATE_' not in request.content
        issued, = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        assert issued['pair_coverage'] == [{'member_indexes': [0, 1], 'overlapping_keys': 0}]
        assert issued['review_actions'] == ['keep', 'review']
        return respond(json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'propose',
            'alignment': [{'member_index': i, 'source_index': i, 'role_index': 0} for i in range(2)]}]}))
    setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), [card], api_key='key')
    assert result['failed_by_pattern_id'] == {card['pattern_id']: 'invalid_decisions'} and not result['decisions']


@pytest.mark.parametrize('model_calls,node_calls,limit', [(1, 3, 1), (None, 2, 2)])
def test_call_and_output_limits_bound_transport(monkeypatch, model_calls, node_calls, limit):
    def reply(request):
        payload = json.loads(request.content)
        items = json.loads(payload['messages'][1]['content'])['patterns']
        assert len(items) == 1 and payload['max_tokens'] == 1024
        return respond(json.dumps({'decisions': [{'pattern_id': items[0]['pattern_id'], 'action': 'keep'}]}))

    requests = setup_provider(monkeypatch, reply)
    patterns = [profile(i) for i in range(66)]
    result = review_overlap_patterns(configured(max_calls=model_calls, max_output_tokens=1024),
                                     patterns, api_key='key', max_calls=node_calls)
    assert len(requests) == result['model_calls'] == result['submitted_patterns'] == limit
    assert result['failed_patterns'] == 0 and result['omitted_patterns'] == len(patterns) - limit
    assert [p for batch in result['batches'] for p in batch['pattern_ids']] == [p['pattern_id'] for p in patterns[:limit]]
    assert result['reason'] == 'model_call_budget_exhausted' and result['status'] == 'partial'


def test_split_prompt_byte_budget_keeps_later_patterns(monkeypatch):
    patterns = [profile(i) for i in range(7)]
    for item in patterns:
        item['sources'][0]['table'] = 'large_structural_field_' * 100

    limit = prompt_bytes([patterns[0]]) + 32
    assert prompt_bytes(patterns[:2]) > limit
    def reply(request):
        assert len(request.content) <= limit
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        return respond(json.dumps({'decisions': [{'pattern_id': p['pattern_id'], 'action': 'keep'} for p in issued]}))

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(max_prompt_bytes=limit), patterns, api_key='key')
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


def test_optional_table_evidence_yields_space_before_an_entire_card_is_skipped(monkeypatch):
    card = alignment_card()
    counts = {'member_indexes': [0, 1], 'source_indexes': [0, 1], 'overlapping_keys': 9,
              'matching_observed_names': 8, 'conflicting_observed_names': 0, 'unverified_names': 1}
    card['binding_comparisons'] = {'candidate_pairs': 24, 'compared_pairs': 24, 'rows': [counts] * 24}

    def reply(request):
        issued, = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        summary = issued['binding_comparisons']
        assert summary['candidate_pairs'] == summary['compared_pairs'] == 24
        assert 0 < len(summary['rows']) < 24
        return respond(json.dumps({'decisions': [{'pattern_id': issued['pattern_id'], 'action': 'review'}]}))

    smaller = deepcopy(card)
    smaller['binding_comparisons']['rows'] = [counts] * 6
    limit = prompt_bytes([smaller]) + 32
    assert prompt_bytes([card]) > limit
    calls = setup_provider(monkeypatch, reply)
    outcome = review_overlap_patterns(configured(max_prompt_bytes=limit), [card], api_key='k')
    assert len(calls) == outcome['reviewed_patterns'] == 1 and not outcome['oversized_pattern_ids']
    assert len(card['binding_comparisons']['rows']) == 24


def prompt_bytes(cards):
    """Calibrate byte-boundary cases against the complete current schema and cards."""
    from lightgraph.construction.overlap_review import _Batch, _INSTRUCTIONS, _structural_profile
    from lightgraph.contracts import canonical
    from lightgraph.discovery.llm import response_format_for
    payload = {'model': 'configured-model', 'stream': False, 'max_tokens': 65536,
               'response_format': response_format_for(_Batch.model_json_schema(), 'json_schema'),
               'messages': [{'role': 'system', 'content': _INSTRUCTIONS},
                            {'role': 'user', 'content': canonical({'patterns': [_structural_profile(c) for c in cards]})}]}
    return len(canonical(payload).encode())


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('output_limit', [1024, 131072])
def test_resolved_project_and_external_agent_settings_reach_the_http_request(
        monkeypatch, tmp_path, external, output_limit):
    settings = {'max_output_tokens': output_limit, 'output_token_field': 'max_completion_tokens',
                'response_format': 'json_object', 'timeout_seconds': 17,
                'max_prompt_bytes': 300000, 'max_response_bytes': 300000,
                'max_prompt_tokens': 250000, 'context_window_tokens': 500000, 'max_calls': 2}
    config = configured(**settings)
    if external:
        agent_file = tmp_path / 'agent.yaml'
        agent_file.write_text(json.dumps({'llm': config.model.model_dump(mode='json')}))
        config = RunConfig(sources=[], agent_config=agent_file.name)
        config.resolve_paths(tmp_path)
    card = profile()
    if output_limit > 65536:
        card['sources'][0]['table'] = 'large_structural_field_' * 4000

    def reply(request):
        packet = json.loads(request.content)
        assert packet['model'] == 'configured-model'
        assert packet['max_completion_tokens'] == output_limit and 'max_tokens' not in packet
        assert packet['response_format'] == {'type': 'json_object'}
        assert packet['stream'] is False
        assert set(request.extensions['timeout'].values()) == {17}
        content = json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'keep'}]})
        return respond((' ' * 140000 if output_limit > 65536 else '') + content)

    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(config, [card], api_key='key')
    assert result['reviewed_patterns'] == result['model_calls'] == len(requests) == 1
    budget = result['effective_model_budget']
    assert budget['max_output_tokens'] == output_limit and budget['max_response_bytes'] == 300000
    assert budget['configuration_source'] == ('agent_config' if external else 'project.model')
    if output_limit > 65536:
        assert len(requests[0].content) > 65536


@pytest.mark.parametrize('failure,retries,limit,expected_order,reviewed,omitted', [
    ('transient_once', 1, 10, [0, 1, 2, 1], 3, 0),
    ('server', 1, 10, [0, 1, 2, 1], 2, 0),
    ('server', 0, 10, [0, 1, 2], 2, 0),
    ('timeout', 1, 10, [0, 1, 2, 1], 2, 0),
    ('invalid_format', 1, 10, [0, 1, 2], 2, 0),
    ('credentials', 1, 10, [0, 1], 1, 1),
    ('server', 1, 3, [0, 1, 2], 2, 0),
])
def test_failed_batch_preserves_success_and_continues_independent_batches_without_changing_model_settings(
        monkeypatch, failure, retries, limit, expected_order, reviewed, omitted):
    cards = [profile(i) for i in range(3)]
    ids = [card['pattern_id'] for card in cards]
    order, payloads = [], []
    def reply(request):
        packet = json.loads(request.content)
        card, = json.loads(packet['messages'][1]['content'])['patterns']
        index = ids.index(card['pattern_id'])
        order.append(index)
        payloads.append(packet)
        assert packet['max_tokens'] == 1024 and packet['response_format']['type'] == 'json_schema'
        assert set(request.extensions['timeout'].values()) == {13}
        if index == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('simulation')
            if failure != 'transient_once' or order.count(1) == 1:
                return httpx.Response({'invalid_format': 400, 'credentials': 403}.get(failure, 503))
        return respond(json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'merge'}]}))
    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(max_output_tokens=1024, retries=retries,
                                     timeout_seconds=13, max_calls=limit), cards, api_key='key')
    assert order == expected_order and result['model_calls'] == len(requests) == len(order)
    assert result['reviewed_patterns'] == reviewed and result['omitted_patterns'] == omitted
    assert result['selected_pattern_ids'] == [ids[i] for i in range(3) if i == 0 or reviewed == 3 or (i == 2 and not omitted)]
    assert result['failed_patterns'] == 3 - reviewed - omitted
    assert result['status'] == ('reviewed' if reviewed == 3 else 'partial')
    if order.count(1) == 2:
        assert payloads[1] == payloads[-1]
        assert result['retry_batches'] == 1
    if failure == 'credentials':
        assert result['batches'][1]['http_status'] == 403


def test_binding_ids_and_single_card_protocol_feedback_preserve_valid_sibling(monkeypatch):
    card, sibling = alignment_card(), profile(8)
    issued_batches = []
    def reply(request):
        packet = json.loads(json.loads(request.content)['messages'][1]['content'])
        issued_batches.append(packet['patterns'])
        current = next(p for p in packet['patterns'] if p['pattern_id'] == card['pattern_id'])
        binding_ids = [choice['binding_id'] for choice in current['binding_choices']]
        assert len(set(binding_ids)) == 2 and all(value.startswith('binding_') for value in binding_ids)
        if len(issued_batches) == 1:
            return respond(json.dumps({'decisions': [
                {'pattern_id': sibling['pattern_id'], 'action': 'merge'},
                {'pattern_id': card['pattern_id'], 'action': 'propose', 'binding_ids': [binding_ids[0], 'not-issued']}]}))
        assert len(packet['patterns']) == 1
        assert current['protocol_feedback']['reason'] == 'unknown_proposal_binding_id'
        original = next(p for p in issued_batches[0] if p['pattern_id'] == card['pattern_id'])
        assert current['binding_choices'] == original['binding_choices']
        return respond(json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'propose',
                                                 'binding_ids': binding_ids[::-1]}]}))
    calls = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), [card, sibling], api_key='key')
    assert len(calls) == 2 and result['reviewed_patterns'] == 2 and result['failed_patterns'] == 0
    assert result['selected_pattern_ids'] == [sibling['pattern_id']] and result['recovered_patterns'] == 1
    assert next(d for d in result['decisions'] if d['pattern_id'] == card['pattern_id'])['alignment'] == [
        {'member_index': i, 'source_index': i, 'role_index': 0} for i in range(2)]


def test_relation_member_ids_are_copied_from_the_issued_card(monkeypatch):
    card = profile(12, eligible=False)
    card.update(kind='relation', basis='same_directed_source_columns')
    card['members'].append(dict(card['members'][0], contributor_id='u2', type='other_relation'))
    card['sources'][0].update(source_key_columns=['id'], target_key_columns=['parent_id'])
    def reply(request):
        issued, = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        ids = [row['member_id'] for row in issued['member_choices']]
        assert len(set(ids)) == 2 and all(mid.startswith('member_') for mid in ids)
        return respond(json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'propose_relation',
                                                 'member_ids': ids[::-1]}]}))
    requests = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), [card], api_key='key')
    assert result['model_calls'] == len(requests) == 1 and result['failed_patterns'] == 0
    assert result['decisions'] == [{'pattern_id': card['pattern_id'], 'action': 'propose_relation', 'member_indexes': [0, 1]}]


@pytest.mark.parametrize('include_usage', [False, True])
def test_configured_stream_and_usage_options_are_preserved(monkeypatch, include_usage):
    from lightgraph.tests.test_model_streaming import streamed, wire
    card = profile()
    def reply(request):
        packet = json.loads(request.content)
        assert packet['stream'] is True
        assert packet.get('stream_options') == ({'include_usage': True} if include_usage else None)
        return streamed(wire({'content': json.dumps({'decisions': [{'pattern_id': card['pattern_id'], 'action': 'keep'}]})}))
    calls = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(stream=True, stream_options_include_usage=include_usage), [card], api_key='key')
    assert result['reviewed_patterns'] == result['model_calls'] == len(calls) == 1
    assert result['batches'][0]['finish_reason'] == 'stop'

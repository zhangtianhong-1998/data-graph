import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.discovery import sdk_transport
from lightgraph.retrieval.mentions import QueryMentions, resolve_mention_extractor
from lightgraph.runtime.config import ModelConfig


def configured(**overrides):
    values = {'mode': 'in_environment', 'base_url': 'https://project-model.example/v1',
              'model': 'project-chat', 'api_key_env': 'LIGHTGRAPH_MENTION_TEST_KEY', **overrides}
    return SimpleNamespace(resolved_model=ModelConfig.model_validate(values))


def test_project_endpoint_only_gets_user_text_and_returns_source_spans(monkeypatch):
    question = '请查维修工段的空气压缩机，限定压力大于5，分别有哪些零件？'
    answer = {'mentions': ['维修工段', '空气压缩机'], 'constraints': ['限定压力大于5'],
              'framing': ['请查'], 'target': '零件', 'intent': 'dimensional'}
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'completion', 'object': 'chat.completion', 'created': 1,
            'model': 'project-chat', 'choices': [{'index': 0, 'finish_reason': 'stop',
                                                   'message': {'role': 'assistant', 'content': json.dumps(answer)}}]})

    complete = sdk_transport.request_completion
    def local(settings, budget, transport, key, payload):
        return complete(settings, budget, httpx.MockTransport(handle), key, payload)

    monkeypatch.setattr(sdk_transport, 'request_completion', local)
    extract = resolve_mention_extractor(configured(), api_key='session-token')
    assert extract(question) == answer
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://project-model.example/v1/chat/completions'
    body = json.loads(request.content)
    assert request.headers['authorization'] == 'Bearer session-token'
    assert body['model'] == 'project-chat'
    assert body['messages'][1:] == [{'role': 'user', 'content': question}]
    assert 'tools' not in body

    json_object = resolve_mention_extractor(configured(response_format='json_object'), api_key='session-token')
    answer.update(constraints=None, framing=None)
    assert json_object(question)['mentions'] == ['维修工段', '空气压缩机']
    assert extract(question) is None
    answer.update(constraints=['限定压力大于5'], framing=[], target='限定压力大于5')
    verified = json_object(question)
    assert verified['target'] is None and verified['constraints'] == ['限定压力大于5']

    answer.update(mentions=['虚构的别名'])
    assert extract(question) is None
    answer.update(mentions=['空气压缩机'], framing=['维修工段的空气压缩机'])
    assert extract(question) is None
    answer.update(mentions=['空气压缩机'], framing=[], node_id='node-not-issued')
    assert extract(question) is None


@pytest.mark.parametrize('overrides', [
    {'mode': 'disabled'}, {'mode': 'metadata_only'}, {'max_calls': 0},
    {'base_url': ''}, {'base_url': 'http://remote.example/v1'},
])
def test_non_permitted_project_model_never_gets_query(overrides):
    assert resolve_mention_extractor(configured(**overrides), api_key='secret') is None


def test_missing_key_and_failed_request_preserve_fallback(monkeypatch):
    monkeypatch.delenv('LIGHTGRAPH_MENTION_TEST_KEY', raising=False)
    assert resolve_mention_extractor(configured()) is None
    monkeypatch.setenv('LIGHTGRAPH_MENTION_TEST_KEY', 'env-token')
    calls = []
    def timeout(settings, budget, transport, key, payload):
        from lightgraph.errors import LightGraphError
        calls.append((settings.timeout_seconds, key))
        raise LightGraphError('model_transport_error')

    monkeypatch.setattr(sdk_transport, 'request_completion', timeout)
    assert resolve_mention_extractor(configured())('非预设的新场景') is None
    assert calls == [(15, 'env-token')]
    assert resolve_mention_extractor(configured(max_prompt_tokens=1))('非预设的新场景') is None
    assert len(calls) == 1


@pytest.mark.parametrize('body', [None, [], {'choices': [None]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': ['malformed']}}]}])
def test_malformed_gateway_response_does_not_interrupt_retrieval(body, monkeypatch):
    complete = sdk_transport.request_completion
    def malformed(settings, budget, transport, key, payload):
        response = httpx.Response(200, content=json.dumps(body), headers={'content-type': 'application/json'})
        return complete(settings, budget, httpx.MockTransport(lambda _: response), key, payload)

    monkeypatch.setattr(sdk_transport, 'request_completion', malformed)
    assert resolve_mention_extractor(configured(), api_key='token')('故障设备') is None


def test_active_spans_and_equivalent_name_hypotheses_are_distinct():
    question = '过去看过天鹅。仅查南区试验中的雪豹，排除灰狐，体重超过十斤。'
    result = QueryMentions(
        mentions=['天鹅', '雪豹', '灰狐'], target=None, constraints=['体重超过十斤'],
        framing=[], intent='search', overview_metric=None,
        scopes=[{'text':'南区试验', 'dimension':'试验', 'value':'南区'}],
        background=['过去看过天鹅'], exclusions=['排除灰狐'],
        equivalents=[{'text':'雪豹', 'names':['snow leopard', 'snow leopard']},
                     {'text':'天鹅', 'names':['swan']}, {'text':'unmentioned', 'names':['invented']}],
    ).verified(question)
    assert result['mentions'] == ['雪豹']
    assert result['equivalents'] == [{'text':'雪豹', 'names':['snow leopard']}]
    assert result['constraints'] == ['体重超过十斤']
    assert result['background'] == ['过去看过天鹅'] and result['exclusions'] == ['排除灰狐']
    assert result['scopes'] == [{'text':'南区试验', 'dimension':'试验', 'value':'南区'}]


def test_scope_model_cannot_select_an_excluded_or_invented_value():
    def parse(value, text):
        return QueryMentions(mentions=[], target=None, constraints=[], framing=[], intent='point',
            overview_metric=None, scopes=[{'text':text, 'dimension':'研究组', 'value':value}],
            background=[], exclusions=['排除甲研究组'], equivalents=[])
    question = '排除甲研究组，只查乙研究组'
    assert parse('甲', '甲研究组').verified(question) is None
    assert parse('甲', '乙研究组').verified(question) is None
    assert parse('乙', '乙研究组').verified(question)['intent'] == 'point'


def test_scope_from_background_kept_when_independently_repeated_in_active_question():
    spans = QueryMentions(mentions=['月蚀'], target=None, constraints=[], framing=[], intent='overview',
        overview_metric='in_degree', scopes=[{'text':'甲研究组', 'dimension':'研究组', 'value':'甲'}],
        background=['以前甲研究组查过月蚀'], exclusions=[], equivalents=[])
    verified = spans.verified('以前甲研究组查过月蚀。这次看甲研究组的月蚀。')
    assert verified['mentions'] == ['月蚀'] and verified['overview_metric'] == 'in_degree'


def test_negation_retained_when_model_duplicates_context_as_framing():
    question = '此前参加过冬季展览。排除已经售出的样品。'
    verified = QueryMentions(mentions=['样品'], target=None, constraints=['排除已经售出的样品'],
        framing=['此前参加过冬季展览'], intent='search', overview_metric=None, scopes=[],
        background=['此前参加过冬季展览'], exclusions=['已经售出的样品'], equivalents=[]).verified(question)
    assert verified['constraints'] == ['排除已经售出的样品'] and not verified['framing']
    assert not verified['mentions']


def test_strict_provider_schema_requires_all_declared_properties():
    schema = QueryMentions.model_json_schema()
    for obj in [schema, *schema['$defs'].values()]:
        assert set(obj['properties']) == set(obj['required'])
        assert obj['additionalProperties'] is False


def test_simple_search_preserves_only_entity_despite_repeated_target():
    verified = QueryMentions(mentions=['番茄'], target='番茄', constraints=[], framing=['请查'],
        intent='search', overview_metric=None, scopes=[], background=[], exclusions=[], equivalents=[]).verified('请查番茄。')
    assert verified['mentions'] == ['番茄'] and verified['target'] is None


def test_coverage_review_requires_complete_disjoint_partition_of_original_spans(monkeypatch):
    question = '那台仪器牵涉到哪些订单，仅限已验收的。'
    spans = ['那台', '牵涉到', '仅限已验收的']
    items = [dict(text=text, role=role, matches_if_removed=effect) for text, role, effect in
             [('那台', 'demonstrative', 'same'), ('牵涉到', 'request_wording', 'same'),
              ('仅限已验收的', 'condition', 'different')]]
    answer = {'items': items}
    calls = []

    def respond(settings, budget, transport, key, payload):
        calls.append(payload)
        return {'choices':[{'message': {'content': json.dumps(answer)}}]}

    monkeypatch.setattr(sdk_transport, 'request_completion', respond)
    extract = resolve_mention_extractor(configured(), api_key='project-token')
    assert extract.review_coverage(question, spans) == ['那台', '牵涉到']
    assert json.loads(calls[0]['messages'][1]['content']) == {'question': question, 'spans': spans}
    assert extract.review_coverage(question, ['未提供的内容']) is None
    assert resolve_mention_extractor(configured(max_calls=1), api_key='x').review_coverage(question, spans) is None
    assert len(calls) == 1
    for bad in [items[:2], [*items, items[2]], [*items[:2], dict(items[2], text='编造的片段')]]:
        answer['items'] = bad
        assert extract.review_coverage(question, spans) is None
    answer['items'] = [items[0], dict(items[1], matches_if_removed='different'), dict(items[2], matches_if_removed='same')]
    assert extract.review_coverage(question, spans) == ['那台']
    answer = items
    assert resolve_mention_extractor(configured(response_format='json_object'), api_key='x').review_coverage(question, spans) == ['那台', '牵涉到']
    assert extract.review_coverage(question, spans) is None


def test_fullwidth_quoted_span_maps_to_its_unique_original_spelling():
    question = '请查设备Ａ－７（备用）。'
    result = QueryMentions(mentions=['设备A-7(备用)'], constraints=[], target=None, framing=['请查'],
        intent='search', overview_metric=None, scopes=[], background=[], exclusions=[], equivalents=[]).verified(question)
    assert result['mentions'] == ['设备Ａ－７（备用）']

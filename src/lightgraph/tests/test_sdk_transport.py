"""SDK wire-path regressions: strict completion, no source-value warnings or hidden retries."""
import json
import logging
import warnings

import httpx
import pytest
from openai.resources.chat.completions import Completions

from lightgraph.discovery.llm import ModelConfig, StructuredModelClient
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet
from lightgraph.tests.test_model_streaming import delta, event, model, streamed, wire

CANARY = 'UNRETAINED_SDK_SOURCE_秘密_4901'


def completion(message, reason='stop'):
    choice = {'message': message}
    if reason is not None:
        choice['finish_reason'] = reason
    return httpx.Response(200, json={'choices': [choice],
                                   'usage': {'prompt_tokens': 30, 'completion_tokens': 20}})


def tool(arguments='{}', call_id='provider-call-exact'):
    return {'content': None, 'tool_calls': [{'id': call_id, 'type': 'function',
        'function': {'name': 'get_metadata', 'arguments': arguments}}]}


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('field', ['content', 'arguments', 'function', 'tool_calls'])
def test_bad_provider_types_have_fixed_errors_and_never_echo_values(stream, field, capsys, caplog):
    message = tool()
    if field == 'content':
        message = {'content': {'untrusted': CANARY}}
    elif field == 'arguments':
        message['tool_calls'][0]['function']['arguments'] = [CANARY]
    elif field == 'function':
        message['tool_calls'][0]['function'] = [CANARY]
    else:
        message['tool_calls'] = {'untrusted': CANARY}
    if stream:
        fragment = message.copy()
        if isinstance(fragment.get('tool_calls'), list):
            fragment['tool_calls'] = [{**c, 'index': i} for i, c in enumerate(fragment['tool_calls'])]
        value = event(delta(fragment)) + event(delta(reason='tool_calls')) + event('[DONE]')
        handler = lambda _: streamed(value)
    else:
        handler = lambda _: completion(message, 'tool_calls')
    client = model(handler, stream=stream)
    toolbox = Toolbox()
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter('always')
        with pytest.raises(LightGraphError, match='model_stream_invalid|model_response_invalid') as error:
            client.explore(input_packet(), toolbox)
    captured = capsys.readouterr()
    assert not emitted
    assert CANARY not in str(error.value) + captured.out + captured.err + caplog.text
    assert toolbox.executed == [] and toolbox.closed
    assert client.usage['calls'] == 1 and client.budget.snapshot()['reserved'] == {}


@pytest.mark.parametrize('reason,code', [(None, 'model_response_incomplete'),
    ('stop', 'model_response_invalid'), ('length', 'model_response_truncated'),
    ('content_filter', 'model_response_refused'), ('unknown', 'model_response_invalid')])
def test_nonstream_incomplete_or_inconsistent_tool_response_cannot_execute(reason, code):
    client = model(lambda _: completion(tool(), reason), stream=False)
    toolbox = Toolbox()
    with pytest.raises(LightGraphError, match=code):
        client.explore(input_packet(), toolbox)
    assert toolbox.executed == [] and toolbox.closed
    assert client.usage['calls'] == 1 and client.budget.snapshot()['reserved'] == {}


@pytest.mark.parametrize('stream', [False, True])
def test_real_sdk_create_preserves_provider_call_id_in_next_request(monkeypatch, stream):
    sdk_calls = []
    original = Completions.create
    def create(self, *args, **kwargs):
        sdk_calls.append(self._client.max_retries)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Completions, 'create', create)
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert 'response_format' not in body and body['tools']
        if len(requests) == 1:
            message, reason = tool(call_id='provider-id-preserved-903'), 'tool_calls'
        else:
            previous = [m for m in body['messages'] if m['role'] == 'assistant'][0]
            returned = [m for m in body['messages'] if m['role'] == 'tool'][0]
            assert previous['tool_calls'][0]['id'] == returned['tool_call_id'] == 'provider-id-preserved-903'
            message, reason = {'content': json.dumps(final(evidence='probe_0001'))}, 'stop'
        return streamed(wire(message)) if stream else completion(message, reason)
    client = model(handler, stream=stream)
    toolbox = Toolbox()
    assert client.explore(input_packet(), toolbox).proposals
    assert sdk_calls == [0, 0] and toolbox.executed == ['get_metadata']
    assert client.usage['calls'] == 2 and client.budget.used.llm_calls == 2


def test_nonstandard_nonstream_object_arguments_are_normalized_without_warnings(capsys, caplog):
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return completion(tool({'query': CANARY}), 'tool_calls')
        body = json.loads(request.content)
        previous = [m for m in body['messages'] if m['role'] == 'assistant'][0]
        assert json.loads(previous['tool_calls'][0]['function']['arguments']) == {'query': CANARY}
        return completion({'content': json.dumps(final(evidence='probe_0001'))})
    toolbox = Toolbox()
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter('always')
        assert model(handler, stream=False).explore(input_packet(), toolbox).proposals
    captured = capsys.readouterr()
    assert not emitted and CANARY not in captured.out + captured.err + caplog.text
    assert toolbox.executed == ['get_metadata']


@pytest.mark.parametrize('stream', [False, True])
def test_duplicate_tool_arguments_are_rejected_before_dispatch(stream):
    message = tool('{"pending":true,"pending":false}')
    handler = lambda _: streamed(wire(message)) if stream else completion(message, 'tool_calls')
    toolbox = Toolbox()
    with pytest.raises(LightGraphError, match='model_json_duplicate_key'):
        model(handler, stream=stream).explore(input_packet(), toolbox)
    assert toolbox.executed == []


def test_sdk_does_not_retry_outside_project_budget(capsys, caplog):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503, text=CANARY)
    client = model(handler, stream=False, retries=1)
    with pytest.raises(LightGraphError, match='model_http_error') as error:
        client.explore(input_packet(), Toolbox())
    captured = capsys.readouterr()
    assert len(calls) == client.usage['calls'] == client.budget.used.llm_calls == 2
    assert client.budget.snapshot()['reserved'] == {}
    assert CANARY not in str(error.value) + captured.out + captured.err + caplog.text


def test_debug_source_logging_is_rejected_before_sdk_request(caplog):
    with caplog.at_level(logging.DEBUG, logger='openai._base_client'):
        client = model(lambda _: pytest.fail('logging policy must reject before transport'), stream=False)
        with pytest.raises(LightGraphError, match='model_source_logging_forbidden'):
            client.explore(input_packet(), Toolbox())

"""OpenAI SDK transport with project limits; no wire protocol is reimplemented.

The pinned SDK owns SSE decoding and delta accumulation. These adapters enforce
completion, size and deadline policy before a model message can reach any tool.
"""
from __future__ import annotations

import logging

import httpx
import openai
from openai._streaming import SSEDecoder, _SSELineDecoder
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletionChunk

from lightgraph.errors import LightGraphError


def stream_transport_limit(max_response_bytes, max_output_tokens):
    return 6 * max_response_bytes + 2048 * max_output_tokens + 65536


class _BoundedLines(_SSELineDecoder):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def _append(self, chunk, start, end):
        if len(self._buffer) + end - start > self.limit:
            raise LightGraphError('model_stream_event_too_large')
        super()._append(chunk, start, end)


class _CompleteDecoder(SSEDecoder):
    """Use SDK framing, but drain and validate the transport before exposing DONE."""
    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self.event_bytes = 0
        self.done = False

    def decode(self, line):
        self.event_bytes += len(line.encode()) + 1
        if self.event_bytes > self.limit:
            raise LightGraphError('model_stream_event_too_large')
        event = super().decode(line)
        if not line or line.startswith(':'):
            self.event_bytes = 0 if not self._data else self.event_bytes
        return event

    def iter_bytes(self, iterator):
        lines = _BoundedLines(self.limit)
        terminal = None
        def checked(events):
            nonlocal terminal
            for event in events:
                if self.done:
                    raise LightGraphError('model_stream_invalid')
                if event.data == '[DONE]':
                    self.done = True
                    terminal = event
                else:
                    yield event
        try:
            for chunk in iterator:
                yield from checked(self._decode_lines(lines.feed(chunk)))
            yield from checked(self._decode_lines(lines.finish()))
            # The final event may lack the usual blank line.
            yield from checked(self._decode_lines(iter([b''])))
            if terminal is not None:
                yield terminal
        finally:
            self._reset()


class _LimitedBytes(httpx.SyncByteStream):
    def __init__(self, stream, budget, limit, streaming):
        self.stream, self.budget, self.limit = stream, budget, limit
        self.streaming = streaming

    def __iter__(self):
        received = 0
        for chunk in self.stream:
            self.budget.check_deadline()
            received += len(chunk)
            if received > self.limit:
                raise LightGraphError('model_stream_transport_too_large' if self.streaming
                                      else 'model_response_too_large')
            yield chunk
        self.budget.check_deadline()

    def close(self):
        self.stream.close()


class _LimitedTransport(httpx.BaseTransport):
    def __init__(self, transport, budget, limit, streaming, strict):
        self.transport = transport or httpx.HTTPTransport()
        self.budget, self.limit, self.streaming, self.strict = budget, limit, streaming, strict

    def handle_request(self, request):
        response = self.transport.handle_request(request)
        self.budget.check_deadline()
        if response.status_code != 200:
            status = response.status_code
            response.close()
            if self.strict and status in (400, 422):
                raise LightGraphError('model_structured_output_rejected')
            raise LightGraphError('model_rate_limited' if status == 429 else 'model_http_error',
                                 retryable=status in (429, 500, 502, 503, 504))
        if response.headers.get('content-encoding', 'identity') != 'identity':
            response.close()
            raise LightGraphError('model_content_encoding_unsupported')
        if response.is_stream_consumed and len(response.content) > self.limit:
            response.close()
            raise LightGraphError('model_stream_transport_too_large' if self.streaming else 'model_response_too_large')
        response.stream = _LimitedBytes(response.stream, self.budget, self.limit, self.streaming)
        return response

    def close(self):
        self.transport.close()


class _CompletionClient(openai.OpenAI):
    def __init__(self, *, event_limit, **kwargs):
        self.completion_decoder = _CompleteDecoder(event_limit)
        super().__init__(**kwargs)

    def _make_sse_decoder(self):
        return self.completion_decoder


def _actionable_message(message):
    """Exclude provider reasoning and other auxiliary fields from application state."""
    if not isinstance(message, dict) or not isinstance(message.get('content'), (str, type(None))):
        raise LightGraphError('model_response_invalid')
    result = {'role': 'assistant', 'content': message.get('content')}
    if message.get('refusal'):
        raise LightGraphError('model_response_refused')
    calls = message.get('tool_calls')
    if calls is not None:
        if not isinstance(calls, list) or len(calls) > 64:
            raise LightGraphError('model_response_invalid')
        result['tool_calls'] = []
        ids = set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type', 'function') != 'function':
                raise LightGraphError('model_response_invalid')
            function, call_id = call.get('function'), call.get('id')
            if (not isinstance(function, dict) or not isinstance(call_id, str) or not call_id
                    or call_id in ids or not isinstance(function.get('name'), str) or not function['name']
                    or not isinstance(function.get('arguments'), (str, dict))):
                raise LightGraphError('model_response_invalid')
            ids.add(call_id)
            result['tool_calls'].append({'id': call_id, 'type': 'function', 'function': {
                'name': function['name'], 'arguments': function['arguments']}})
    return result


def _sdk_dict(value):
    """SDK response construction is permissive; serialization must never echo values."""
    if not callable(getattr(value, 'model_dump', None)):
        raise LightGraphError('model_response_invalid')
    # Malformed compatible gateways can leave dicts in typed string fields.
    # Pydantic's default serializer warnings include their raw input values.
    return value.model_dump(exclude_unset=True, warnings=False)


def _complete_envelope(response):
    envelope = _sdk_dict(response)
    choices = envelope.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise LightGraphError('model_response_invalid')
    choice = choices[0]
    reason = choice.get('finish_reason')
    if reason is None:
        raise LightGraphError('model_response_incomplete')
    if reason not in ('stop', 'tool_calls', 'length', 'content_filter') or choice.get('index', 0) != 0:
        raise LightGraphError('model_response_invalid')
    if reason == 'length':
        raise LightGraphError('model_response_truncated')
    if reason == 'content_filter':
        raise LightGraphError('model_response_refused')
    message = _actionable_message(choice.get('message'))
    if bool(message.get('tool_calls')) != (reason == 'tool_calls'):
        raise LightGraphError('model_response_invalid')
    choice['message'] = message
    return envelope


def _actionable_delta(delta):
    """Validate fragments before byte accounting or SDK accumulation touches them."""
    if not isinstance(delta, dict):
        raise LightGraphError('model_stream_invalid')
    if delta.get('refusal'):
        raise LightGraphError('model_response_refused')
    if not isinstance(delta.get('content'), (str, type(None))):
        raise LightGraphError('model_stream_invalid')
    if delta.get('role') not in (None, 'assistant'):
        raise LightGraphError('model_stream_invalid')
    calls = delta.get('tool_calls')
    if calls is not None:
        if not isinstance(calls, list) or len(calls) > 64:
            raise LightGraphError('model_stream_invalid')
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get('id'), (str, type(None))):
                raise LightGraphError('model_stream_invalid')
            function = call.get('function', {})
            if not isinstance(function, dict) or any(
                    not isinstance(function.get(k), (str, type(None))) for k in ('name', 'arguments')):
                raise LightGraphError('model_stream_invalid')
    return {k: v for k, v in delta.items() if k in ('role', 'content', 'tool_calls')}


def request_completion(config, budget, transport, key, payload):
    """Exactly one SDK request, with no SDK retries, repair or semantic fallback."""
    if any(logging.getLogger(name).isEnabledFor(logging.DEBUG)
           for name in ('openai', 'openai._base_client')):
        raise LightGraphError('model_source_logging_forbidden')
    streaming = config.stream
    wire_limit = (stream_transport_limit(config.max_response_bytes, config.max_output_tokens)
                  if streaming else config.max_response_bytes)
    bounded = _LimitedTransport(transport, budget, wire_limit, streaming,
                                payload.get('response_format', {}).get('type') == 'json_schema')
    http = httpx.Client(transport=bounded, follow_redirects=False, trust_env=False,
                       timeout=min(config.timeout_seconds, budget.remaining_seconds()))
    try:
        with _CompletionClient(api_key=key, base_url=config.base_url, max_retries=0,
                               http_client=http, event_limit=6 * config.max_response_bytes + 65536,
                               default_headers={'Accept-Encoding': 'identity'}) as sdk:
            response = sdk.chat.completions.create(**payload)
            if not streaming:
                return _complete_envelope(response)
            state = ChatCompletionStreamState()
            finish = None
            content_bytes = 0
            seen_calls = set()
            initialized = False
            with response:
                for chunk in response:
                    budget.check_deadline()
                    raw = _sdk_dict(chunk)
                    choices = raw.get('choices')
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise LightGraphError('model_stream_invalid')
                    if choices:
                        choice = choices[0]
                        if not isinstance(choice, dict) or choice.get('index', 0) != 0 or finish is not None:
                            raise LightGraphError('model_stream_invalid')
                        delta = choice.get('delta', {})
                        clean = _actionable_delta(delta)
                        if not initialized:
                            clean.setdefault('role', 'assistant')
                        content_bytes += len((clean.get('content') or '').encode())
                        for call in clean.get('tool_calls') or []:
                            index = call.get('index')
                            if type(index) is not int or not 0 <= index < 64:
                                raise LightGraphError('model_stream_invalid')
                            seen_calls.add(index)
                            if call.get('type', 'function') != 'function':
                                raise LightGraphError('model_stream_invalid')
                            content_bytes += len((call.get('id') or '').encode())
                            content_bytes += sum(len((call.get('function', {}).get(k) or '').encode())
                                                 for k in ('name', 'arguments'))
                        if content_bytes > config.max_response_bytes:
                            raise LightGraphError('model_response_too_large')
                        reason = choice.get('finish_reason')
                        if reason is not None and reason not in ('stop', 'tool_calls', 'length', 'content_filter'):
                            raise LightGraphError('model_stream_invalid')
                        finish = reason
                        choice['delta'] = clean
                    # SDK accumulation handles all content/tool argument fragments.
                    # Envelope metadata defaults accommodate sparse compatible gateways.
                    raw = {'id': '', 'created': 0, 'model': config.model,
                           'object': 'chat.completion.chunk', **raw}
                    if isinstance(raw.get('usage'), dict):
                        raw['usage'].setdefault('total_tokens', sum(raw['usage'].get(k, 0) for k in ('prompt_tokens', 'completion_tokens')))
                    for _ in state.handle_chunk(ChatCompletionChunk.model_validate(raw)):
                        pass
                    initialized = True
            if not sdk.completion_decoder.done or finish is None:
                raise LightGraphError('model_stream_incomplete')
            if (seen_calls and finish == 'stop' or not seen_calls and finish == 'tool_calls'
                    or sorted(seen_calls) != list(range(len(seen_calls)))):
                raise LightGraphError('model_stream_invalid')
            envelope = _sdk_dict(state.current_completion_snapshot)
            message = _actionable_message(envelope['choices'][0]['message'])
            if any(not c['id'] or not c['function']['name'] for c in message.get('tool_calls', [])):
                raise LightGraphError('model_stream_invalid')
            return {'choices': [{'message': message, 'finish_reason': finish}],
                    'usage': envelope.get('usage') or {}}
    except openai.APIConnectionError as exc:
        if isinstance(exc.__cause__, LightGraphError):
            raise exc.__cause__ from None
        raise LightGraphError('model_transport_error', retryable=True) from None
    except openai.APIError:
        raise LightGraphError('model_transport_error', retryable=True) from None

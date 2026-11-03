"""Protocol failures stay failures while retaining value-free failure locations."""
from copy import deepcopy
import json

import httpx
import pytest

from lightgraph.discovery.llm import MODEL_PROTOCOL_CODES, MODEL_PROTOCOL_STAGES
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_discovery_agent import client, input_packet, response, Toolbox
from lightgraph.tests.test_schema_protocol_correction import (
    malformed_hierarchy, corrected_hierarchy, legacy_batch)


CANARY = 'REJECTED_KEY_AND_VALUE_秘密_9274'


def diagnostics(model):
    return {tuple((item['stage'], tuple(item['field']), item['code'])): count
            for encoded, count in model.usage['model_protocol_diagnostics'].items()
            for item in [json.loads(encoded)]}


def test_schema_failures_retain_both_attempts_without_rejected_values(monkeypatch, capsys, caplog):
    raw = malformed_hierarchy()
    raw['proposals'][0]['claim'][CANARY] = CANARY
    model = client(monkeypatch, lambda request: response(raw))
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    observed = diagnostics(model)
    assert observed[('response_schema',
        ('proposals', 0, 'wide_hierarchy', 'claim', '<unknown_field>'), 'extra_forbidden')] == 4
    assert model.usage['calls'] == 2 and model.usage['schema_repair_attempts'] == 1
    assert model.usage.get('schema_repairs_succeeded', 0) == 0
    assert CANARY not in json.dumps(model.usage) + capsys.readouterr().out + caplog.text
    assert all(stage in MODEL_PROTOCOL_STAGES and code in MODEL_PROTOCOL_CODES
               for stage, _, code in observed)


@pytest.mark.parametrize('usage,field,code', [
    ([], ('usage',), 'dict_type'),
    ({'prompt_tokens': -1, 'completion_tokens': 20}, ('usage', 'prompt_tokens'), 'invalid_usage'),
    ({'prompt_tokens': 30, 'completion_tokens': True}, ('usage', 'completion_tokens'), 'invalid_usage'),
])
def test_malformed_usage_is_distinct_from_output_schema(monkeypatch, usage, field, code):
    body = response({'task': 'T1_table_discovery', 'abstain': True}).json()
    body['usage'] = usage
    model = client(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert diagnostics(model) == {('provider_usage', field, code): 2}
    assert model.usage['calls'] == 2 and model.usage['request_retry_exhausted'] == 1


def test_invalid_provider_envelope_has_its_own_stage(monkeypatch):
    model = client(monkeypatch, lambda request: httpx.Response(200, json={
        'choices': [], CANARY: CANARY}))
    with pytest.raises(LightGraphError, match='model_response_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert diagnostics(model) == {('provider_envelope', (), 'model_response_invalid'): 2}
    assert model.usage['request_retry_exhausted'] == 1
    assert CANARY not in json.dumps(model.usage)


def test_bad_final_json_retains_location_after_failed_repair(monkeypatch):
    body = response({'task': 'T1_table_discovery', 'abstain': True}).json()
    body['choices'][0]['message']['content'] = CANARY
    model = client(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert diagnostics(model) == {('response_json', (), 'json_invalid'): 2}
    assert model.usage['calls'] == 2 and CANARY not in json.dumps(model.usage)


@pytest.mark.parametrize('repair', ['tool_call', 'changed_claim'])
def test_repair_contract_failure_is_distinct_from_initial_schema_failure(monkeypatch, repair):
    original = malformed_hierarchy()
    changed = corrected_hierarchy(deepcopy(original))
    changed['proposals'].pop()  # A valid JSON correction must not drop exclusions.
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(original)
        return response(tool='get_metadata') if repair == 'tool_call' else response(changed)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    observed = diagnostics(model)
    assert observed[('response_schema',
        ('proposals', 0, 'wide_hierarchy', 'claim', '<unknown_field>'), 'extra_forbidden')] == 1
    expected = (('tool_calls',), 'tool_calls_forbidden') if repair == 'tool_call' else ((), 'meaning_changed')
    assert observed[('repair_contract', *expected)] == 1
    assert model.usage['calls'] == 2 and model.usage.get('schema_repairs_succeeded', 0) == 0

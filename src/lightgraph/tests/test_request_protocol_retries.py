"""Failed requests retry in place; accepted meaning and repair locks survive."""
import json
from copy import deepcopy

import httpx
import pytest

from lightgraph.contracts import StrictModel, canonical
from lightgraph.errors import LightGraphError
from lightgraph.discovery.semantics import review_semantics
from lightgraph.discovery.models import ProposalBatch
from lightgraph.tests.test_discovery_agent import client, input_packet, response, Toolbox
from lightgraph.tests.test_json_syntax_recovery import wire
from lightgraph.tests.test_schema_protocol_correction import (
    invalid_slot_batch, legacy_batch)


class PatchResult(StrictModel):
    count: int


@pytest.mark.parametrize('failure', ['json', 'transport'])
def test_disabling_schema_repair_keeps_request_retries(monkeypatch, failure):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return wire('{"count":') if failure == 'json' else httpx.Response(503)
        assert requests[0] == requests[1]
        return response({'count': 1})
    model = client(monkeypatch, handler)
    result = model.complete_reference_patch({}, PatchResult)
    assert result.count == 1
    assert model.usage['request_retry_attempts'] == model.usage['request_retry_succeeded'] == 1
    assert not model.usage.get('schema_repair_attempts')
    assert not model.usage.get('request_retry_exhausted')


def test_disabling_schema_repair_still_rejects_parseable_schema_error(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return response({'count': 'invalid'})
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='semantic_review_patch_invalid'):
        model.complete_reference_patch({}, PatchResult)
    assert calls == [1] and not model.usage.get('request_retry_attempts')


def test_unreadable_review_request_can_retry_without_voting_twice(monkeypatch):
    requests = []
    def handler(request):
        body = json.loads(request.content); requests.append(body)
        if len(requests) == 1:
            return wire('{"decision":')
        assert requests[0] == requests[1]
        packet = json.loads(body['messages'][1]['content'])['input']
        return response({'proposition_ref': packet['semantic_proposition_ref'],
            'decision': 'supported', 'basis': 'data_inference',
            'evidence': [{'reference': 'probe_1', 'stance': 'supports'}], 'conditions': []})
    model = client(monkeypatch, handler)
    assert review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]},
        {'kind': 'wide_hierarchy'}, [])
    assert model.usage['request_retry_succeeded'] == 1
    assert not model.usage.get('schema_repair_attempts')


@pytest.mark.parametrize('failure', ['json', 'transport'])
@pytest.mark.parametrize('change', [False, True])
def test_failed_repair_request_keeps_its_original_anchor(monkeypatch, failure, change):
    original, corrected, slot = invalid_slot_batch()
    final = deepcopy(corrected)
    if change:final['proposals'].pop(1)  # The already parsed exclusion must survive.
    requests = []
    def handler(request):
        body = json.loads(request.content); requests.append(body)
        if len(requests) == 1:return response(original)
        correction = json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
        assert correction['allowed_corrections'] == [slot]
        assert json.loads(correction['previous_candidate']) == original
        assert 'tools' not in body
        if len(requests) == 2:
            return wire('{"proposals":') if failure == 'json' else httpx.Response(503)
        assert requests[1] == requests[2]
        return response(final)
    model = client(monkeypatch, handler)
    model.config.retries = 2  # Exercise one request retry inside an existing repair.
    if change:
        with pytest.raises(LightGraphError, match='llm_schema_invalid'):
            legacy_batch(model, input_packet(), Toolbox())
        assert not model.usage.get('request_retry_succeeded')
        assert 'meaning_changed' in canonical(model.usage['model_protocol_diagnostics'])
    else:
        result = legacy_batch(model, input_packet(), Toolbox())
        assert result.model_dump() == ProposalBatch.model_validate(corrected).model_dump()
        assert model.usage['request_retry_succeeded'] == 1
    assert len(requests) == 3 and model.usage['request_retry_attempts'] == 1
    assert model.budget.snapshot()['reserved'] == {}


def test_unverifiable_parsed_candidate_is_not_called_meaning_change(monkeypatch):
    original, corrected, _ = invalid_slot_batch()
    original['unrecognized_condition'] = 'PRIVATE_CONDITION_724'
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    histogram = canonical(model.usage['model_protocol_diagnostics'])
    assert 'comparison_unavailable' in histogram and 'meaning_changed' not in histogram
    assert 'PRIVATE_CONDITION_724' not in histogram
    assert not model.usage.get('request_retry_attempts')

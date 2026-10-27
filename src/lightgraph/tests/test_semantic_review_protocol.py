"""Short review bindings and bounded repair never bypass semantic rejection."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.semantics import SemanticReview, review_semantics
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_semantic_decisions import review_fixture


def reference_response(packet, **overrides):
    return {'proposition_ref': packet['semantic_proposition_ref'],
        'decision': 'supported', 'basis': 'data_inference',
        'evidence': [{'reference': 'probe_1', 'stance': 'supports'}], **overrides}


def test_five_level_claim_is_reviewed_by_reference_without_model_echo(monkeypatch):
    claim = {'kind': 'wide_hierarchy', 'claim': {
        'levels': [{'position': i, 'key': f'L{i}_CODE', 'label': f'L{i}_CN_NAME',
                    'alias': f'L{i}_EN_NAME'} for i in range(1, 6)],
        'namespace_columns': ['RESPONSIBILITY_CENTER'], 'repeat_policy': 'none',
        'gap_policy': 'split', 'identity_scope': 'namespace_local'}}
    original = deepcopy(claim)
    seen = []
    def handler(request):
        _, packet = input_packet(request); seen.append(packet)
        assert packet['semantic_proposition'] == original
        assert packet['semantic_proposition_ref'].startswith('proposition_')
        return response(reference_response(packet))
    model = client(monkeypatch, handler)
    refs = review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, claim, [])
    assert refs == ['semantic_review:data_inference', 'probe_1']
    assert len(seen) == model.usage['calls'] == 1 and claim == original


def test_omitted_legacy_default_gets_one_directed_contract_correction(monkeypatch):
    seen = []
    proposition = {'predicate': 'parent_of', 'source': 'L1', 'target': 'L2',
                   'conditions': {}, 'gap_policy': 'split'}
    def handler(request):
        _, packet = input_packet(request); seen.append(packet)
        if len(seen) == 1:
            echo = dict(packet['semantic_proposition']); echo.pop('gap_policy')
            return response({'proposition': echo, 'decision': 'supported', 'basis': 'data_inference',
                             'evidence': [{'reference': 'probe_1', 'stance': 'supports'}]})
        repair = packet['semantic_review_repair']
        assert repair['error_code'] == 'semantic_review_proposition_mismatch'
        assert repair['allowed_evidence_refs'] == ['probe_1']
        assert packet['semantic_proposition'] == proposition
        assert packet['semantic_proposition_ref'] == seen[0]['semantic_proposition_ref']
        return response(reference_response(packet))
    model = client(monkeypatch, handler)
    assert review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, proposition, [])
    assert len(seen) == model.usage['calls'] == 2


@pytest.mark.parametrize('change,code', [
    (lambda answer, packet: answer.update(proposition_ref='another-proposition'),
     'semantic_review_reference_mismatch'),
    (lambda answer, packet: answer.update(proposition={'source': 'B', 'target': 'A'}),
     'semantic_review_proposition_mismatch'),
    (lambda answer, packet: answer.pop('proposition_ref'),
     'semantic_review_proposition_missing'),
])
def test_reference_never_accepts_wrong_binding_or_conflicting_echo(change, code):
    calls = []
    def complete(task, packet, schema):
        calls.append(packet['semantic_proposition_ref'])
        answer = reference_response(packet); change(answer, packet)
        return schema.model_validate(answer)
    with pytest.raises(LightGraphError, match=code):
        review_semantics(SimpleNamespace(complete=complete),
            {'probe_digest': [{'probe_id': 'probe_1'}]}, {'source': 'A', 'target': 'B'}, [])
    assert len(calls) == 2 and calls[0] == calls[1]


def test_a_previous_review_reference_cannot_be_replayed_for_reversed_endpoints():
    previous_ref = None
    calls = []
    def complete(task, packet, schema):
        nonlocal previous_ref
        calls.append(packet['semantic_proposition_ref'])
        previous_ref = previous_ref or packet['semantic_proposition_ref']
        return schema.model_validate(reference_response(packet, proposition_ref=previous_ref))
    model = SimpleNamespace(complete=complete)
    packet = {'probe_digest': [{'probe_id': 'probe_1'}]}
    assert review_semantics(model, packet, {'source': 'A', 'target': 'B'}, [])
    with pytest.raises(LightGraphError, match='semantic_review_reference_mismatch'):
        review_semantics(model, packet, {'source': 'B', 'target': 'A'}, [])
    assert len(calls) == 3 and calls[0] != calls[1] == calls[2]


@pytest.mark.parametrize('override,code', [
    ({'decision': 'refuted'}, 'semantic_claim_refuted'),
    ({'decision': 'unresolved'}, 'semantic_claim_unresolved'),
    ({'evidence': [{'reference': 'probe_1', 'stance': 'refutes'}]}, 'semantic_claim_refuted'),
    ({'conditions': [{'state': 'unknown', 'evidence_refs': ['probe_1']}]}, 'semantic_condition_unresolved'),
    ({'conditions': [{'state': 'unsatisfied', 'evidence_refs': ['probe_1']}]}, 'semantic_condition_unresolved'),
])
def test_negative_or_undecided_review_is_not_retried_until_positive(override, code):
    calls = []
    def complete(task, packet, schema):
        calls.append(packet)
        # A malformed binding does not turn a denial into a repairable failure.
        return schema.model_validate(reference_response(packet, proposition_ref='wrong', **override))
    with pytest.raises(LightGraphError, match=code):
        review_semantics(SimpleNamespace(complete=complete),
            {'probe_digest': [{'probe_id': 'probe_1'}]}, {'predicate': 'parent_of'}, [])
    assert len(calls) == 1


def test_repair_must_still_classify_all_documents_and_reject_denial():
    docs, packet, proposition, _ = review_fixture(); calls = []
    def complete(task, payload, schema):
        calls.append(payload['semantic_proposition_ref'])
        answer = reference_response(payload)
        if len(calls) == 2:
            assert payload['semantic_review_repair']['required_document_refs'] == ['context@1:rules']
            answer['evidence'].append({'reference': 'context@1:rules', 'stance': 'refutes'})
        return schema.model_validate(answer)
    with pytest.raises(LightGraphError, match='semantic_claim_refuted'):
        review_semantics(SimpleNamespace(complete=complete), packet, proposition, docs)
    assert len(calls) == 2


def test_contract_correction_uses_existing_call_budget(monkeypatch):
    calls = []
    def handler(request):
        _, packet = input_packet(request); calls.append(packet)
        return response(reference_response(packet, proposition_ref='wrong'))
    model = client(monkeypatch, handler)
    model.budget = BudgetAllocator(Cost(llm_calls=1, llm_tokens=1000000, seconds=60))
    with pytest.raises(BudgetExceeded, match='llm_calls_budget_exhausted'):
        review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, {'predicate': 'parent_of'}, [])
    assert len(calls) == model.usage['calls'] == 1
    assert model.budget.snapshot()['reserved'] == {}


def test_bad_review_text_is_not_reflected_in_repair_or_accepted_result():
    marker = 'PRIVATE_BAD_REVIEW_TEXT'; calls = []
    def complete(task, packet, schema):
        calls.append(deepcopy(packet))
        answer = reference_response(packet)
        if len(calls) == 1:
            answer['proposition_ref'] = marker
            answer['evidence'][0]['explanation'] = marker
        return schema.model_validate(answer)
    refs = review_semantics(SimpleNamespace(complete=complete),
        {'probe_digest': [{'probe_id': 'probe_1'}]}, {'predicate': 'parent_of'}, [])
    assert marker not in str(calls) + str(refs)
    assert len(calls) == 2

"""Schema correction carries ephemeral feedback without retrying away denials."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from lightgraph.contracts import canonical
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.semantics import review_semantics
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import client, input_packet, response, Toolbox


CANARY = 'UNKNOWN_FIELD_SOURCE_VALUE_4926'


def legacy_batch(model, packet, tools):
    """Exercise the legacy batch transport; AgentScope now submits one draft per tool."""
    try:
        return model.complete(packet['task'], packet, ProposalBatch)
    finally:
        tools.close()



def malformed_hierarchy():
    return {'task': 'T1_table_discovery', 'proposals': [{'kind': 'wide_hierarchy',
        'claim': {'group_id': 'tree', 'levels': [{'position': 1, 'key': 'c_a'},
            {'position': 2, 'key': 'c_b'}], 'identity_evidence': ['c_a'], 'order_evidence': ['c_b']},
        'evidence_cited': ['c_a', 'c_b']}, {'kind': 'exclusion',
        'claim': {'exclusions': [{'column_id': 'c_b', 'reason': 'out_of_goal', 'affects': ['projection']}]},
        'evidence_cited': ['c_b']}]}


def corrected_hierarchy(raw):
    result = deepcopy(raw)
    claim = result['proposals'][0]['claim']
    claim['ordering_evidence'] = claim.pop('order_evidence')
    return result


def test_extra_fields_get_directed_correction_preserving_candidate_and_budget(monkeypatch, tmp_path, capsys, caplog):
    raw = malformed_hierarchy(); original = deepcopy(raw)
    calls = []; reservations = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) == 1:
            return response(raw)
        feedback = json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
        assert json.loads(feedback['previous_candidate']) == original
        assert feedback['diagnostics'] == [
            {'code': 'extra_forbidden', 'field': ['proposals', 0, 'wide_hierarchy', 'claim', '<unknown_field>']}]
        assert 'Do not silently delete' in feedback['instruction']
        assert 'tools' not in body
        return response(corrected_hierarchy(raw))
    model = client(monkeypatch, handler)
    reserve = model.budget.reserve
    def record_reservation(identifier, cost):
        reservations.append(cost); reserve(identifier, cost)
    model.budget.reserve = record_reservation
    tools = Toolbox()
    result = legacy_batch(model, input_packet(), tools)
    assert result == ProposalBatch.model_validate(corrected_hierarchy(original))
    assert raw == original and tools.closed and not tools.executed
    assert len(calls) == model.usage['calls'] == 2
    assert model.usage['failures'] == model.usage['schema_repair_attempts'] == model.usage['schema_repairs_succeeded'] == 1
    assert model.budget.used.llm_calls == 2 and model.budget.used.llm_tokens == 100
    assert reservations[1].llm_tokens > reservations[0].llm_tokens
    assert model.budget.snapshot()['reserved'] == {}
    assert set(vars(model)) == {'config', 'budget', 'transport', 'usage'}
    assert not list(tmp_path.iterdir()) and not capsys.readouterr().out and not caplog.text


def test_unknown_field_values_are_absent_from_diagnostics_and_failures(monkeypatch, tmp_path, capsys, caplog):
    raw = malformed_hierarchy(); raw['proposals'][0]['claim'][CANARY] = CANARY
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(1)
        if len(calls) == 2:
            feedback = json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
            assert CANARY in feedback['previous_candidate']  # Only this transient request retains it.
            assert CANARY not in json.dumps(feedback['diagnostics'])
        return response(raw)
    model = client(monkeypatch, handler); tools = Toolbox()
    with pytest.raises(LightGraphError, match='llm_schema_invalid') as caught:
        legacy_batch(model, input_packet(), tools)
    assert len(calls) == 2 and tools.closed
    assert CANARY not in str(caught.value) + json.dumps(model.usage) + capsys.readouterr().out + caplog.text
    assert not list(tmp_path.iterdir()) and model.budget.snapshot()['reserved'] == {}


@pytest.mark.parametrize('change,code', [
    ({'decision': 'refuted'}, 'semantic_claim_refuted'),
    ({'decision': 'unresolved'}, 'semantic_claim_unresolved'),
    ({'evidence': [{'reference': 'probe_1', 'stance': 'refutes'}]}, 'semantic_claim_refuted'),
    ({'evidence': {'reference': 'probe_1', 'stance': 'refutes'}}, 'semantic_claim_refuted'),
    ({'conditions': [{'state': 'unknown', 'evidence_refs': ['probe_1']}]}, 'semantic_condition_unresolved'),
    ({'conditions': [{'state': 'unsatisfied', 'evidence_refs': ['probe_1']}]}, 'semantic_condition_unresolved'),
    ({'conditions': [{'state': 'satisfied', 'evidence_refs': []}]}, 'semantic_condition_unresolved'),
])
def test_schema_invalid_negative_review_is_terminal_through_outer_review(monkeypatch, change, code):
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(1)
        packet = json.loads(body['messages'][1]['content'])['input']
        raw = {'proposition_ref': packet['semantic_proposition_ref'], 'decision': 'supported',
            'basis': 'data_inference', 'evidence': [{'reference': 'probe_1', 'stance': 'supports'}],
            CANARY: CANARY, **change}
        # A second model reply would be positive; it must never be requested.
        if len(calls) > 1: raw.pop(CANARY); raw.update(decision='supported', conditions=[],
            evidence=[{'reference': 'probe_1', 'stance': 'supports'}])
        return response(raw)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match=code):
        review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, {'kind': 'wide_hierarchy'}, [])
    assert len(calls) == model.usage['calls'] == 1
    assert 'schema_repair_attempts' not in model.usage and model.budget.snapshot()['reserved'] == {}


def test_schema_correction_cannot_execute_a_tool(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return response(malformed_hierarchy()) if len(calls) == 1 else response(tool='get_metadata')
    model = client(monkeypatch, handler); tools = Toolbox()
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), tools)
    assert len(calls) == 2 and not tools.executed and tools.closed


@pytest.mark.parametrize('constraint', ['calls', 'prompt', 'tokens'])
def test_schema_correction_stays_within_original_limits(monkeypatch, constraint):
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(1)
        prompt_bytes = len(canonical({'messages': body['messages'], 'tools': body.get('tools', []),
                                     'response_format':body['response_format']}).encode())
        if constraint == 'prompt': model.config.max_prompt_bytes = prompt_bytes + 100
        if constraint == 'tokens':
            model.budget.limits = replace(model.budget.limits,
                llm_tokens=prompt_bytes + model.config.max_output_tokens + 100)
        return response(malformed_hierarchy())
    model = client(monkeypatch, handler); tools = Toolbox()
    if constraint == 'calls': model.budget = BudgetAllocator(replace(model.budget.limits, llm_calls=1))
    code = {'calls': 'llm_calls_budget_exhausted', 'prompt': 'model_packet_too_large',
            'tokens': 'llm_tokens_budget_exhausted'}[constraint]
    with pytest.raises((LightGraphError, BudgetExceeded), match=code):
        legacy_batch(model, input_packet(), tools)
    assert len(calls) == model.usage['calls'] == 1 and tools.closed
    assert model.budget.snapshot()['reserved'] == {}


def test_schema_correction_cannot_discard_unknown_review_meaning(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        packet = json.loads(body['messages'][1]['content'])['input']
        answer = {'proposition_ref': packet['semantic_proposition_ref'], 'decision': 'supported',
            'basis': 'data_inference', 'evidence': [{'reference': 'probe_1', 'stance': 'supports'}]}
        if len(calls) == 1: answer['unregistered_condition'] = CANARY
        else:
            assert calls[0]['messages'] == body['messages'][:-1]
            assert json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
        return response(answer)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='semantic_claim_unresolved') as caught:
        review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, {'kind': 'wide_hierarchy'}, [])
    assert CANARY not in str(caught.value)
    assert len(calls) == 2 and model.usage.get('schema_repairs_succeeded', 0) == 0


@pytest.mark.parametrize('change', ['drop_exclusion', 'drop_hierarchy', 'rename_group', 'change_identity',
                                   'drop_evidence', 'add_namespace', 'change_exclusion'])
@pytest.mark.parametrize('entrypoint', ['explore', 'propose'])
def test_schema_correction_cannot_change_claims_or_valid_siblings(monkeypatch, change, entrypoint):
    original = malformed_hierarchy()
    corrected = corrected_hierarchy(original)
    claim = corrected['proposals'][0]['claim']
    if change == 'drop_exclusion': corrected['proposals'].pop()
    elif change == 'drop_hierarchy': corrected['proposals'].pop(0)
    elif change == 'rename_group': claim['group_id'] = 'renamed'
    elif change == 'change_identity': claim['levels'][1]['key'] = 'c_a'
    elif change == 'drop_evidence': corrected['proposals'][0]['evidence_cited'] = ['c_a']
    elif change == 'add_namespace': claim['namespace_columns'] = ['c_b']
    else: corrected['proposals'][1]['claim']['exclusions'][0]['affects'] = ['identity']
    # These changes are syntactically valid. Only the fidelity boundary rejects them.
    ProposalBatch.model_validate(corrected)
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler); tools = Toolbox()
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        if entrypoint == 'explore': legacy_batch(model, input_packet(), tools)
        else: model.propose(input_packet())
    assert len(calls) == 2 and not tools.executed
    assert model.usage.get('schema_repairs_succeeded', 0) == 0
    assert model.budget.snapshot()['reserved'] == {}


def test_schema_correction_cannot_guess_unknown_evidence_semantics(monkeypatch):
    original = malformed_hierarchy()
    claim = original['proposals'][0]['claim']
    claim['repeat_evidence'] = claim.pop('identity_evidence')
    corrected = corrected_hierarchy(original)
    corrected['proposals'][0]['claim']['identity_evidence'] = corrected['proposals'][0]['claim'].pop('repeat_evidence')
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert len(calls) == 2 and model.usage.get('schema_repairs_succeeded', 0) == 0


def test_unverifiable_schema_correction_can_abstain_from_whole_batch(monkeypatch):
    original = malformed_hierarchy()
    original['proposals'][0]['claim'][CANARY] = CANARY
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else {'task': original['task'], 'abstain': True})
    model = client(monkeypatch, handler)
    result = legacy_batch(model, input_packet(), Toolbox())
    assert result.abstain and not result.proposals
    assert model.usage['schema_repair_abstentions'] == 1
    assert model.usage.get('schema_repairs_succeeded', 0) == 0


def test_unparseable_candidate_can_only_abstain_in_requested_task(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={'choices': [{'message': {'content': 'not parseable JSON'}}]})
        return response({'task': 'T1_table_discovery', 'abstain': True})
    model = client(monkeypatch, handler)
    result = legacy_batch(model, input_packet(), Toolbox())
    assert result.abstain and not result.proposals
    assert model.usage['schema_repair_abstentions'] == 1
    assert model.usage.get('schema_repairs_succeeded', 0) == 0


def test_correction_transport_failure_cannot_start_an_unconstrained_attempt(monkeypatch):
    original = malformed_hierarchy()
    changed = corrected_hierarchy(original)
    changed['proposals'].pop()  # A transient HTTP failure cannot erase the exclusion lock.
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) == 1: return response(original)
        assert 'tools' not in body
        assert json.loads(body['messages'][-1]['content'])['schema_protocol_correction']['previous_candidate'] == json.dumps(original)
        if len(calls) == 2: return httpx.Response(503)
        return response(changed)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='model_http_error'):
        legacy_batch(model, input_packet(), Toolbox())
    assert len(calls) == 2 and model.usage['schema_repair_attempts'] == 1
    assert model.usage.get('schema_repairs_succeeded', 0) == 0
    assert model.budget.snapshot()['reserved'] == {}


def invalid_slot_batch(case='empty_members'):
    corrected = corrected_hierarchy(malformed_hierarchy())
    group = {'kind': 'column_group', 'claim': {'group_id': 'subject', 'entity_kind_hint': 'Metric',
        'members': [{'column_id': 'c_a', 'role': 'identity_code', 'in_identity': True}]},
        'evidence_cited': ['c_a']}
    corrected['proposals'].append(group)
    original = deepcopy(corrected)
    claim = original['proposals'][2]['claim']
    if case == 'empty_members':
        claim['members'] = []; path = ['proposals', 2, 'claim', 'members']; mode = 'append'
    elif case == 'missing_members':
        claim.pop('members'); path = ['proposals', 2, 'claim', 'members']; mode = 'missing'
    elif case == 'single_level':
        original['proposals'][0]['claim']['levels'].pop()
        path = ['proposals', 0, 'claim', 'levels']; mode = 'append'
    elif case == 'identifier_type':
        claim['members'][0]['column_id'] = 17
        path = ['proposals', 2, 'claim', 'members', 0, 'column_id']; mode = 'identifier'
    else:
        claim['members'][0].pop('column_id')
        path = ['proposals', 2, 'claim', 'members', 0, 'column_id']; mode = 'missing'
    return original, corrected, {'path': path, 'mode': mode}


@pytest.mark.parametrize('case', ['empty_members', 'missing_members', 'single_level', 'identifier_type', 'missing_reference'])
@pytest.mark.parametrize('entrypoint', ['explore', 'propose'])
def test_only_invalid_slots_are_repaired_without_losing_valid_hierarchy_or_exclusion(monkeypatch, case, entrypoint):
    original, corrected, slot = invalid_slot_batch(case)
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) == 2:
            feedback = json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
            assert feedback['allowed_corrections'] == [slot]
            assert json.loads(feedback['previous_candidate']) == original
            assert 'tools' not in body
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    result = (legacy_batch(model, input_packet(), Toolbox()) if entrypoint == 'explore' else model.propose(input_packet()))
    assert result == ProposalBatch.model_validate(corrected)
    assert [p.kind for p in result.proposals] == ['wide_hierarchy', 'exclusion', 'column_group']
    assert len(calls) == 2 and model.usage['schema_repairs_succeeded'] == 1
    assert model.budget.snapshot()['reserved'] == {}


@pytest.mark.parametrize('change', ['drop_exclusion', 'drop_hierarchy', 'rename_sibling', 'change_valid_level',
                                   'change_invalid_group_kind', 'change_exclusion', 'clear_notes', 'clear_rationale',
                                   'change_task'])
def test_invalid_slot_permission_cannot_change_other_meaning(monkeypatch, change):
    original, corrected, _ = invalid_slot_batch()
    original['notes'] = corrected['notes'] = 'Do not combine distinct reporting paths.'
    original['proposals'][0]['rationale'] = corrected['proposals'][0]['rationale'] = 'Only the declared namespace applies.'
    if change == 'drop_exclusion': corrected['proposals'].pop(1)
    elif change == 'drop_hierarchy': corrected['proposals'].pop(0)
    elif change == 'rename_sibling': corrected['proposals'][0]['claim']['group_id'] = 'renamed'
    elif change == 'change_valid_level': corrected['proposals'][0]['claim']['levels'][0]['key'] = 'c_b'
    elif change == 'change_invalid_group_kind': corrected['proposals'][2]['claim']['entity_kind_hint'] = 'Dimension'
    elif change == 'change_exclusion': corrected['proposals'][1]['claim']['exclusions'][0]['affects'] = ['identity']
    elif change == 'clear_notes': corrected['notes'] = ''
    elif change == 'clear_rationale': corrected['proposals'][0]['rationale'] = ''
    else: corrected['task'] = 'T5_revision'
    ProposalBatch.model_validate(corrected)
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert len(calls) == 2 and model.usage.get('schema_repairs_succeeded', 0) == 0


def test_appending_hierarchy_level_cannot_replace_existing_level(monkeypatch):
    original, corrected, _ = invalid_slot_batch('single_level')
    corrected['proposals'][0]['claim']['levels'][0]['key'] = 'c_b'
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert len(calls) == 2


def test_repair_can_reorder_but_not_remove_issued_evidence(monkeypatch):
    original, corrected, _ = invalid_slot_batch()
    corrected['proposals'][0]['evidence_cited'].reverse()
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    result = legacy_batch(client(monkeypatch, handler), input_packet(), Toolbox())
    assert result.proposals[0].evidence_cited == ['c_b', 'c_a']


def test_identifier_slot_repair_still_requires_issued_reference(monkeypatch):
    from lightgraph.discovery.reference_validation import validate_proposal_references
    original, corrected, _ = invalid_slot_batch('identifier_type')
    corrected['proposals'][2]['claim']['members'][0]['column_id'] = 'unissued_reference'
    calls = []
    def handler(request):
        calls.append(1)
        return response(original if len(calls) == 1 else corrected)
    model = client(monkeypatch, handler)
    result = model.complete('T1_table_discovery', input_packet(), ProposalBatch)
    with pytest.raises(LightGraphError, match='unauthorized_column_reference'):
        validate_proposal_references(result, input_packet())


def test_later_schema_errors_cannot_expand_first_invalid_slot_plan(monkeypatch):
    original, corrected, slot = invalid_slot_batch()
    second = deepcopy(corrected); second['proposals'][0]['claim']['group_id'] = 17
    third = deepcopy(corrected); third['proposals'][0]['claim']['group_id'] = 'rewritten'
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) > 1:
            feedback = json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
            assert feedback['allowed_corrections'] == [slot]
            assert json.loads(feedback['previous_candidate']) == original
        return response([original, second, third][len(calls)-1])
    model = client(monkeypatch, handler)
    # Exercise a hypothetical third attempt without changing the production
    # constructor's one-retry maximum or making any network call.
    model.config.retries = 2
    with pytest.raises(LightGraphError, match='llm_schema_invalid'):
        legacy_batch(model, input_packet(), Toolbox())
    assert len(calls) == 3 and model.usage.get('schema_repairs_succeeded', 0) == 0


@pytest.mark.parametrize('change', ['none', 'remove_condition', 'change_condition_explanation'])
def test_missing_review_slot_never_unlocks_existing_conditions(monkeypatch, change):
    from types import SimpleNamespace
    calls = []
    def handler(request):
        body = json.loads(request.content); calls.append(body)
        packet = json.loads(body['messages'][1]['content'])['input']
        answer = {'proposition_ref': packet['semantic_proposition_ref'], 'decision': 'supported',
            'evidence': [{'reference': 'probe_1', 'stance': 'supports'}],
            'conditions': [{'state': 'satisfied', 'evidence_refs': ['probe_1'],
                            'explanation': 'Applicable only within the stated namespace.'}]}
        if len(calls) == 2:
            answer['basis'] = 'data_inference'
            if change == 'remove_condition': answer['conditions'] = []
            if change == 'change_condition_explanation': answer['conditions'][0]['explanation'] = ''
        return response(answer)
    # This fixture exercises an older client whose response has a required
    # basis. The production compact protocol has no model-authored basis.
    model = SimpleNamespace(complete=client(monkeypatch, handler).complete)
    if change == 'none':
        assert review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, {'kind': 'wide_hierarchy'}, [])
    else:
        with pytest.raises(LightGraphError, match='semantic_claim_unresolved'):
            review_semantics(model, {'probe_digest': [{'probe_id': 'probe_1'}]}, {'kind': 'wide_hierarchy'}, [])
    assert len(calls) == 2

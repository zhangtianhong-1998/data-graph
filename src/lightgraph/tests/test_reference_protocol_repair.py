"""Reference repair changes issued IDs only, never business claims or scope."""
import json

import pytest

from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.reference_validation import (
    invalid_reference_slots, normalize_column_references, validate_or_repair, validate_proposal_references,
)
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import client, final, input_packet, response, Toolbox


UNKNOWN = 'UNKNOWN_REFERENCE_MUST_NOT_BE_ECHOED_9271'


def document_packet():
    packet = input_packet()
    packet['document_snippets'] = [{'doc_id': 'manual', 'revision': 'v2', 'section_id': 'scope',
        'heading': 'scope', 'locator': 'line:1-5', 'text': 'The columns describe objects.'}]
    return packet


@pytest.mark.parametrize('reference', ['manual', 'manual@v2:scope'])
def test_current_document_section_and_legacy_document_id_are_issued(monkeypatch, reference):
    model = client(monkeypatch, lambda request: response(final(evidence=reference)))
    result = model.explore(document_packet(), Toolbox())
    assert result.proposals and model.usage['calls'] == 1
    assert 'reference_repair_attempts' not in model.usage


@pytest.mark.parametrize('reference', ['manual@v1:scope', 'manual@v2:unseen', 'other@v2:scope'])
def test_stale_and_unissued_sections_remain_rejected(reference):
    with pytest.raises(LightGraphError, match='dangling_evidence_reference'):
        validate_proposal_references(ProposalBatch.model_validate(final(evidence=reference)), document_packet())


def test_precise_diagnostics_and_repair_preserve_siblings_and_exclusion(monkeypatch, capsys, caplog):
    raw = final(evidence=UNKNOWN)
    raw['proposals'] += [final()['proposals'][0], {'kind': 'exclusion', 'evidence_cited': ['c_a'],
        'claim': {'exclusions': [{'column_id': 'c_b', 'reason': 'audit_metadata', 'affects': ['identity']}]}}]
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        attempts.append(body)
        if len(attempts) == 1:
            return response(raw)
        assert UNKNOWN not in request.content.decode()
        repair = json.loads(body['messages'][1]['content'])['input']['reference_repair']
        assert repair['diagnostics'] == [{'slot_id': 0, 'proposal_index': 0,
            'field': ['evidence_cited', 0], 'map_key': False, 'category': 'evidence',
            'code': 'dangling_evidence_reference', 'allowed_references': ['c_a', 'c_b', 's_1']}]
        assert len(repair['candidate']['proposals']) == 3
        return response({'replacements': [{'slot_id': 0, 'reference': 'c_a'}]})

    model = client(monkeypatch, handler, retries=0)
    tools = Toolbox()
    result = model.explore(input_packet(), tools)
    assert result.proposals[0].evidence_cited == ['c_a']
    assert result.proposals[1].model_dump(mode='json') == ProposalBatch.model_validate(raw).proposals[1].model_dump(mode='json')
    assert result.proposals[2].model_dump(mode='json') == ProposalBatch.model_validate(raw).proposals[2].model_dump(mode='json')
    assert tools.closed and model.usage['reference_repairs_succeeded'] == 1
    assert UNKNOWN not in capsys.readouterr().out + caplog.text
    assert set(vars(model)) == {'config', 'budget', 'transport', 'usage'}


def test_unissued_repair_is_bounded_and_original_code_survives(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return response(final(evidence=UNKNOWN) if len(calls) == 1 else
                        {'replacements': [{'slot_id': 0, 'reference': 'still_unknown'}]})
    model = client(monkeypatch, handler, retries=0); tools = Toolbox()
    with pytest.raises(LightGraphError, match='dangling_evidence_reference'):
        model.explore(input_packet(), tools)
    assert len(calls) == 3 and model.usage['reference_repair_attempts'] == 2 and tools.closed


def test_reference_repair_can_abstain_only_from_whole_batch(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return response(final(evidence=UNKNOWN) if len(calls) == 1 else {'abstain': True})
    model = client(monkeypatch, handler, retries=0)
    result = model.explore(input_packet(), Toolbox())
    assert result.abstain and result.proposals == []


def test_no_repair_request_after_model_budget_exhaustion(monkeypatch):
    from lightgraph.discovery.budget import BudgetAllocator, Cost
    model = client(monkeypatch, lambda request: response(final(evidence=UNKNOWN)), retries=0)
    model.budget = BudgetAllocator(Cost(llm_calls=1, llm_tokens=1000000, memory_bytes=1000000, seconds=60))
    tools = Toolbox(); packet = input_packet()
    with pytest.raises(BudgetExceeded, match='llm_calls_budget_exhausted'):
        model.explore(packet, tools)
    assert model.usage['calls'] == 1 and tools.closed and 'tool_observations' not in packet
    assert model.budget.snapshot()['reserved'] == {}


@pytest.mark.parametrize('field', ['identity_evidence', 'attribute_evidence', 'global_scope_evidence',
    'taxonomy_evidence', 'position_evidence', 'context_evidence', 'dag_evidence'])
def test_all_hierarchy_evidence_fields_require_registered_ids(field):
    raw = {'task': 'T3_hierarchy', 'proposals': [{'kind': 'wide_hierarchy', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'g', 'levels': [{'position': 1, 'key': 'c_a'}, {'position': 2, 'key': 'c_b'}],
                  field: [UNKNOWN]}}]}
    packet = input_packet(); packet['task'] = 'T3_hierarchy'
    slots = invalid_reference_slots(ProposalBatch.model_validate(raw), packet)
    assert len(slots) == 1 and slots[0].diagnostic(0)['field'] == ['claim', field, 0]
    assert UNKNOWN not in json.dumps(slots[0].diagnostic(0))


def test_invalid_property_mapping_key_is_redacted_and_repaired(monkeypatch):
    raw = {'task': 'T1_table_discovery', 'proposals': [{'kind': 'entity_key', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'g', 'entity_kind_hint': 'Metric', 'key_columns': ['c_a'],
                  'attributes': {'single_valued': ['c_b'], 'property_names': {UNKNOWN: 'label'}}}}]}
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(raw)
        assert UNKNOWN not in request.content.decode()
        return response({'replacements': [{'slot_id': 0, 'reference': 'c_b'}]})
    result = client(monkeypatch, handler, retries=0).explore(input_packet(), Toolbox())
    assert result.proposals[0].claim.attributes.property_names == {'c_b': 'label'}


def test_authorized_patch_cannot_create_duplicate_identity_key(monkeypatch, capsys):
    raw = {'task': 'T1_table_discovery', 'proposals': [{'kind': 'entity_key', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'g', 'entity_kind_hint': 'Metric', 'key_columns': ['c_a', UNKNOWN],
                  'attributes': {}}}]}
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(raw)
        assert UNKNOWN not in request.content.decode()
        return response({'replacements': [{'slot_id': 0, 'reference': 'c_a'}]})
    tools = Toolbox()
    with pytest.raises(LightGraphError, match='unauthorized_column_reference'):
        client(monkeypatch, handler, retries=0).explore(input_packet(), tools)
    assert len(calls) == 3 and tools.closed
    assert UNKNOWN not in capsys.readouterr().out


@pytest.mark.parametrize('mapping_key', ['CN_NAME', 'c_b'])
def test_property_binding_is_checked_after_atomic_reference_patch(monkeypatch, mapping_key):
    raw = {'task': 'T1_table_discovery', 'proposals': [{'kind': 'entity_key', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'g', 'entity_kind_hint': 'Metric', 'key_columns': ['c_a'],
                  'attributes': {'single_valued': ['CN_NAME'], 'property_names': {mapping_key: 'label'}}}}]}
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(raw)
        repair = json.loads(json.loads(request.content)['messages'][1]['content'])['input']['reference_repair']
        assert all('c_b' in item['allowed_references'] for item in repair['diagnostics'])
        return response({'replacements': [{'slot_id': item['slot_id'], 'reference': 'c_b'}
                                         for item in repair['diagnostics']]})
    result = client(monkeypatch, handler, retries=0).explore(input_packet(), Toolbox())
    attributes = result.proposals[0].claim.attributes
    assert attributes.single_valued == ['c_b'] and attributes.property_names == {'c_b': 'label'}
    assert len(calls) == 2


def test_authorized_unbound_property_reaches_per_proposal_compiler(monkeypatch):
    raw = {'task': 'T1_table_discovery', 'proposals': [{'kind': 'entity_key', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'g', 'entity_kind_hint': 'Metric', 'key_columns': ['c_a'],
                  'attributes': {'single_valued': ['CN_NAME'], 'property_names': {'CN_NAME': 'label'}}}}]}
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(raw)
        # Both IDs are issued, but the mapping key does not reference an attribute.
        return response({'replacements': [{'slot_id': 0, 'reference': 'c_b'},
                                           {'slot_id': 1, 'reference': 'c_a'}]})
    tools = Toolbox()
    result = client(monkeypatch, handler, retries=0).explore(input_packet(), tools)
    # The compiler now owns this contract; authorization alone does not bind it.
    assert result.proposals[0].claim.attributes.single_valued == ['c_b']
    assert result.proposals[0].claim.attributes.property_names == {'c_a': 'label'}
    assert tools.closed and len(calls) == 2


def test_exact_authorized_names_normalize_without_model_repair_or_text_rewrite(monkeypatch):
    raw = {'task': 'T1_table_discovery', 'notes': 'opaque_b', 'proposals': [{
        'kind': 'entity_key', 'rationale': 'opaque_a', 'evidence_cited': ['c_a'],
        'claim': {'group_id': 'opaque_a', 'entity_kind_hint': 'Metric',
                  'key_columns': ['opaque_a'], 'attributes': {
                      'single_valued': ['opaque_b'], 'property_names': {'opaque_b': 'label'}}}}]}
    model = client(monkeypatch, lambda request: response(raw), retries=0)
    result = model.explore(input_packet(), Toolbox())
    assert result.proposals[0].claim.key_columns == ['c_a']
    assert result.proposals[0].claim.attributes.property_names == {'c_b': 'label'}
    assert result.notes == 'opaque_b' and result.proposals[0].rationale == 'opaque_a'
    assert result.proposals[0].claim.group_id == 'opaque_a'
    assert model.usage['calls'] == 1 and model.usage['column_references_normalized'] == 3
    assert 'reference_repair_attempts' not in model.usage


@pytest.mark.parametrize('name', ['unknown', 'Opaque_a', ' opaque_a', 'public.objects.opaque_a'])
def test_unknown_and_nonexact_column_names_are_not_normalized(name):
    raw = final(); raw['proposals'][0]['claim']['members'][0]['column_id'] = name
    batch = ProposalBatch.model_validate(raw)
    normalized, count = normalize_column_references(batch, input_packet())
    assert normalized == batch and count == 0
    with pytest.raises(LightGraphError, match='unauthorized_column_reference'):
        validate_proposal_references(normalized, input_packet())


@pytest.mark.parametrize('duplicate_in_metadata', [True, False])
def test_duplicate_authorized_names_or_cross_table_names_stay_ambiguous(duplicate_in_metadata):
    packet = input_packet()
    if duplicate_in_metadata:
        packet['metadata_digest']['columns'].append({'id': 'c_other', 'name': 'opaque_a'})
    else:
        packet['scope'] = {'source_id': 's', 'schema': 'public', 'table': 'objects'}
        packet['catalog'] = [{'source_id': 's', 'schema': 'public', 'name': name,
            'columns': [{'name': 'opaque_a'}]} for name in ('objects', 'other')]
    raw = final(); raw['proposals'][0]['claim']['members'][0]['column_id'] = 'opaque_a'
    batch = ProposalBatch.model_validate(raw)
    normalized, count = normalize_column_references(batch, packet)
    assert normalized == batch and count == 0
    with pytest.raises(LightGraphError, match='unauthorized_column_reference'):
        validate_proposal_references(normalized, packet)


def test_catalog_only_column_name_does_not_grant_column_authority():
    packet = input_packet()
    packet['catalog'] = [{'source_id': 's', 'schema': 'public', 'name': 'other',
        'columns': [{'name': 'foreign_column'}]}]
    raw = final(); raw['proposals'][0]['claim']['members'][0]['column_id'] = 'foreign_column'
    normalized, count = normalize_column_references(ProposalBatch.model_validate(raw), packet)
    assert count == 0
    with pytest.raises(LightGraphError, match='unauthorized_column_reference'):
        validate_proposal_references(normalized, packet)


def test_name_normalization_does_not_guess_evidence_or_overwrite_mapping():
    raw = {'task': 'T1_table_discovery', 'proposals': [{'kind': 'entity_key',
        'evidence_cited': ['opaque_a'], 'claim': {'group_id': 'g', 'entity_kind_hint': 'Metric',
            'key_columns': ['c_a'], 'attributes': {'single_valued': ['c_b'],
                'property_names': {'opaque_b': 'label', 'c_b': 'description'}}}}]}
    original = ProposalBatch.model_validate(raw)
    normalized, count = normalize_column_references(original, input_packet())
    assert normalized == original and count == 0
    assert len(normalized.proposals[0].claim.attributes.property_names) == 2
    with pytest.raises(LightGraphError, match='dangling_evidence_reference'):
        validate_proposal_references(normalized, input_packet())

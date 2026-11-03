"""Repeated tool context can refer to its complete fixed packet copy."""
from copy import deepcopy
import json

from lightgraph.contracts import canonical
from lightgraph.discovery.context_projection import deduplicate_request_context
from lightgraph.discovery.model_references import ModelReferences
from lightgraph.discovery.prompt_window import compact_source_history


def fixture():
    document = {'doc_id': 'definition_document_12345', 'revision': '1', 'section_id': 'identity',
        'read_state': 'complete', 'scope': {'sources': ['s'], 'tables': ['public.items']},
        'text': 'Full definition, including exceptions and counterevidence. ' * 15}
    refs = [{key: value for key, value in document.items() if key != 'text'}]
    tables = [{'source_id': 's', 'schema': 'public', 'table': 'items'}]
    packet = {'document_refs': refs, 'document_snippets': [document], 'context_tables': tables}
    return [{'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': canonical({'input': packet, 'output_schema': {}})},
        {'role': 'assistant', 'tool_calls': [{'id': 'old', 'type': 'function',
            'function': {'name': 'get_metadata', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'old', 'content': canonical({'status': 'ok', 'probe_id': 'probe_1',
            'context': {'document_refs': refs, 'documents': [document], 'tables': tables},
            'data': {'columns': ['code', 'name'], 'rows': [['001', 'name']]},
            'coverage': {'result_complete': False, 'truncated': True}})}]


def test_reuses_exact_complete_copies_without_changing_rows_or_raw_history():
    messages = fixture()
    original = deepcopy(messages)
    fitted, metrics = deduplicate_request_context(messages)
    assert messages == original and fitted[:3] == messages[:3]
    result = json.loads(fitted[-1]['content'])
    assert result['context'] == {'document_refs_ref': 'input.document_refs',
        'documents_ref': 'input.document_snippets', 'tables_ref': 'input.context_tables'}
    assert result['data'] == json.loads(messages[-1]['content'])['data']
    assert result['coverage'] == json.loads(messages[-1]['content'])['coverage']
    assert result['probe_id'] == 'probe_1'
    assert metrics['reused_fields'] == 3 and metrics['projected_results'] == 1
    assert metrics['saved_estimated_tokens'] > 100
    again, repeated = deduplicate_request_context(fitted)
    assert again == fitted and repeated['reused_fields'] == 0


def test_changed_scope_revision_partial_read_and_unknown_fields_are_never_replaced():
    messages = fixture()
    result = json.loads(messages[-1]['content'])
    result['context']['documents'][0]['read_state'] = 'partial'
    result['context']['document_refs'][0]['revision'] = '2'
    result['context']['tables'][0]['table'] = 'different_table'
    result['context']['unrecognised'] = {'obligation': 'keep me'}
    messages[-1]['content'] = canonical(result)
    fitted, metrics = deduplicate_request_context(messages)
    assert fitted == messages and metrics['reused_fields'] == 0


def test_projection_preserves_short_references_and_survives_source_exchange_eviction():
    messages = fixture()
    aliases = ModelReferences(json.loads(messages[1]['content'])['input'])
    messages = aliases.messages(messages)
    latest = deepcopy(messages[-2:])
    latest[0]['tool_calls'][0]['id'] = 'latest'
    latest[1]['tool_call_id'] = 'latest'
    messages += latest
    fitted, metrics = deduplicate_request_context(messages)
    assert metrics['projected_results'] == 2
    window, _ = compact_source_history(fitted, None, 'json_object', set(), target_tokens=1)
    # The old exchange can vanish; the only referenced source remains fixed.
    packet = json.loads(window[1]['content'])['input']
    result = json.loads(window[-1]['content'])
    assert result['context']['document_refs_ref'] == 'input.document_refs'
    assert packet['document_refs'][0]['reference'] == 'section1'
    assert packet['document_refs'][0]['read_state'] == 'complete'
    assert window[-1]['tool_call_id'] == 'latest'


def test_no_anchor_or_malformed_json_is_not_interpreted_or_repaired():
    messages = fixture()
    for content in ('{"context":{},"context":{"documents":[]}}', '{"context":NaN}',
                    '{"context":{"tables":[{"position":1e309}]}}', 'not JSON'):
        messages[-1]['content'] = content
        fitted, metrics = deduplicate_request_context(messages)
        assert fitted == messages and metrics['reused_fields'] == 0
    messages = fixture()[2:]
    fitted, metrics = deduplicate_request_context(messages)
    assert fitted == messages and metrics['reused_fields'] == 0


def test_type_differences_and_existing_reference_slots_are_preserved():
    messages = fixture()
    envelope = json.loads(messages[1]['content'])
    envelope['input']['context_tables'] = [{'position': 1}]
    messages[1]['content'] = canonical(envelope)
    result = json.loads(messages[-1]['content'])
    result['context']['tables'] = [{'position': True}]
    result['context']['document_refs_ref'] = 'original_reference'
    result['context']['documents_ref'] = 'original_document_reference'
    messages[-1]['content'] = canonical(result)
    fitted, metrics = deduplicate_request_context(messages)
    assert fitted == messages and metrics['reused_fields'] == 0

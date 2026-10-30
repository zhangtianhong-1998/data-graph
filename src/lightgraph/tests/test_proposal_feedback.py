"""Diagnose strict proposal errors without leaking values or caching false gaps."""
import asyncio
from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from lightgraph.discovery.agentscope_tools import create_toolkit
from lightgraph.discovery.models import ColumnGroupProposal, Member
from lightgraph.discovery.proposal_session import ProposalSession, proposal_tool_specs
from lightgraph.discovery.validation_feedback import schema_diagnostics, model_diagnostics
from lightgraph.tests.test_agentscope_tools import call
from lightgraph.tests.test_discovery_agent import input_packet
from lightgraph.tests.test_proposal_session import group, tools_response


CANARY = 'PRIVATE_VALUE_AND_FIELD_82413'


@pytest.mark.parametrize('finished,pending,expected', [
    (False, False, ['discovery_invalid_drafts']),
    (True, True, ['discovery_invalid_drafts', 'discovery_submission_pending']),
    (True, False, []),
])
def test_rejected_attempt_count_is_distinct_from_remaining_work(finished, pending, expected):
    session = ProposalSession(input_packet())
    invalid = group()
    invalid['claim']['members'][0]['role'] = CANARY
    result = session.execute('submit_column_group', invalid)
    assert result['code'] == 'invalid_proposal_arguments'
    assert result['diagnostics'][0]['path'] == ['claim', 'members', 0, 'role']
    assert result['diagnostics'][0]['constraint'] == 'enum'
    assert 'identity_code' in result['diagnostics'][0]['allowed_values']
    assert CANARY not in json.dumps(result)
    assert session.execute('submit_column_group', group())['status'] == 'ok'
    if finished:
        session.execute('finish_discovery', {'pending': pending})
    batch = session.batch()
    assert len(batch.proposals) == 1
    assert batch._runtime_invalid_drafts == 1
    assert batch._runtime_pending_codes == expected
    assert '_runtime_invalid_drafts' not in batch.model_dump_json()


def test_explicit_withdrawal_does_not_create_a_valid_draft_or_semantic_authority():
    session = ProposalSession(input_packet())
    session.execute('submit_column_group', {'claim': {CANARY: CANARY}})
    session.execute('finish_discovery', {'pending': False})
    batch = session.batch()
    assert batch.abstain and batch.proposals == []
    assert batch._runtime_invalid_drafts == 1 and not batch._runtime_pending_codes


def test_tool_feedback_identifies_static_fields_without_echoing_source_keys(capsys, caplog):
    class SubmissionTools:
        def tool_specs(self):
            return proposal_tool_specs('T1_table_discovery')
    session = ProposalSession(input_packet())
    toolkit = create_toolkit(SubmissionTools(), session.execute)
    arguments = group()
    arguments['claim']['members'][0]['role'] = CANARY
    arguments['claim'][CANARY] = {'source': CANARY}
    arguments['claim'].pop('entity_kind_hint')
    result = asyncio.run(call(toolkit, 'submit_column_group', arguments))
    assert result['code'] == 'invalid_tool_arguments'
    assert {'path': ['claim', 'entity_kind_hint'], 'constraint': 'required'} in result['diagnostics']
    assert {'path': ['claim'], 'constraint': 'additionalProperties'} in result['diagnostics']
    assert any(d['path'] == ['claim', 'members', 0, 'role'] and
               'identity_code' in d.get('allowed_values', []) for d in result['diagnostics'])
    assert not session.drafts
    assert CANARY not in json.dumps(result) + caplog.text + str(capsys.readouterr())


def test_dynamic_map_keys_and_validator_exception_context_are_redacted():
    schema = {'type': 'object', 'properties': {'mapping': {'type': 'object',
              'additionalProperties': {'type': 'integer'}}}}
    result = schema_diagnostics(Draft202012Validator(schema), {'mapping': {CANARY: CANARY}})
    assert result == [{'path': ['mapping', '*'], 'constraint': 'type'}]
    value = {'column_id': CANARY, 'role': 'fact_value', 'in_identity': True}
    with pytest.raises(ValidationError) as caught:
        Member.model_validate(value)
    result = model_diagnostics(caught.value, Member.model_json_schema(), value)
    assert result == [{'path': [], 'constraint': 'semantic_contract'}]
    assert CANARY not in json.dumps(result)


def test_feedback_is_bounded_and_never_rewrites_business_words():
    arguments = group()
    arguments['claim']['members'] = [deepcopy(arguments['claim']['members'][0]) for _ in range(64)]
    for member in arguments['claim']['members']:
        member['role'] = CANARY
    session = ProposalSession(input_packet())
    result = session.execute('submit_column_group', arguments)
    assert 1 <= len(result['diagnostics']) <= 8
    assert not session.drafts
    assert arguments['claim']['members'][0]['role'] == CANARY
    assert CANARY not in json.dumps(result)


def test_corrected_tool_submission_publishes_and_does_not_repeat_discovery(monkeypatch, tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.sources import MemorySource
    from lightgraph.tests.test_agent_pipeline import client, group as pipeline_group, input_packet as read_packet, table
    source_table = table('feedback', ['key', 'label'])
    turns = []
    def handler(request):
        body, packet = read_packet(request)
        turns.append(packet['task'])
        assert packet['task'] == 'T0_discovery'
        proposal = pipeline_group(packet, 'key', 'label')['proposals'][0]
        proposal.pop('kind')
        if len(turns) == 1:
            proposal['claim']['members'][0]['role'] = CANARY
            return tools_response(('submit_column_group', proposal))
        assert len(turns) == 2
        error = json.loads(body['messages'][-1]['content'])
        assert error['code'] == 'invalid_tool_arguments'
        assert CANARY not in json.dumps(error)
        return tools_response(('submit_column_group', proposal), ('finish_discovery', {'pending': False}))
    model = client(monkeypatch, handler)
    config = RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0)
    with MemorySource([source_table], {'public.feedback': [('K', 'Name')]}) as source:
        first = run_pipeline(config, [source], model=model)
        second = run_pipeline(config, [source], model=model)
    assert first.status == second.status == 'COMPLETED'
    assert first.counts['business_nodes'] == second.counts['business_nodes'] == 1
    assert first.counts['discovery_invalid_drafts'] == 1
    assert not any(i['code'] == 'discovery_invalid_drafts' for i in first.issues)
    assert first.counts.get('discovery_pending_submissions', 0) == 0
    assert first.counts['work_deferred'] == second.counts['work_deferred'] == 0
    assert len(turns) == 2 and second.counts['reused_rules'] == 1

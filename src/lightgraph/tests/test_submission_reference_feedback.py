"""Rejected locator repair is explicit, scoped and measurable without source text."""
from copy import deepcopy
import json

import pytest

from lightgraph.context import discovery_envelope
from lightgraph.discovery.proposal_session import ProposalSession
from lightgraph.discovery.reference_validation import definition_sections
from lightgraph.discovery.engine import _check_code_domain_evidence
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_discovery_agent import input_packet
from lightgraph.tests.test_proposal_session import group


def packet():
    p=input_packet();p['scope']={'source_id':'s','schema':'public','table':'definitions'}
    p['document_snippets']=[{'doc_id':'definitions','revision':'r1','section_id':sid,
        'read_state':'complete','evidence_role':'definition_evidence',
        'sources':[p['scope']],'text':'SOURCE_TEXT_MUST_NOT_PERSIST'} for sid in ('code','scope')]
    return p


def shared(ref):
    value=group()
    value['claim'].update(identity_resolution='verified_identity_domain',identity_domain='metric_codes',
        identity_evidence=[ref])
    return value


def test_domain_reference_rejection_lists_exact_choices_and_corrected_submission_compiles():
    p=packet();session=ProposalSession(p)
    rejected=session.execute('submit_column_group',shared('definitions'))
    assert rejected['code']=='identity_domain_evidence_unresolved'
    diagnostic=rejected['diagnostics'][0]
    assert diagnostic['field']==['claim','identity_evidence',0]
    assert diagnostic['allowed_references']==['definitions@r1:code','definitions@r1:scope']
    fixed={**shared('definitions@r1:code'),'resolves_rejections':[rejected['rejection_id']]}
    assert session.execute('submit_column_group',fixed)['status']=='ok'
    session.execute('finish_discovery',{})
    batch=session.batch()
    assert not batch._runtime_pending_codes and batch._runtime_invalid_codes=={'identity_domain_evidence_unresolved':1}
    _check_code_domain_evidence(batch.proposals[0].claim.identity_evidence,p['document_snippets'],
        table('definitions',['opaque_a','opaque_b']),['semantic_review:independently_supported'])
    assert batch._runtime_invalid_diagnostics==[{'code':'identity_domain_evidence_unresolved',
        'path':['claim','identity_evidence',0],'constraint':'reference','count':1}]
    assert 'definitions@' not in json.dumps(batch._runtime_invalid_diagnostics)
    assert 'SOURCE_TEXT' not in json.dumps(batch._runtime_invalid_diagnostics)
    assert '_runtime_invalid' not in batch.model_dump_json()


@pytest.mark.parametrize('value',['definitions','definitions@r1'])
def test_single_issued_section_alias_is_normalized_but_no_section_is_guessed(value):
    p=packet();p['document_snippets']=p['document_snippets'][:1]
    session=ProposalSession(p)
    assert session.execute('submit_column_group',shared(value))['status']=='ok'
    assert session.drafts[0].claim.identity_evidence==['definitions@r1:code']
    assert session.invalid_count==0


@pytest.mark.parametrize('change',[{'read_state':'partial'},{'derived':True},
    {'evidence_role':'exploration_guidance'},{'sources':[{'source_id':'other','table':'definitions'}]}])
def test_unusable_definition_sections_cannot_enter_shared_code_evidence(change):
    p=packet();p['document_snippets'][0].update(change)
    assert definition_sections(p)=={'definitions@r1:scope'}
    choices=discovery_envelope(p)['reference_options']['definition_sections']
    assert choices==['definitions@r1:scope']
    response=ProposalSession(p).execute('submit_column_group',shared('definitions@r1:code'))
    assert response['status']=='error' and response['diagnostics'][0]['allowed_references']==choices


def test_finish_cannot_hide_rejected_work_and_failed_correction_does_not_close_it():
    session=ProposalSession(input_packet())
    bad=group();bad['claim']['members'][0]['role']='PRIVATE_INVALID_VALUE'
    rejected=session.execute('submit_column_group',bad)
    failed=session.execute('submit_column_group',{**bad,'resolves_rejections':[rejected['rejection_id']]})
    assert failed['status']=='error'
    response=session.execute('finish_discovery',{'pending':False})
    assert response['pending'] and len(response['unresolved_rejections'])==2
    assert set(session.batch()._runtime_pending_codes)=={'discovery_invalid_drafts','discovery_submission_pending'}


def test_successful_unrelated_submission_cannot_claim_to_fix_different_group():
    session=ProposalSession(input_packet())
    bad=group();bad['claim']['members'][0]['role']='PRIVATE_INVALID_VALUE'
    rejected=session.execute('submit_column_group',bad)
    other=group();other['claim']['group_id']='different_group'
    other['resolves_rejections']=[rejected['rejection_id']]
    assert session.execute('submit_column_group',other)['code']=='invalid_rejection_resolution'
    assert session.drafts==[]
    session.execute('finish_discovery',{'withdraw_rejections':list(session.open_rejections)})
    assert not session.batch()._runtime_pending_codes


def test_diagnostics_survive_interrupted_session_even_when_no_draft_was_valid():
    import httpx
    from lightgraph.tests.test_discovery_agent import Toolbox,response
    from lightgraph.tests.test_model_streaming import model
    calls=[]
    def handle(request):
        calls.append(1)
        if len(calls)==1:
            return response(tool='submit_column_group',args={'claim':{'private_source_field':'CANARY'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'},'finish_reason':'length'}]})
    client=model(handle,stream=False,max_discovery_steps=3)
    batch=client.explore(input_packet(),Toolbox())
    assert batch.abstain and batch.proposals==[] and client.usage['calls']==2
    assert batch._runtime_invalid_codes=={'invalid_tool_arguments':1}
    assert batch._runtime_invalid_diagnostics
    assert {'discovery_submission_incomplete','discovery_invalid_drafts','model_response_truncated'}<=set(batch._runtime_pending_codes)
    assert 'CANARY' not in json.dumps(batch._runtime_invalid_diagnostics)


def test_reference_candidates_without_stable_ids_cannot_resolve_each_others_rejections():
    p=input_packet();p['task']='T2_cross_table';p['authorized_entities']=['source','target_a','target_b']
    session=ProposalSession(p)
    original={'evidence_cited':['c_a'],'claim':{'source_entity':'source','source_columns':['c_a'],
        'target_entity':'target_a','target_columns':['c_b'],'cardinality_hint':'invalid_cardinality'}}
    rejected=session.execute('submit_reference',original)
    assert rejected['resolution']=='explicit_withdrawal'
    other=deepcopy(original)
    other['claim'].update(target_entity='target_b',cardinality_hint='n_to_1')
    other['resolves_rejections']=[rejected['rejection_id']]
    denied=session.execute('submit_reference',other)
    assert denied['code']=='invalid_rejection_resolution' and not session.drafts
    assert rejected['rejection_id'] in session.open_rejections
    # Anonymous candidates remain usable: submit first, deliberately withdraw the
    # earlier rejected attempt afterwards, never infer repair from mere success.
    other.pop('resolves_rejections')
    assert session.execute('submit_reference',other)['status']=='ok'
    assert rejected['rejection_id'] in session.open_rejections
    finish=session.execute('finish_discovery',{'withdraw_rejections':list(session.open_rejections)})
    assert not finish['pending'] and not session.batch()._runtime_pending_codes


def test_missing_group_id_cannot_be_treated_as_wildcard_for_successful_submission():
    session=ProposalSession(input_packet())
    rejected=session.execute('submit_column_group',{'claim':{}})
    assert rejected['resolution']=='explicit_withdrawal'
    corrected={**group(),'resolves_rejections':[rejected['rejection_id']]}
    assert session.execute('submit_column_group',corrected)['code']=='invalid_rejection_resolution'
    assert session.drafts==[] and rejected['rejection_id'] in session.open_rejections

"""Declared code/name slots constrain proposals without promoting candidates."""
from copy import deepcopy
import json

import pytest

from lightgraph.discovery.proposal_session import ProposalSession
from lightgraph.tests.test_hierarchy_plan import fixture


def setup(kind):
    _,packet,plan=fixture();ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    packet['document_snippets']=[{'doc_id':'defs','revision':'1','section_id':'objects',
        'read_state':'complete','evidence_role':'definition_evidence','sources':[packet['scope']],
        'text':'SOURCE_VALUE_MUST_NOT_APPEAR_IN_DIAGNOSTICS'}]
    mapping={'object_id':'metric','key_columns':[ids['p1']],
        'label_columns':[ids['n1']],'namespace_columns':[ids['tenant']]}
    base={'group_id':'objects','entity_kind_hint':'Metric','namespace_columns':[ids['tenant']]}
    if kind=='column_group':
        base.pop('namespace_columns');base['members']=[
            {'column_id':ids['p1'],'role':'identity_code','in_identity':True},
            {'column_id':ids['tenant'],'role':'namespace','in_identity':True},
            {'column_id':ids['p1'],'role':'label'}]
        path=['claim','members',2,'column_id']
    elif kind=='entity_key':
        base.update(key_columns=[ids['tenant'],ids['p1']],attributes={
            'single_valued':[ids['p1']]})
        path=['claim','attributes','single_valued',0]
    elif kind=='long_hierarchy':
        base.update(key_column=ids['p1'],parent_column=ids['p2'],label_column=ids['p1'])
        path=['claim','label_column']
    elif kind=='concept_mapping':
        base.update(key_columns=[ids['p1'],ids['p2']],evidence_ref='defs@1',components=[
            {'role':'metric','entity_kind':'Metric','column':ids['p1'],'key_column':ids['p1'],
             'definition_ref':'defs@1:objects','relation':'uses_measure'},
            {'role':'product','entity_kind':'Product','column':ids['n2'],'key_column':ids['p2'],
             'definition_ref':'defs@1:objects','relation':'member_of'}])
        base['entity_kind_hint']='ConceptBinding'
        path=['claim','components',0,'column']
    elif kind=='hierarchy_plan':
        plan['positions'][0]['label']=ids['p1']
        return packet,mapping,plan,['positions',0,'label'],ids
    else:raise AssertionError(kind)
    return packet,mapping,{'claim':base,'evidence_cited':['probe_1']},path,ids


def set_path(value,path,replacement):
    for key in path[:-1]:value=value[key]
    value[path[-1]]=replacement


@pytest.mark.parametrize('kind',['column_group','entity_key','concept_mapping','long_hierarchy','hierarchy_plan'])
def test_explicit_model_catches_code_used_as_label_then_accepts_repair(kind):
    packet,mapping,bad,path,ids=setup(kind);session=ProposalSession(packet)
    assert session.execute('declare_object_model',mapping)['status']=='ok'
    rejected=session.execute('submit_'+kind,bad)
    assert rejected['code']=='object_model_mapping_conflict'
    assert rejected['diagnostics'][0]['field']==path
    assert rejected['diagnostics'][0]['allowed']==[ids['n1']]
    assert 'SOURCE_VALUE' not in json.dumps(rejected)
    assert not session.drafts
    if kind=='hierarchy_plan':assert session.schema.is_empty()
    fixed=deepcopy(bad);set_path(fixed,path,ids['n1'])
    fixed['resolves_rejections']=[rejected['rejection_id']]
    assert session.execute('submit_'+kind,fixed)['status']=='ok'
    assert len(session.drafts)==1 and not session.open_rejections


@pytest.mark.parametrize('kind',['column_group','entity_key','concept_mapping','long_hierarchy','hierarchy_plan'])
def test_name_family_guess_and_name_less_object_do_not_veto_legacy_mapping(kind):
    packet,mapping,bad,_,_=setup(kind)
    packet['schema_knowledge']={'objects':[{**mapping,'basis':'metadata_name_family','status':'candidate'}]}
    assert ProposalSession(deepcopy(packet)).execute('submit_'+kind,bad)['status']=='ok'
    session=ProposalSession(packet)
    assert session.execute('declare_object_model',{**mapping,'label_columns':[]})['status']=='ok'
    assert session.execute('submit_'+kind,bad)['status']=='ok'


@pytest.mark.parametrize('basis,status',[('agent_field_model','candidate'),('executed_rule','published')])
def test_issued_knowledge_can_be_explicitly_revised_and_scope_is_exact(basis,status):
    packet,mapping,bad,_,ids=setup('column_group')
    packet['schema_knowledge']={'objects':[{**mapping,'basis':basis,'status':status}]}
    session=ProposalSession(packet)
    assert session.execute('submit_column_group',bad)['code']=='object_model_mapping_conflict'
    # An explicit local revision remains a hypothesis, even over prior knowledge.
    assert session.execute('declare_object_model',{**mapping,'object_id':'revised','label_columns':[]})['status']=='ok'
    assert session.execute('submit_column_group',bad)['status']=='ok'
    packet['schema_knowledge']={'objects':[{**mapping,'namespace_columns':[], 'basis':basis,'status':status}]}
    assert ProposalSession(packet).execute('submit_column_group',bad)['status']=='ok'


def test_existing_draft_feedback_identifies_explicit_replacement_and_keeps_old_draft():
    packet,mapping,bad,path,ids=setup('column_group');session=ProposalSession(packet)
    old=session.execute('submit_column_group',bad)
    session.execute('declare_object_model',mapping)
    rejected=session.execute('submit_column_group',{**bad,'replace_draft_id':old['draft_id']})
    assert rejected['code']=='object_model_mapping_conflict'
    assert rejected['replace_draft_id']==old['draft_id']
    assert session.drafts[0].claim.members[-1].column_id==ids['p1']
    fixed=deepcopy(bad);set_path(fixed,path,ids['n1'])
    fixed.update(replace_draft_id=old['draft_id'],resolves_rejections=[rejected['rejection_id']])
    assert session.execute('submit_column_group',fixed)['replaced']
    assert session.drafts[0].claim.members[-1].column_id==ids['n1']


def test_rejected_plan_revision_does_not_leave_implicit_schema_definition():
    _,packet,plan=fixture();session=ProposalSession(packet)
    old=session.execute('submit_hierarchy_plan',plan)
    schema=session.schema.model_dump();revised=deepcopy(plan)
    revised['entity_type']='OtherConcept'
    revised['replace_draft_id']=old['draft_id']
    assert session.execute('submit_hierarchy_plan',revised)['code']=='draft_replacement_changes_obligations'
    assert session.schema.model_dump()==schema
    revised.pop('replace_draft_id')
    assert session.execute('submit_hierarchy_plan',revised)['code']=='draft_replacement_required'
    assert session.schema.model_dump()==schema


def test_finish_exposes_conflict_from_later_knowledge_and_accepts_explicit_repair():
    packet,mapping,bad,path,ids=setup('column_group');session=ProposalSession(packet)
    old=session.execute('submit_column_group',bad)
    session.execute('declare_object_model',mapping)
    rejected=session.execute('finish_discovery',{})
    assert rejected['code']=='object_model_mapping_conflict' and not session.finished
    assert rejected['replace_draft_id']==old['draft_id']
    assert rejected['diagnostics'][0]['field']==path
    assert session.execute('finish_discovery',{})['rejection_id']==rejected['rejection_id']
    assert session.invalid_count==1
    forced=session.batch(pending_code='discovery_submission_incomplete')
    assert not forced.proposals and 'object_model_mapping_conflict' in forced._runtime_pending_codes
    assert len(session.drafts)==1 and session.invalid_count==1
    assert session.batch(abstain=True).abstain
    fixed=deepcopy(bad);set_path(fixed,path,ids['n1'])
    fixed.update(replace_draft_id=old['draft_id'],resolves_rejections=[rejected['rejection_id']])
    assert session.execute('submit_column_group',fixed)['replaced']
    assert session.execute('finish_discovery',{})['status']=='ok'
    assert len(session.batch().proposals)==1 and not session.batch()._runtime_pending_codes


def test_legacy_final_cannot_skip_persisted_explicit_mapping_feedback():
    from lightgraph.tests.test_model_streaming import model
    from lightgraph.tests.test_discovery_agent import Toolbox,response
    packet,mapping,bad,_,_=setup('column_group')
    packet.pop('probe_digest');packet.pop('document_snippets')
    bad['evidence_cited']=mapping['key_columns']
    packet['schema_knowledge']={'objects':[{**mapping,'basis':'agent_field_model','status':'candidate'}]}
    client=model(lambda request:response({'task':packet['task'],
        'proposals':[{'kind':'column_group',**bad}]}),stream=False)
    batch=client.explore(packet,Toolbox(),max_steps=1)
    assert batch.abstain and not batch.proposals
    assert batch._runtime_invalid_codes['object_model_mapping_conflict']==1


def test_mapping_and_replacement_feedback_survives_restart_without_payload(tmp_path):
    from lightgraph.contracts import ExtractionGoal,SourceRef
    from lightgraph.runtime.iteration import IterationLedger
    from lightgraph.store import GraphStore
    codes=['object_model_mapping_conflict','draft_replacement_required',
           'invalid_draft_replacement','draft_replacement_changes_obligations']
    source=SourceRef(source_id='s',table='items');path=tmp_path/'feedback.sqlite'
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        ledger.record_discovery_feedback([source],[{'code':'discovery_draft_field_diagnostic',
            'reason_code':code,'proposal':'SOURCE_VALUE'} for code in codes],
            task='metadata_discovery')
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        item=ledger.enqueue(source,task='metadata_discovery',context_version='one')
        feedback=ledger.context([item],[],iteration=0)['prior_discovery_feedback']
        assert feedback[0]['codes']==sorted(codes)
        assert 'SOURCE_VALUE' not in json.dumps(feedback)

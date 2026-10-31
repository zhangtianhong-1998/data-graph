"""Reviewed repair contracts remain diagnosable without retaining source values."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal, ForeignKeySpec, RuleSpec, SourceRef
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import DiscoveryResult, ProposalBatch
from lightgraph.discovery.repair import compiled_contract_error
from lightgraph.discovery.workflow import _compile, _exploration_order, _record_submission_state
from lightgraph.pipeline import _issues
from lightgraph.runtime.iteration import IterationLedger
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_concept_mapping import coded_rule


def test_exploration_orders_declared_targets_and_narrow_tables_without_business_names():
    wide=table('x',['id','a','b','c']);small=table('z',['id']);middle=table('y',['id','a'])
    small.foreign_keys=[ForeignKeySpec(name='fk',columns=['id'],target_schema='public',
        target_table='y',target_columns=['id'])]
    assert [t.name for t in _exploration_order([wide,small,middle])]==['y','z','x']
    middle.foreign_keys=[ForeignKeySpec(name='cycle',columns=['id'],target_schema='public',
        target_table='z',target_columns=['id'])]
    assert len(_exploration_order([wide,small,middle]))==3


def test_concept_validation_error_becomes_candidate_and_valid_sibling_survives(monkeypatch):
    source=table('items',['id','label']);ids={c.name:_column_id(source,c.name) for c in source.columns}
    batch=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[
        {'kind':'column_group','evidence_cited':[ids['id']],'claim':{'group_id':name,'entity_kind_hint':'ReportItem',
          'members':[{'column_id':ids['id'],'role':'identity_code','in_identity':True},
                     {'column_id':ids['label'],'role':'label'}]}} for name in ('broken','valid')]})
    raw=coded_rule().model_dump();raw['concept_mapping']['components'][0]['identity_domain']='codes'
    with pytest.raises(ValidationError) as caught:RuleSpec.model_validate(raw)
    assert compiled_contract_error(caught.value)=='concept_identity_domain_evidence_required'
    import lightgraph.discovery.engine as engine
    original=engine._compile_model
    def compile_one(batch,*args,**kwargs):
        if batch.proposals[-1].claim.group_id=='broken':raise caught.value
        return original(batch,*args,**kwargs)
    monkeypatch.setattr(engine,'_compile_model',compile_one)
    result=DiscoveryResult()
    rules=_compile(batch,source,ExtractionGoal(),[],result)
    assert len(rules)==1
    assert len(result.pending_candidates)==1
    assert result.pending_candidates[0].failure_code=='concept_identity_domain_evidence_required'


def test_diagnostic_report_and_restart_feedback_are_structural_only(tmp_path):
    source=table('items',['id']);batch=ProposalBatch(task='T0_discovery')
    batch._runtime_invalid_drafts=2
    batch._runtime_invalid_codes={'invalid_tool_arguments':2}
    batch._runtime_invalid_diagnostics=[{'code':'invalid_tool_arguments',
        'path':['claim','members',0,'role'],'constraint':'enum','count':2}]
    result=DiscoveryResult();_record_submission_state(result,source,batch)
    report=SimpleNamespace(issues=[]);_issues(report,result.issues)
    detail=next(i for i in report.issues if i['code']=='discovery_draft_field_diagnostic')
    assert detail['field_path']=='["claim","members",0,"role"]'
    assert detail['reason_code']=='invalid_tool_arguments' and detail['count']==2
    src=SourceRef(source_id='s',table='items');path=tmp_path/'feedback.sqlite'
    canary='PRIVATE_SOURCE_VALUE'
    report.issues.extend([
        {'table':'public.items','code':'identity_domain_evidence_unresolved','message':canary},
        {**detail,'field_path':json.dumps(['claim',canary]),'allowed_values':[canary]},
        {**detail,'constraint':canary},{**detail,'reason_code':canary},
        {**detail,'field_path':json.dumps(['positions',0,'code'])}])
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        ledger.record_discovery_feedback([src],report.issues,task='metadata_discovery')
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        item=ledger.enqueue(src,task='metadata_discovery',context_version='v16')
        feedback=ledger.context([item],[],iteration=0)['prior_discovery_feedback'][0]
        assert feedback['codes']==['identity_domain_evidence_unresolved']
        assert feedback['diagnostics'][0]['path']==['claim','members',0,'role']
        assert feedback['diagnostics'][1]['path']==['claim','*']
        assert feedback['diagnostics'][2]['path']==['positions',0,'code']
        assert canary not in json.dumps(feedback)
        assert canary not in json.dumps([dict(row) for row in store.connection.execute('SELECT * FROM discovery_diagnostic')])


def test_recovered_submission_keeps_actual_truncation_cause():
    batch=ProposalBatch(task='T0_discovery')
    batch._runtime_pending_codes=['discovery_submission_incomplete','model_response_truncated','PRIVATE_INPUT']
    result=DiscoveryResult();_record_submission_state(result,table('items',['id']),batch)
    assert {issue['code'] for issue in result.issues}=={'discovery_submission_incomplete','model_response_truncated'}

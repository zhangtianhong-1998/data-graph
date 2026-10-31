"""A known cross-rule naming conflict must be repaired before optional T2 work."""
from types import SimpleNamespace

from lightgraph.discovery.models import DiscoveryResult
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_concept_identity_domains import shared_rules


def test_mapping_conflict_keeps_table_pending_after_forced_submission():
    from lightgraph.discovery.models import ProposalBatch
    from lightgraph.discovery.workflow import _record_submission_state
    batch=ProposalBatch(task='T1_table_discovery',abstain=True)
    batch._runtime_pending_codes=['object_model_mapping_conflict']
    result=DiscoveryResult()
    _record_submission_state(result,table('metric',['code','name']),batch)
    assert result.issues==[{'table':'public.metric','code':'object_model_mapping_conflict','status':'deferred'}]
    assert result.stats['discovery_pending_submissions']==1


def test_shared_name_repair_precedes_relation_calls(monkeypatch,tmp_path):
    document=load_document('codes.md',text='---\npurpose: data_description\n---\n# 编码\n同编码具有统一名称。')
    first=shared_rules()[0]
    reference=document.document_id+'@'+document.revision+':codes'
    first.identity.identity_evidence=[reference]
    first.identity.attribute_evidence=[reference]
    first.dependencies.documents=[document.document_id+'@'+document.revision]
    second=first.model_copy(deep=True);second.rule_id='alternate'
    second.attributes.single_valued['label']='wrong'
    events=[]
    def discover(*args,**kwargs):
        prior=kwargs.get('revision_rules')
        if prior:
            events.append('repair')
            revised=[r.model_copy(deep=True) for r in prior]
            for rule in revised:rule.attributes.single_valued['label']='name'
            return DiscoveryResult(rules=revised)
        events.append('initial')
        return DiscoveryResult(rules=[first,second])
    def relations(*args,**kwargs):
        events.append('relations')
        assert 'repair' in events
        return DiscoveryResult(relation_table_outcomes={'public.metric':'resolved'})
    monkeypatch.setattr('lightgraph.discovery.discover',discover)
    monkeypatch.setattr('lightgraph.discovery.discover_references',relations)
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kwargs:([],{},set()))
    model=SimpleNamespace(explore=lambda:None,usage={'calls':0})
    with MemorySource([table('metric',['code','name','wrong'])],
            {'public.metric':[('M01','占用','M01')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
            [source],model=model,documents=[document])
    assert 'relations' in events and events.index('repair')<events.index('relations'),(events,report.model_dump())
    assert set(report.rules_published)=={first.rule_id,second.rule_id},report.model_dump()
    assert report.counts['business_nodes']==1
    assert not any(i['code']=='pattern_review_no_progress' for i in report.issues)


def test_new_local_cycle_keeps_repair_work_when_old_global_cycle_disappears(monkeypatch,tmp_path):
    from lightgraph.contracts import RuleSpec
    document=load_document('codes.md',text='---\npurpose: data_description\n---\n# 编码\n编码共享且父级关系有方向。')
    first=shared_rules()[0];first.rule_id='first'
    reference=document.document_id+'@'+document.revision+':codes'
    first.identity.identity_evidence=[reference];first.identity.attribute_evidence=[reference]
    first.dependencies.documents=[document.document_id+'@'+document.revision]
    first=RuleSpec.model_validate({**first.model_dump(),'kind':'long_hierarchy',
        'hierarchy':{'parent_column':'parent1','taxonomy':'items','order_evidence':[reference]}})
    second=first.model_copy(deep=True);second.rule_id='second';second.hierarchy.parent_column='parent2'
    revisions=[]
    def discover(*args,**kwargs):
        prior=kwargs.get('revision_rules')
        if not prior:return DiscoveryResult(rules=[first,second])
        rule=prior[0].model_copy(deep=True)
        if rule.rule_id=='first':
            revisions.append(rule.hierarchy.parent_column)
            rule.hierarchy.parent_column='both' if len(revisions)==1 else 'neither'
        return DiscoveryResult(rules=[rule])
    monkeypatch.setattr('lightgraph.discovery.discover',discover)
    monkeypatch.setattr('lightgraph.discovery.discover_references',lambda *a,**kw:DiscoveryResult())
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kwargs:([],{},set()))
    with MemorySource([table('metric',['code','name','parent1','parent2','both','neither'])],
            {'public.metric':[('A','Alpha','B',None,'B',None),('B','Beta',None,'A','A',None)]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0,
            max_discovery_rounds=3),[source],model=SimpleNamespace(explore=lambda:None,usage={'calls':0}),
            documents=[document])
    assert revisions==['parent1','both'],report.model_dump()
    assert set(report.rules_published)=={'first','second'},report.model_dump()
    assert report.counts['business_nodes']==2 and report.counts['business_edges']==1

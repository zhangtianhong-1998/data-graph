"""A timeout retains field work and guards, never approves a hierarchy."""
from types import SimpleNamespace

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.workflow import discover_with_agent, _proposal_guards
from lightgraph.documents import load_document
from lightgraph.errors import BudgetExceeded
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_field_model_rule import ReviewModel
from lightgraph.tests.test_object_model_supersession import fixture
from lightgraph.tests.test_schema_knowledge import setup_ledger, packet


def setup(tmp_path):
    current,_,model=fixture()
    guide=load_document('paths.md',text='---\nid: paths\npurpose: data_description\napplies_to: [s:public.paths]\n---\n# Definition\nCodes denote objects across positions. Repeated codes are padding.')
    store=GraphStore(tmp_path/'knowledge.sqlite')
    ledger=setup_ledger(store,[current],docs=[guide])
    body=packet(current);body['schema_knowledge']=ledger.context(current)
    ledger.record_models(current,[model],body)
    row={'tenant_id':'T'}
    for position in range(1,6):
        for suffix,value in [('code',str(position)),('cn_name','Label '+str(position)),('en_name','English '+str(position)),('alias','Alias '+str(position))]:
            row[f'item_l{position}_{suffix}']=value
    return current,guide,store,ledger,row


def test_timeout_retains_mapping_and_next_run_starts_with_semantics(tmp_path):
    current,guide,store,ledger,row=setup(tmp_path)
    class Timeout(ReviewModel):
        def explore(self, body, tools, **kwargs):
            assert body['task']=='T3_hierarchy'
            raise BudgetExceeded('wall_time_budget_exhausted')
    try:
        with MemorySource([current],{current.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=Timeout(),schema_knowledge=ledger)
        assert not result.rules
        assert ledger._load(current)['field_model_work']=='pending'
        assert len(next(o for o in ledger.current_objects(current) if o['basis']=='agent_field_model')['positions'])==5
    finally:store.close()
    with GraphStore(tmp_path/'knowledge.sqlite') as restarted:
        ledger=setup_ledger(restarted,[current],docs=[guide])
        class Resume(ReviewModel):
            tasks=[]
            def explore(self, body, tools, **kwargs):
                self.tasks.append(body['task'])
                return super().explore(body,tools,**kwargs)
        model=Resume()
        with MemorySource([current],{current.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=model,schema_knowledge=ledger)
        assert model.tasks==['T3_hierarchy']
        assert len(result.rules)==1 and result.rules[0].kind=='wide_hierarchy'
        assert all(level.aliases for level in result.rules[0].hierarchy.levels)


def test_explicit_exclusion_survives_resume_and_blocks_projecting_alias(tmp_path):
    current,guide,store,ledger,row=setup(tmp_path)
    try:
        ledger.record_field_model_guards(current,projection=['item_l1_en_name'])
        with MemorySource([current],{current.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=ReviewModel(),schema_knowledge=ledger)
        assert not result.rules
        assert any(i['code']=='excluded_column_projected' for i in result.issues)
        assert ledger.field_model_guards(current)[0]=={'item_l1_en_name'}
    finally:store.close()


def test_refuted_hierarchy_is_not_automatically_retried_from_mapping(tmp_path):
    current,guide,store,ledger,row=setup(tmp_path)
    class Abstain(ReviewModel):
        tasks=[]
        def explore(self, body, tools, **kwargs):
            self.tasks.append(body['task'])
            return ProposalBatch(task=body['task'],abstain=True)
    try:
        ledger.set_field_model_work(current,'review_refuted')
        ledger.set_field_model_work(current,'pending')
        model=Abstain()
        with MemorySource([current],{current.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=model,schema_knowledge=ledger)
        assert model.tasks==['T0_discovery'] and not result.rules
        assert ledger._load(current)['field_model_work']=='review_refuted'
    finally:store.close()


def test_display_only_on_rejected_entity_is_not_global_exclusion():
    current=table('items',['code','name']);code=_column_id(current,'code');label=_column_id(current,'name')
    batch=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[{'kind':'entity_key','evidence_cited':[code],'claim':{
        'group_id':'wrong_grain','entity_kind_hint':'ReportItem','key_columns':[code],
        'attributes':{'display_only':[label]}}}]})
    assert _proposal_guards(batch,current,ExtractionGoal())==(set(),set())


def test_table_timeout_does_not_take_next_tables_time(monkeypatch):
    now=[100.0]
    monkeypatch.setattr('lightgraph.discovery.budget.time.monotonic',lambda:now[0])
    tables=[table('a',['code','name']),table('b',['code','name'])]
    class Model(ReviewModel):
        def __init__(self):
            super().__init__();self.budget=BudgetAllocator(Cost(seconds=100,db_cu=100,llm_calls=100,llm_tokens=None,memory_bytes=1<<20));self.tasks=[]
        def explore(self, body, tools, **kwargs):
            self.tasks.append(body['scope']['table'])
            if len(self.tasks)==1:
                now[0]=self.budget.deadline_monotonic+0.01
                raise BudgetExceeded('wall_time_budget_exhausted')
            return ProposalBatch(task=body['task'],abstain=True)
    model=Model();deadline=model.budget.deadline_monotonic
    with MemorySource(tables,{t.qualified_name:[] for t in tables}) as source:
        result=discover_with_agent(source,model=model)
    assert model.tasks==['a','b']
    assert len(result.understandings)==2
    assert model.budget.deadline_monotonic==deadline


def test_refuted_composite_key_can_be_replaced_by_independently_reviewed_path(tmp_path):
    from lightgraph.context import context_dependency_fingerprint
    from lightgraph.discovery.conflicts import candidate_dependency
    from lightgraph.documents import DocumentCorpus
    current,guide,store,ledger,row=setup(tmp_path)
    corpus=DocumentCorpus([guide]);corpus.bind_catalog([current])
    docs=corpus.context_for(current,current.column_names)
    refs=[d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in docs]
    keys=[_column_id(current,f'item_l{p}_code') for p in range(1,6)]
    names=[_column_id(current,f'item_l{p}_{suffix}') for p in range(1,6) for suffix in ('cn_name','en_name')]
    candidate={'candidate_id':'candidate_'+'a'*24,'source':{'source_id':'s','schema':'public','table':'paths'},
        'proposal_kind':'entity_key','failure_code':'semantic_claim_refuted','review_stage':'entity_granularity',
        'column_refs':keys+names,'blocking_evidence_refs':refs,'document_refs':refs,
        'dependency_fingerprint':candidate_dependency(current,ExtractionGoal(),context_dependency_fingerprint(corpus,[current],ExtractionGoal())),
        'mappings':[{'variant_id':'a'*64,'guard_fingerprint':'b'*64,'proposal_kind':'entity_key','entity_kind':'ReportItem',
            'fields':[{'role':'key','columns':keys},{'role':'excluded','columns':names}]}]}
    class Review(ReviewModel):
        def complete_bound_review(self, body, **kwargs):
            assert body['semantic_revision_guard']['blocking_evidence_refs']==refs
            return super().complete_bound_review(body,**kwargs)
    try:
        with MemorySource([current],{current.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=corpus,model=Review(),schema_knowledge=ledger,
                iteration_context={'pending_candidates':[candidate]})
        assert len(result.rules)==1
        assert result.resolved_candidate_ids==[candidate['candidate_id']]
        assert result.candidate_replacement_rules[candidate['candidate_id']]==[result.rules[0].rule_id]
        assert candidate['failure_code']=='semantic_claim_refuted'
        assert all(level.label and level.aliases for level in result.rules[0].hierarchy.levels)
    finally:store.close()


def test_new_mapping_recovery_inherits_same_batch_exclusion(tmp_path):
    current,_,field_model=fixture()
    guide=load_document('paths.md',text='---\nid: paths\npurpose: data_description\napplies_to: [s:public.paths]\n---\n# Definition\nCodes identify relative path objects.')
    with GraphStore(tmp_path/'fresh.sqlite') as store:
        ledger=setup_ledger(store,[current],docs=[guide])
        class FirstPass(ReviewModel):
            tasks=[]
            def explore(self, body, tools, **kwargs):
                self.tasks.append(body['task'])
                if body['task']=='T0_discovery':
                    batch=ProposalBatch.model_validate({'task':body['task'],'proposals':[
                        {'kind':'exclusion','evidence_cited':[_column_id(current,'item_l1_en_name')],'claim':{'exclusions':[{'column_id':_column_id(current,'item_l1_en_name'),
                            'reason':'out_of_goal','affects':['projection']}]}}]})
                    batch._runtime_object_models=[field_model]
                    return batch
                return super().explore(body,tools,**kwargs)
        model=FirstPass()
        with MemorySource([current],{current.qualified_name:[]}) as source:
            result=discover_with_agent(source,documents=[guide],model=model,schema_knowledge=ledger)
        assert model.tasks==['T0_discovery','T3_hierarchy']
        assert not result.rules
        assert any(item['code']=='excluded_column_projected' for item in result.issues)
        assert ledger.field_model_guards(current)[0]=={'item_l1_en_name'}

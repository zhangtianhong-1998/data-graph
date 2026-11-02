"""Restored grain review uses fresh evidence, preserving the old rejected shape."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.semantics import BoundSemanticVerdict, review_semantics
from lightgraph.discovery.workflow import discover_with_agent
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_field_model_resume import setup
from lightgraph.tests.test_field_model_rule import ReviewModel


def rejected_grain(table, docs, *, probe='probe_0001', guide=None):
    keys=[_column_id(table,f'item_l{p}_code') for p in range(1,6)]
    from lightgraph.contracts import ExtractionGoal
    from lightgraph.context import context_dependency_fingerprint
    from lightgraph.discovery.conflicts import candidate_dependency
    from lightgraph.documents import DocumentCorpus
    corpus=DocumentCorpus([guide] if guide else []);corpus.bind_catalog([table])
    return {'dependency_fingerprint':candidate_dependency(table,ExtractionGoal(),context_dependency_fingerprint(corpus,[table],ExtractionGoal())), 'candidate_id':'candidate_'+'a'*24,'source':{'source_id':'s','schema':'public','table':'paths'},
        'proposal_kind':'entity_key','failure_code':'semantic_claim_refuted','review_stage':'entity_granularity',
        'column_refs':keys,'blocking_evidence_refs':docs+[probe],'document_refs':docs,
        'mappings':[{'variant_id':'a'*64,'guard_fingerprint':'b'*64,'proposal_kind':'entity_key','entity_kind':'ReportItem',
            'fields':[{'role':'key','columns':keys}]}]}


class Recover(ReviewModel):
    def __init__(self):
        super().__init__();self.config.allow_source_samples=True;self.seen=[]
    def complete_bound_review(self, packet, *, proposition_ref, **kwargs):
        self.seen.append(deepcopy(packet))
        guard=packet['semantic_revision_guard']
        evidence=[{'reference':ref,'stance':'supports'} for ref in packet['required_evidence_refs']]
        return BoundSemanticVerdict(decision='supported',evidence=evidence,
            recovery_obligations=[{'candidate_id':o['candidate_id'],'decision':'resolved',
                'evidence_refs':o['required_evidence_refs']} for o in guard['recovery_obligations']]).bind(proposition_ref)


def test_recovery_rechecks_old_probe_without_collision_and_executes_shared_positions(tmp_path):
    table,guide,store,ledger,row=setup(tmp_path);model=Recover()
    # One object occurs in all five positions; English aliases differ by slot.
    for p in range(1,6):
        row[f'item_l{p}_code']='A';row[f'item_l{p}_cn_name']='Alpha'
    try:
        candidate=rejected_grain(table,[],guide=guide)
        with MemorySource([table],{table.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=model,schema_knowledge=ledger,
                iteration_context={'pending_candidates':[candidate]})
        assert len(result.rules)==1,result.issues
        assert result.resolved_candidate_ids==[candidate['candidate_id']]
        packet=model.seen[0];obligation=packet['semantic_revision_guard']['recovery_obligations'][0]
        assert obligation['historical_probe_refs']==['probe_0001']
        assert obligation['historical_probe_status']=='unavailable_not_replayed'
        assert 'probe_0001' not in packet['issued_evidence_refs']
        assert len(packet['probe_digest'])==6
        assert len(obligation['current_rechecks'])==6
        assert all(item['keys']==[_column_id(table,'tenant_id'),_column_id(table,f'item_l{index}_code')]
            and item['values']==[_column_id(table,f'item_l{index}_cn_name')]
            for index,item in enumerate(obligation['current_rechecks'][1:],1))
        assert 'Alpha' not in str(obligation)+str(packet['probe_digest'])
        assert all('rows' not in p for p in packet['probe_digest'])
        assert all(level.aliases for level in result.rules[0].hierarchy.levels)
        # The new rule still needs normal full execution and publication.
        with GraphStore(tmp_path/'execution.sqlite') as target:
            with MemorySource([table],{table.qualified_name:[row]}) as source:
                executed=RuleEngine(target).execute(source,result.rules[0],document_versions={guide.document_id+'@'+guide.revision})
            assert executed.published,executed.codes
            nodes=target.view({'T'})['nodes'];assert len(nodes)==1
            assert set(nodes[0]['properties']['source_position'])=={1,2,3,4,5}
            assert len(nodes[0]['aliases'])>=5
    finally:store.close()


def test_legacy_probe_without_a_verifiable_structural_replacement_stays_pending(tmp_path):
    table,guide,store,ledger,row=setup(tmp_path);model=Recover()
    candidate=rejected_grain(table,[],guide=guide);candidate['mappings']=[]
    try:
        with MemorySource([table],{table.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=model,schema_knowledge=ledger,
                iteration_context={'pending_candidates':[candidate]})
        assert not result.rules and not model.seen
        assert any(i['code']=='semantic_recovery_evidence_required' for i in result.issues)
        assert ledger._load(table)['field_model_work']=='pending'
    finally:store.close()


def guard_packet():
    return {'probe_digest':[{'probe_id':'probe_new'}],
        'semantic_revision_guard':{'blocking_evidence_refs':['probe_new'],'conditions':[],
            'recovery_obligations':[{'candidate_id':'candidate_a','required_evidence_refs':['probe_new']}]}}


def test_missing_counterevidence_and_obligation_get_one_fixed_verdict_patch():
    calls=[];usage={}
    def complete(task,packet,schema):
        calls.append(deepcopy(packet))
        if len(calls)==1:
            return schema.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference',
                'evidence':[{'reference':'c_support','stance':'supports'}]})
        assert packet['semantic_review_repair']['required_evidence_refs']==['probe_new']
        return schema.model_validate({'document_classifications':[{'reference':'probe_new','stance':'context'}],
            'recovery_classifications':[{'candidate_id':'candidate_a','decision':'resolved','evidence_refs':['probe_new']}]})
    packet=guard_packet();packet['metadata_digest']={'columns':[{'id':'c_support'}]}
    refs=review_semantics(SimpleNamespace(complete=complete,usage=usage),packet,{'kind':'wide_hierarchy'},[])
    assert 'c_support' in refs and len(calls)==2
    assert usage['semantic_reference_repairs_succeeded']==1
    assert calls[0]['semantic_proposition_ref']==calls[1]['semantic_proposition_ref']


@pytest.mark.parametrize('case,code',[
    ('unresolved','semantic_recovery_obligation_unresolved'),
    ('changed_condition','semantic_revision_constraints_changed'),
    ('refuted','semantic_claim_refuted'),
])
def test_recovery_cannot_patch_away_actual_rejection_or_conditions(case,code):
    calls=[];packet=guard_packet()
    if case=='changed_condition':packet['semantic_revision_guard']['conditions']=[
        {'condition_ref':'condition_1','condition':'Keep namespace','prior_state':'unknown','evidence_refs':[]}]
    def complete(task,body,schema):
        calls.append(1)
        return schema.model_validate({'proposition_ref':body['semantic_proposition_ref'],
            'decision':'refuted' if case=='refuted' else 'supported','basis':'data_inference',
            'evidence':[{'reference':'probe_new','stance':'supports'}],
            'recovery_obligations':[{'candidate_id':'candidate_a','decision':'unresolved' if case=='unresolved' else 'resolved','evidence_refs':['probe_new']}]})
    with pytest.raises(LightGraphError,match=code):
        review_semantics(SimpleNamespace(complete=complete),packet,{'kind':'wide_hierarchy'},[])
    assert calls==[1]


def test_failed_reference_patch_is_retained_as_a_recoverable_hierarchy_candidate(tmp_path):
    table,guide,store,ledger,row=setup(tmp_path)
    class Missing(Recover):
        def complete_bound_review(self, packet, *, proposition_ref, **kwargs):
            return BoundSemanticVerdict(decision='supported',evidence=[
                {'reference':ref,'stance':'supports'} for ref in packet['required_document_refs']]).bind(proposition_ref)
        def complete(self,task,packet,schema):return schema.model_validate({})
    try:
        with MemorySource([table],{table.qualified_name:[row]}) as source:
            result=discover_with_agent(source,documents=[guide],model=Missing(),schema_knowledge=ledger,
                iteration_context={'pending_candidates':[rejected_grain(table,[],guide=guide)]})
        assert not result.rules
        assert any(c.failure_code=='semantic_revision_evidence_missing' for c in result.pending_candidates)
        assert ledger._load(table)['field_model_work']=='pending'
    finally:store.close()


def test_recovery_reviews_all_document_windows_without_cross_window_unissued_refs(monkeypatch):
    import lightgraph.context
    docs=[{'doc_id':str(i),'revision':'1','section_id':'definition','text':'definition',
        'purpose':'data_description','evidence_role':'definition_evidence','read_state':'complete'} for i in range(2)]
    monkeypatch.setattr(lightgraph.context,'document_batches',lambda documents:iter([[docs[0]],[docs[1]]]))
    refs=['0@1:definition','1@1:definition'];packet=guard_packet()
    packet['semantic_revision_guard']['blocking_evidence_refs']+=refs
    packet['semantic_revision_guard']['recovery_obligations'][0]['required_evidence_refs']+=refs
    calls=[]
    def complete(task,body,schema):
        calls.append(body)
        expected={'probe_new',refs[len(calls)-1]}
        assert set(body['required_evidence_refs'])==expected
        assert set(body['semantic_revision_guard']['recovery_obligations'][0]['required_evidence_refs'])==expected
        return schema.model_validate({'proposition_ref':body['semantic_proposition_ref'],
            'decision':'supported','basis':'data_inference',
            'evidence':[{'reference':r,'stance':'supports'} for r in expected],
            'recovery_obligations':[{'candidate_id':'candidate_a','decision':'resolved','evidence_refs':list(expected)}]})
    assert set(refs)<=set(review_semantics(SimpleNamespace(complete=complete),packet,{'kind':'wide_hierarchy'},docs))
    assert len(calls)==2


def test_unavailable_historical_probe_never_uses_coincident_current_probe_id():
    from lightgraph.discovery.recovery_evidence import prepare_grain_recovery
    from lightgraph.tests.test_object_model_supersession import fixture
    from lightgraph.tests.test_schema_knowledge import packet as make_packet
    table,_,obj=fixture();packet=make_packet(table)
    packet['probe_digest']=[{'probe_id':'probe_0001','operator':'profile_columns','aggregates':{'rows':999}}]
    class Tools:
        sequence=0
        def reserve_probe_ids(self,ids):self.reserved=set(ids)
        def execute(self,name,args):
            self.sequence+=1
            return {'status':'ok','probe_id':f'probe_current_{self.sequence}',
                'coverage':{'result_complete':True},'aggregates':{'rows':1}}
    tools=Tools();candidate=rejected_grain(table,[])
    with pytest.raises(LightGraphError,match='semantic_recovery_evidence_required'):
        prepare_grain_recovery(packet,table,[obj],[candidate],tools)
    assert tools.sequence==0  # Ambiguous evidence never reaches independent review.


def test_malformed_negative_recovery_cannot_start_schema_repair():
    import json
    from lightgraph.discovery.llm import _invalid_review_terminal
    assert _invalid_review_terminal({'content':json.dumps({'decision':'supported','extra':'invalid',
        'recovery_obligations':[{'candidate_id':'a','decision':'unresolved'}]})})=='semantic_recovery_obligation_unresolved'


def test_every_fresh_executor_reserves_blocking_required_and_historical_probe_ids():
    from lightgraph.contracts import ExtractionGoal
    from lightgraph.discovery.budget import BudgetAllocator
    from lightgraph.discovery.recovery_evidence import reserved_evidence_probe_ids
    from lightgraph.discovery.tools import ToolExecutor
    from lightgraph.tests.test_agent_pipeline import table
    current=table('items',['code','name'])
    packet={'probe_digest':[{'probe_id':'probe_0001'}], 'iteration_context':{
        'pending_candidates':[{'required_evidence_refs':['probe_0002']}],
        'field_model_revision_guard':{'blocking_evidence_refs':['probe_0003'],
            'recovery_obligations':[{'historical_probe_refs':['probe_0004']}]}}}
    with MemorySource([current],{current.qualified_name:[]}) as source:
        for _ in range(2):
            tools=ToolExecutor(source,ExtractionGoal(),BudgetAllocator(),allowed_tables=[current])
            try:
                tools.reserve_probe_ids(reserved_evidence_probe_ids(packet))
                response=tools.execute('get_metadata',{'table':current.qualified_name})
                assert response['status']=='ok' and response['probe_id']=='probe_0005'
            finally:tools.close()


def test_independent_position_models_only_review_their_own_rejection():
    from lightgraph.discovery.recovery_evidence import select_recovery_guard,validate_recovery_shape
    def obligation(cid,key,label):
        return {'candidate_id':cid,'prior_mapping':{'entity_kind':'ReportItem',
            'fields':[{'role':'key','columns':[key,key+'2']}]},
            'replacement_entity_kind':'ReportItem','replacement_namespaces':['scope'],
            'replacement_positions':[{'position':i,'code':col,'label':label+col,'aliases':[]} for i,col in enumerate([key,key+'2'],1)],
            'required_evidence_refs':['doc@1:section','probe_'+cid]}
    a=obligation('a','a','name_');b=obligation('b','b','name_')
    guard={'blocking_evidence_refs':['doc@1:section','probe_a','probe_b'],
        'conditions':[],'recovery_obligations':[a,b]}
    for expected in (a,b):
        proposal=SimpleNamespace(kind='wide_hierarchy',claim=SimpleNamespace(
            levels=[SimpleNamespace(position=p['position'],key=p['code'],label=p['label'],aliases=[])
                for p in expected['replacement_positions']],
            entity_kind_hint='ReportItem',path_semantics='relative_path',namespace_columns=['scope']))
        selected=select_recovery_guard(proposal,guard)
        assert [o['candidate_id'] for o in selected['recovery_obligations']]==[expected['candidate_id']]
        assert set(selected['blocking_evidence_refs'])==set(expected['required_evidence_refs'])
        validate_recovery_shape(proposal,selected)
    assert len(guard['recovery_obligations'])==2  # Neither sibling work item was consumed.


@pytest.mark.parametrize('kind,keys',[
    ('RenamedItem',['a','b','c']),
    ('ReportItem',['a','b']),
    ('RenamedItem',['a','b']),
])
def test_type_rename_or_partial_key_removal_cannot_drop_recovery_obligations(kind,keys):
    from lightgraph.discovery.recovery_evidence import select_recovery_guard,validate_recovery_shape
    obligation={'candidate_id':'candidate_a','prior_mapping':{'entity_kind':'ReportItem',
        'fields':[{'role':'key','columns':['a','b','c']}]},
        'replacement_entity_kind':'ReportItem','replacement_namespaces':['scope'],
        'replacement_positions':[{'position':i,'code':key,'label':'name_'+key,'aliases':[]}
            for i,key in enumerate(['a','b','c'],1)],
        'required_evidence_refs':['probe_current']}
    guard={'blocking_evidence_refs':['probe_current'],'conditions':[],
        'recovery_obligations':[obligation]}
    proposal=SimpleNamespace(kind='wide_hierarchy',claim=SimpleNamespace(
        levels=[SimpleNamespace(position=i,key=key,label='name_'+key,aliases=[])
            for i,key in enumerate(keys,1)],
        entity_kind_hint=kind,path_semantics='relative_path',namespace_columns=['scope']))
    selected=select_recovery_guard(proposal,guard)
    assert selected['recovery_obligations']==[obligation]
    with pytest.raises(LightGraphError,match='semantic_recovery_evidence_required'):
        validate_recovery_shape(proposal,selected)

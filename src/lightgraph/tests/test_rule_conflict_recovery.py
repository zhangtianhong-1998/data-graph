"""Conflicts retain repairable mappings without replaying untrusted values."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from lightgraph.contracts import ExtractionGoal,RuleSpec,canonical
from lightgraph.discovery.conflicts import (candidate_dependency,execution_candidate,
    merge_compatible_rules,prepare_candidate_context)
from lightgraph.discovery.engine import _column_id,discover
from lightgraph.discovery.models import DiscoveryResult,ProposalBatch
from lightgraph.discovery.semantics import SemanticReview
from lightgraph.discovery.workflow import _compile_candidates
from lightgraph.documents import DocumentCorpus
from lightgraph.rules import RuleEngine
from lightgraph.runtime.candidates import CandidateQueue
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table,client,input_packet,response,group


def test_channel_cross_group_and_tool_kind_merge_and_execute(monkeypatch):
    t=table('channels',['code','name','english']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T0_discovery'
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        first=group(packet,'code','name')['proposals'][0]
        second={'kind':'entity_key','claim':{'group_id':'additional_names','entity_kind_hint':'ReportItem',
            'key_columns':[ids['code']],'attributes':{'single_valued':[ids['name']],
                'multi_valued':[ids['english']],'property_names':{ids['english']:'english_name'}}},
            'evidence_cited':[ids['code']]}
        return response({'task':packet['task'],'proposals':[first,second]})
    with MemorySource([t],{'public.channels':[('C01','批发','Wholesale'),('C02','零售','Retail')]}) as source,GraphStore() as store:
        result=discover(source,model=client(monkeypatch,handler))
        assert len(result.rules)==1 and result.stats['compatible_rule_merges']==1
        assert RuleEngine(store).execute(source,result.rules[0]).published
        graph=store.view({'public'})
        assert {n['label'] for n in graph['nodes']}=={'批发','零售'}
        assert {n['properties']['english_name'] for n in graph['nodes']}=={'Wholesale','Retail'}
    assert tasks==['T0_discovery'] and not result.pending_candidates


def conflict_setup():
    t=table('objects',['code','name','english','other_code','other_name','audit'])
    ids={c.name:_column_id(t,c.name) for c in t.columns}
    packet={'task':'T0_discovery','metadata_digest':{'columns':[{'id':v,'name':k} for k,v in ids.items()]},
        'iteration_context':{'candidate_dependency':candidate_dependency(t)},'probe_digest':[]}
    first=group(packet,'code','code')['proposals'][0];first['claim']['group_id']='code_label'
    second=group(packet,'code','name')['proposals'][0];second['claim']['group_id']='human_label'
    good=group(packet,'other_code','other_name')['proposals'][0];good['claim']['group_id']='independent'
    first['rationale']='SOURCE_VALUE SQL SELECT secret FROM private_table'
    return t,ids,packet,ProposalBatch.model_validate({'task':'T0_discovery','proposals':[first,second,good]})


class Tools:
    def context_documents(self):return []
    def close(self):pass


@pytest.mark.parametrize('mode',['same','rename','new_evidence','changed_mapping','abstain'])
def test_one_conflict_repair_requires_progress_and_independent_review(mode):
    t,ids,packet,batch=conflict_setup();result=DiscoveryResult();calls=[]
    class Model:
        config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
        def explore(self,p,tools,**kwargs):
            calls.append(p['task']);requests=p['iteration_context']['rule_conflict_requests']
            assert len(requests)==1 and len(requests[0]['alternatives'])==2
            assert 'SOURCE_VALUE' not in canonical(requests)
            if mode=='abstain':return ProposalBatch(task='T5_revision',abstain=True)
            fixed=batch.proposals[1].model_copy(deep=True)
            if mode=='rename':fixed.claim.group_id='renamed'
            if mode=='new_evidence':
                p['probe_digest'].append({'probe_id':'probe_1','operator':'check_fd',
                    'aggregates':{'violations':0,'complete':True}})
                fixed.evidence_cited.append('probe_1')
            if mode=='changed_mapping':fixed.claim.members[-1].column_id=ids['english']
            return ProposalBatch(task='T5_revision',proposals=[fixed])
        def complete(self,task,p,response_model):
            calls.append(task)
            assert p['iteration_context']['rule_conflict_requests']
            return SemanticReview(proposition_ref=p['semantic_proposition_ref'],decision='supported',
                basis='data_inference',evidence=[{'reference':ids['name'],'stance':'supports'}])
    rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    progress=mode in {'new_evidence','changed_mapping'}
    assert len(rules)==(2 if progress else 1)
    assert rules[0].identity.key_columns==['other_code']
    assert calls==(['T5_revision','semantic_review'] if progress else ['T5_revision'])
    assert len(result.pending_candidates)==1
    candidate=result.pending_candidates[0]
    assert len(candidate.mappings)==2 and candidate.differences==['label_mapping']
    assert 'SOURCE_VALUE' not in candidate.model_dump_json() and 'SELECT' not in candidate.model_dump_json()
    if progress:
        assert rules[-1].evidence.semantic_status=='supported'
        assert result.resolved_candidate_ids==[candidate.candidate_id]
        assert result.stats['rule_conflict_revisions_compiled']==1
        assert all(i['status']=='resolved' for i in result.issues if i['code']=='conflicting_rule_proposals')
        packet['iteration_context']['pending_candidates']=[candidate.model_dump(mode='json',by_alias=True)]
        selected=batch.proposals[1].model_copy(deep=True)
        if mode=='changed_mapping':selected.claim.members[-1].column_id=ids['english']
        repeated=DiscoveryResult()
        again,*_=_compile_candidates(batch.model_copy(update={'proposals':[selected,batch.proposals[-1]]}),
            t,ExtractionGoal(),[],repeated,model=Model(),packet=packet,observations=None,tool_factory=Tools)
        assert len(again)==1 and calls==['T5_revision','semantic_review']


def test_pending_conflict_restores_mappings_and_unchanged_material_does_not_vote_again(tmp_path):
    t,ids,packet,batch=conflict_setup();result=DiscoveryResult();calls=[]
    class Model:
        config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
        def explore(self,p,tools,**kwargs):
            calls.append(p['task']);return ProposalBatch(task='T5_revision',abstain=True)
    model=Model()
    _compile_candidates(batch,t,ExtractionGoal(),[],result,model=model,packet=packet,
        observations=None,tool_factory=Tools)
    path=tmp_path/'pending.sqlite'
    with GraphStore(path) as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'test');queue.record(result)
    with GraphStore(path) as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'test')
        queue.refresh_context({('s','public','objects'):candidate_dependency(t)})
        pending=queue.context({('s','public','objects')})
        assert len(pending)==1 and pending[0]['mappings'][0]['fields']
        packet['iteration_context']['pending_candidates']=pending
        next_result=DiscoveryResult()
        rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],next_result,model=model,packet=packet,
            observations=None,tool_factory=Tools)
        assert calls==['T5_revision'] and len(rules)==1
        assert next_result.issues[0]['code']=='rule_conflict_unchanged'
        queue.refresh_context({('s','public','objects'):candidate_dependency(t,context_version='new-document')})
        assert queue.context({('s','public','objects')})==[] and queue.counts()=={'stale':1}
        assert CandidateQueue(store,ExtractionGoal(),['private'],'test').context({('s','public','objects')})==[]


def test_candidate_dependency_and_prompt_retire_schema_goal_or_document_changes():
    t,_,packet,batch=conflict_setup();result=DiscoveryResult()
    _compile_candidates(batch,t,ExtractionGoal(),[],result,model=None,packet=packet,observations=None)
    candidate=result.pending_candidates[0]
    packet['iteration_context']['pending_candidates']=[candidate.model_dump(mode='json',by_alias=True)]
    altered=ExtractionGoal(excluded_columns=['name'])
    assert candidate_dependency(t)!=candidate_dependency(t,altered)
    prepare_candidate_context(packet,t,altered,DocumentCorpus([]))
    assert packet['iteration_context']['pending_candidates']==[]
    changed=t.model_copy(deep=True);changed.columns.pop()
    assert candidate_dependency(t)!=candidate_dependency(changed)


def test_merge_is_order_independent_and_retains_component_evidence():
    left=RuleSpec(rule_id='same',kind='column_group_entity',source={'source_id':'s','table':'t'},
        identity={'key_columns':['code']},attributes={'multi_valued':{'alias':'english'}})
    right=left.model_copy(deep=True);right.attributes.multi_valued['alias']='alternate'
    assert merge_compatible_rules([left,right])==merge_compatible_rules([right,left])
    from lightgraph.tests.test_concept_mapping import coded_rule
    left=coded_rule();right=left.model_copy(deep=True)
    left.concept_mapping.components[0]=left.concept_mapping.components[0].model_copy(update={
        'identity_domain':'metric_codes','identity_evidence':['decl@1:first']})
    right.concept_mapping.components[0]=right.concept_mapping.components[0].model_copy(update={
        'identity_domain':'metric_codes','identity_evidence':['decl@1:second']})
    merged=merge_compatible_rules([left,right])
    assert merged is not None
    assert merged.concept_mapping.components[0].identity_evidence==['decl@1:first','decl@1:second']
    assert merged==merge_compatible_rules([right,left])


def test_execution_candidate_keeps_policies_and_exclusions_not_conditions():
    t=table('t',['code','name','audit'])
    rule=RuleSpec(rule_id='known',kind='column_group_entity',source={'source_id':'s','table':'t'},
        scope={'predicate':{'column':'name','op':'eq','value':'SOURCE_VALUE'}},
        identity={'key_columns':['code']},attributes={'single_valued':{'label':'name'},
            'excluded':[{'column':'audit','reason':'display_order'}]})
    candidate=execution_candidate(rule,['fd_violation','RAW_PRIVATE_ERROR'],table=t,
        goal=ExtractionGoal(excluded_columns=['audit']))
    assert candidate and candidate.failure_details==['fd_violation']
    assert candidate.mappings[0].identity_resolution=='source_scoped'
    assert 'scope_condition' in candidate.mappings[0].required_reread
    assert any(f.role=='excluded' and _column_id(t,'audit') in f.columns for f in candidate.mappings[0].fields)
    assert 'SOURCE_VALUE' not in candidate.model_dump_json() and 'RAW_PRIVATE_ERROR' not in candidate.model_dump_json()
    assert execution_candidate(rule,['RAW_PRIVATE_ERROR'],table=t) is None


def test_only_one_conflict_is_sent_and_revision_submission_feedback_is_recorded():
    t,ids,packet,batch=conflict_setup();result=DiscoveryResult();recorded=[]
    other=batch.proposals[-1].model_copy(deep=True)
    other.claim.group_id='other_conflicting_label';other.claim.members[-1].column_id=ids['other_code']
    batch.proposals.append(other)
    class Model:
        config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
        def explore(self,p,tools,**kwargs):
            assert len(p['iteration_context']['rule_conflict_requests'])==1
            response=ProposalBatch(task='T5_revision',abstain=True)
            response._runtime_invalid_drafts=1
            response._runtime_invalid_codes={'invalid_proposal_arguments':1}
            response._runtime_object_models=[{'object_id':'revised'}]
            return response
    rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools,on_revision=lambda b,p:recorded.extend(b._runtime_object_models))
    assert not rules and len(result.pending_candidates)==2
    assert result.stats['rule_conflict_revision_attempts']==1 and result.stats['discovery_invalid_drafts']==1
    assert len([p for p in result.pending_candidates if p.attempted_fingerprints])==1
    assert recorded==[{'object_id':'revised'}]
    assert any(i['code']=='rule_conflict_revision_deferred' for i in result.issues)


def test_protocol_failure_retains_structured_mapping_without_semantic_revote():
    from lightgraph.errors import LightGraphError
    from lightgraph.tests.test_semantic_revision import setup_batch
    t,batch,packet=setup_batch();result=DiscoveryResult();calls=[]
    class Model:
        config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
        def complete(self,task,p,response_model):
            calls.append(task);raise LightGraphError('semantic_review_protocol_invalid')
        def explore(self,*args,**kwargs):raise AssertionError('Protocol errors do not create semantic votes')
    rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    assert not rules and calls==['semantic_review']
    candidate=result.pending_candidates[0]
    assert candidate.failure_code=='semantic_review_protocol_invalid'
    assert {f.role for f in candidate.mappings[0].fields}>={'key','parent','attribute'}


def test_repeating_an_identical_probe_with_a_new_id_is_not_new_evidence():
    from lightgraph.discovery.conflicts import evidence_fingerprint
    probe={'probe_id':'probe_1','operator':'check_fd','aggregates':{'violations':0}}
    first={'probe_digest':[probe]}
    second={'probe_digest':[probe,{**probe,'probe_id':'probe_99'}]}
    assert evidence_fingerprint(first,[])==evidence_fingerprint(second,[])
    second['probe_digest'].append({'probe_id':'probe_100','operator':'get_metadata','aggregates':{}})
    assert evidence_fingerprint(first,[])==evidence_fingerprint(second,[])


def seeded_conflict():
    t,ids,packet,batch=conflict_setup();result=DiscoveryResult()
    class Model:
        config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
        def explore(self,*args,**kwargs):return ProposalBatch(task='T5_revision',abstain=True)
    _compile_candidates(batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    packet['iteration_context']['pending_candidates']=[p.model_dump(mode='json',by_alias=True)
        for p in result.pending_candidates]
    return t,ids,packet,batch


@pytest.mark.parametrize('variant',['code','name','different_tool'])
def test_restart_cannot_drop_competing_interpretation_to_publish_old_single_variant(variant):
    t,ids,packet,batch=seeded_conflict();result=DiscoveryResult()
    chosen=batch.proposals[0 if variant=='code' else 1]
    if variant=='different_tool':
        chosen=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[{
            'kind':'entity_key','claim':{'group_id':'new_tool','entity_kind_hint':'ReportItem',
                'key_columns':[ids['code']],'attributes':{'single_valued':[ids['name']]}},
            'evidence_cited':[ids['code']]}]}).proposals[0]
    class Model:
        def complete(self,*args,**kwargs):raise AssertionError('Unchanged conflict must not solicit another vote')
        def explore(self,*args,**kwargs):raise AssertionError('Unchanged conflict must not solicit another choice')
    rules,*_=_compile_candidates(batch.model_copy(update={'proposals':[chosen,batch.proposals[-1]]}),
        t,ExtractionGoal(),[],result,model=Model(),packet=packet,observations=None,tool_factory=Tools)
    assert len(rules)==1 and rules[0].identity.key_columns==['other_code']
    assert any(i['code']=='rule_conflict_unchanged' for i in result.issues)
    assert len(result.pending_candidates)==1 and not result.resolved_candidate_ids


@pytest.mark.parametrize('change',['mapping','evidence'])
def test_restart_with_new_material_reviews_old_alternatives_before_accepting_one(change):
    t,ids,packet,batch=seeded_conflict();result=DiscoveryResult();calls=[]
    chosen=batch.proposals[1].model_copy(deep=True)
    if change=='mapping':chosen.claim.members[-1].column_id=ids['english']
    else:packet['probe_digest'].append({'probe_id':'probe_new','operator':'check_fd',
        'aggregates':{'violations':0,'complete':True}})
    class Model:
        def complete(self,task,p,response_model):
            calls.append(task)
            requests=p['iteration_context']['rule_conflict_requests']
            assert len(requests)==1 and len(requests[0]['candidate']['mappings'])==2
            assert p['iteration_context']['pending_candidates']==[]
            return SemanticReview(proposition_ref=p['semantic_proposition_ref'],decision='supported',
                basis='data_inference',evidence=[{'reference':ids['name'],'stance':'supports'}])
        def explore(self,*args,**kwargs):raise AssertionError('A revised selection goes directly to independent review')
    next_batch=batch.model_copy(update={'proposals':[chosen,batch.proposals[-1]]})
    rules,*_=_compile_candidates(next_batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    assert calls==['semantic_review'] and len(rules)==2
    assert result.resolved_candidate_ids==[result.pending_candidates[0].candidate_id]
    assert all(i['status']=='resolved' for i in result.issues if i['code']=='conflicting_rule_proposals')
    # If publication fails, the next identical material still cannot solicit a
    # fresh semantic vote. A stored mapping is not itself publication approval.
    packet['iteration_context']['pending_candidates']=[p.model_dump(mode='json',by_alias=True)
        for p in result.pending_candidates]
    repeated=DiscoveryResult()
    rules,*_=_compile_candidates(next_batch,t,ExtractionGoal(),[],repeated,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    assert calls==['semantic_review'] and len(rules)==1


def test_wide_logical_rule_identity_matches_compiler_even_for_reordered_levels():
    from lightgraph.discovery.conflicts import proposal_logical_id
    from lightgraph.discovery.engine import _compile_model
    t=table('paths',['a','b','an','bn']);ids={c.name:_column_id(t,c.name) for c in t.columns}
    batch=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[{'kind':'wide_hierarchy',
        'claim':{'group_id':'path','levels':[{'position':2,'key':ids['b'],'label':ids['bn']},
            {'position':1,'key':ids['a'],'label':ids['an']}]},'evidence_cited':[ids['a']]}]})
    rule=_compile_model(batch,t,ExtractionGoal(),[],set(),semantic_evidence=[ids['a']])[0]
    assert proposal_logical_id(batch.proposals[0],t)==rule.rule_id


@pytest.mark.parametrize('code',['concept_key_label_conflict','null_key','hierarchy_self_loop'])
def test_execution_failures_keep_repairable_mapping_for_known_data_checks(code):
    t=table('t',['code','name'])
    rule=RuleSpec(rule_id='known',kind='column_group_entity',source={'source_id':'s','table':'t'},
        identity={'key_columns':['code']},attributes={'single_valued':{'label':'name'}})
    candidate=execution_candidate(rule,[code],table=t)
    assert candidate and candidate.failure_details==[code] and candidate.mappings


def test_invalid_concept_cannot_escape_normal_error_handling_during_conflict_recovery(monkeypatch):
    from lightgraph.discovery.conflicts import proposal_logical_id
    t,ids,packet,batch=seeded_conflict()
    concept=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[{'kind':'concept_mapping',
        'claim':{'group_id':'binding','entity_kind_hint':'ConceptBinding','key_columns':[ids['code']],
            'evidence_ref':'decl@1','components':[
                {'role':'metric','entity_kind':'Metric','column':ids['name'],
                 'definition_ref':'metric','relation':'binds_metric'},
                {'role':'product','entity_kind':'Product','column':ids['english'],
                 'definition_ref':'product','relation':'binds_product'}]},'evidence_cited':[ids['code']]}]}).proposals[0]
    # Defensive coverage for internal mutations after proposal validation.
    concept.claim.components[1].relation='binds_metric'
    assert proposal_logical_id(concept,t) is None
    monkeypatch.setattr('lightgraph.discovery.workflow.review_semantics',lambda *args,**kwargs:[ids['name']])
    result=DiscoveryResult()
    rules,*_=_compile_candidates(batch.model_copy(update={'proposals':[concept,batch.proposals[-1]]}),
        t,ExtractionGoal(),[{'doc_id':'decl','revision':'1','section_id':'body','text':''}],result,
        model=object(),packet=packet,observations=None)
    assert len(rules)==1 and rules[0].identity.key_columns==['other_code']
    assert any(p.failure_code=='duplicate_concept_relation' for p in result.pending_candidates)

"""An interrupted request retains work, but neither approves nor refutes it."""
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.conflicts import prepare_candidate_context
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import DiscoveryResult, ProposalBatch
from lightgraph.discovery.semantics import SemanticReview, SemanticReferencePatch, review_semantics
from lightgraph.discovery.workflow import _compile_candidates
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.runtime.candidates import CandidateQueue
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table, group


def setup():
    t=table('items',['key','parent','label','other_code','other_name'])
    ids={name:_column_id(t,name) for name in t.column_names}
    packet={'task':'T0_discovery','metadata_digest':{'columns':[{'id':v,'name':k} for k,v in ids.items()]}}
    prepare_candidate_context(packet,t,ExtractionGoal(),DocumentCorpus([]))
    hierarchy={'kind':'long_hierarchy','claim':{'group_id':'path','key_column':ids['key'],
        'parent_column':ids['parent'],'label_column':ids['label']},'evidence_cited':[ids['key']]}
    sibling=group(packet,'other_code','other_name')['proposals'][0]
    return t,ProposalBatch.model_validate({'task':'T0_discovery','proposals':[hierarchy,sibling]}),packet


class Model:
    config=SimpleNamespace(max_discovery_steps=2,max_tool_calls=2)
    def __init__(self,code=None):self.code=code;self.calls=[]
    def complete(self,task,packet,response_model):
        self.calls.append(task)
        if self.code:
            raise (BudgetExceeded(self.code) if 'budget_exhausted' in self.code else LightGraphError(self.code))
        ref=packet['metadata_digest']['columns'][0]['id']
        return SemanticReview(proposition_ref=packet['semantic_proposition_ref'],decision='supported',
            basis='data_inference',evidence=[{'reference':ref,'stance':'supports'}])
    def explore(self,*args,**kwargs):
        raise AssertionError('A request interruption must not demand a changed semantic proposal')


def compile_batch(t,batch,packet,model):
    result=DiscoveryResult()
    rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],result,model=model,packet=packet,
        observations=None,tool_factory=None)
    result.rules=rules
    return result


@pytest.mark.parametrize('code',['model_transport_error','wall_time_budget_exhausted',
    'llm_calls_budget_exhausted','model_response_truncated'])
def test_interrupted_review_is_saved_and_same_mapping_retried_after_restart(tmp_path,code):
    t,batch,packet=setup();model=Model(code)
    initial=compile_batch(t,batch,packet,model)
    assert model.calls==['semantic_review']
    assert len(initial.rules)==1 and initial.rules[0].kind=='column_group_entity'
    pending=initial.pending_candidates[0]
    assert pending.failure_code=='semantic_review_interrupted' and pending.failure_details==[code]
    assert pending.review_stage=='hierarchy' and not pending.blocking_evidence_refs
    assert not initial.resolved_candidate_ids and not initial.stats.get('semantic_revision_attempts')
    path=tmp_path/'graph.sqlite'
    with GraphStore(path) as store:
        CandidateQueue(store,ExtractionGoal(),['public'],'d').record(initial)
    with GraphStore(path) as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        packet['iteration_context']['pending_candidates']=queue.context({(t.source_id,t.schema_name,t.name)})
        prepare_candidate_context(packet,t,ExtractionGoal(),DocumentCorpus([]))
        request=packet['iteration_context']['interrupted_review_requests'][0]
        assert request['requires_structural_change'] is False
        retry=Model();result=compile_batch(t,batch,packet,retry)
        assert retry.calls==['semantic_review'] and len(result.rules)==2
        assert result.resolved_candidate_ids==[pending.candidate_id]
        assert not result.pending_candidates
        queue.record(result)
        assert queue.counts()=={'revised':1}  # Compilation alone is not publication.
        rule=next(r for r in result.rules if r.kind=='long_hierarchy')
        with MemorySource([t],{t.qualified_name:[('root',None,'Root','one','One'),
                ('child','root','Child','two','Two')]}) as source:
            assert RuleEngine(store).execute(source,rule).published
        queue.confirm_publications([rule.rule_id])
        assert queue.counts()=={'resolved':1}


def test_unrelated_compile_or_policy_failure_is_not_reclassified_as_review_interruption():
    t,batch,packet=setup()
    result=compile_batch(t,batch,packet,Model('agent_source_persistence_forbidden'))
    assert len(result.rules)==1 and not result.pending_candidates
    assert any(i['code']=='agent_source_persistence_forbidden' for i in result.issues)


@pytest.mark.parametrize('failure',['semantic_claim_refuted','semantic_recovery_obligation_unresolved',
    'semantic_revision_no_progress'])
def test_request_failure_cannot_overwrite_existing_refutation_or_counterevidence(failure):
    t,batch,packet=setup()
    interrupted=compile_batch(t,batch,packet,Model('model_transport_error')).pending_candidates[0]
    original=interrupted.model_copy(update={'failure_code':failure,'failure_details':[],
        'blocking_evidence_refs':[packet['metadata_digest']['columns'][0]['id']],
        'required_evidence_refs':[packet['metadata_digest']['columns'][1]['id']]})
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        queue.record(DiscoveryResult(pending_candidates=[original]))
        queue.record(DiscoveryResult(pending_candidates=[interrupted]))
        kept=queue.listing()['candidates'][0]
        assert kept['failure_code']==failure
        assert kept['blocking_evidence_refs']==original.blocking_evidence_refs and kept['revision']==1
        assert kept['required_evidence_refs']==original.required_evidence_refs


def test_interrupted_condition_guard_must_be_reconstructed_before_fresh_review():
    t,batch,packet=setup()
    batch.proposals=batch.proposals[:1]
    ref=packet['metadata_digest']['columns'][0]['id']
    packet['semantic_revision_guard']={'blocking_evidence_refs':[ref],
        'conditions':[{'condition_ref':'condition_1','condition':'PRIVATE condition text',
            'prior_state':'unknown','evidence_refs':[ref]}]}
    initial=compile_batch(t,batch,packet,Model('model_transport_error'))
    candidate=initial.pending_candidates[0]
    assert candidate.review_guard_fingerprint and candidate.blocking_evidence_refs==[ref]
    assert ref in candidate.required_evidence_refs
    assert 'PRIVATE' not in candidate.model_dump_json()
    packet['iteration_context']['pending_candidates']=[candidate.model_dump(mode='json',by_alias=True)]
    guard=packet.pop('semantic_revision_guard')
    retry=Model();deferred=compile_batch(t,batch,packet,retry)
    assert not retry.calls and not deferred.rules
    assert deferred.pending_candidates==[candidate] and not deferred.resolved_candidate_ids
    packet['semantic_revision_guard']=guard
    again=compile_batch(t,batch,packet,Model('model_transport_error'))
    assert again.pending_candidates[0].review_guard_fingerprint==candidate.review_guard_fingerprint
    assert again.pending_candidates[0].blocking_evidence_refs==candidate.blocking_evidence_refs
    class Reviewer(Model):
        def complete(self,task,p,response_model):
            self.calls.append(task)
            assert p['semantic_revision_guard']['conditions']==guard['conditions']
            return SemanticReview(proposition_ref=p['semantic_proposition_ref'],decision='supported',
                basis='data_inference',evidence=[{'reference':ref,'stance':'supports'}],
                conditions=[{'condition_ref':'condition_1','state':'satisfied',
                    'explanation':'PRIVATE condition text','evidence_refs':[ref]}])
    retry=Reviewer();accepted=compile_batch(t,batch,packet,retry)
    assert retry.calls==['semantic_review'] and len(accepted.rules)==1
    assert accepted.resolved_candidate_ids==[candidate.candidate_id]


@pytest.mark.parametrize('includes_probe',[True,False])
def test_required_probe_is_exposed_and_must_be_classified_without_becoming_counterevidence(includes_probe):
    packet={'metadata_digest':{'columns':[{'id':'c_key','name':'code'}]},
        'probe_digest':[{'probe_id':'probe_required'}],
        'semantic_revision_guard':{'blocking_evidence_refs':[],
            'required_evidence_refs':['probe_required']}}
    calls=[]
    def complete(task,body,schema):
        calls.append(schema)
        assert body['required_evidence_refs']==['probe_required']
        assert body['semantic_revision_guard']['blocking_evidence_refs']==[]
        if schema is SemanticReferencePatch:
            assert body['semantic_review_repair']['required_evidence_refs']==['probe_required']
            return SemanticReferencePatch()  # An incomplete patch must not approve it.
        evidence=[{'reference':'c_key','stance':'supports'}]
        if includes_probe:evidence.append({'reference':'probe_required','stance':'context'})
        return SemanticReview(proposition_ref=body['semantic_proposition_ref'],decision='supported',
            basis='data_inference',evidence=evidence)
    model=SimpleNamespace(complete=complete)
    if includes_probe:
        assert 'c_key' in review_semantics(model,packet,{'kind':'long_hierarchy'},[])
        assert calls==[SemanticReview]
    else:
        with pytest.raises(LightGraphError,match='semantic_review_incomplete_context|semantic_revision_evidence_missing'):
            review_semantics(model,packet,{'kind':'long_hierarchy'},[])
        assert calls==[SemanticReview,SemanticReferencePatch]


@pytest.mark.parametrize('windows',[1,2])
def test_unissued_required_probe_is_rejected_before_any_review_call(monkeypatch,windows):
    import lightgraph.context
    docs=[{'doc_id':str(i),'revision':'1','section_id':'definition','text':'definition',
        'read_state':'complete'} for i in range(windows)]
    monkeypatch.setattr(lightgraph.context,'document_batches',lambda documents:iter([[doc] for doc in docs]))
    packet={'semantic_revision_guard':{'required_evidence_refs':['probe_not_issued']}}
    def complete(*args):raise AssertionError('Unissued evidence must be rejected before the model call')
    with pytest.raises(LightGraphError,match='semantic_recovery_evidence_required'):
        review_semantics(SimpleNamespace(complete=complete),packet,{'kind':'long_hierarchy'},docs)


def test_required_documents_are_windowed_but_required_probe_is_present_in_each_review(monkeypatch):
    import lightgraph.context
    docs=[{'doc_id':str(i),'revision':'1','section_id':'definition','text':'definition',
        'purpose':'data_description','evidence_role':'definition_evidence','read_state':'complete'} for i in range(2)]
    monkeypatch.setattr(lightgraph.context,'document_batches',lambda documents:iter([[doc] for doc in docs]))
    refs=['0@1:definition','1@1:definition'];calls=[]
    packet={'probe_digest':[{'probe_id':'probe_required'}],
        'semantic_revision_guard':{'blocking_evidence_refs':[],
            'required_evidence_refs':refs+['probe_required']}}
    def complete(task,body,schema):
        current=refs[len(calls)];calls.append(body['required_evidence_refs'])
        assert set(body['required_evidence_refs'])=={current,'probe_required'}
        assert body['semantic_revision_guard']['blocking_evidence_refs']==[]
        return SemanticReview(proposition_ref=body['semantic_proposition_ref'],decision='supported',
            basis='data_inference',evidence=[{'reference':current,'stance':'supports'},
                {'reference':'probe_required','stance':'context'}])
    assert set(refs)<=set(review_semantics(SimpleNamespace(complete=complete),packet,{'kind':'long_hierarchy'},docs))
    assert len(calls)==2

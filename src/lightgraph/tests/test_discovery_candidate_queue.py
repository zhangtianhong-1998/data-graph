import pytest
from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.models import DiscoveryResult, PendingCandidate
from lightgraph.errors import LightGraphError
from lightgraph.runtime.candidates import CandidateQueue
from lightgraph.store import GraphStore


def candidate():
    return PendingCandidate(candidate_id='candidate_'+'a'*24,source={'source_id':'s','table':'t'},
        proposal_kind='wide_hierarchy',failure_code='semantic_claim_unresolved',review_stage='hierarchy',
        column_refs=['c1'],document_refs=['doc@1:section'])


def rejected_candidate():
    return candidate().model_copy(update={'proposal_kind':'reference','review_stage':'reference',
        'failure_code':'reference_contract_unresolved',
        'failure_details':['relation_endpoint_kind_mismatch'], 'status':'rejected',
        'dependency_fingerprint':'a'*64})


def test_closed_reference_candidate_is_not_replayed_without_material_change_or_explicit_retry():
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        closed=rejected_candidate()
        queue.record(DiscoveryResult(pending_candidates=[closed]))
        item=queue.listing()['candidates'][0]
        assert item['state']=='rejected' and queue.counts()=={'rejected':1}
        assert queue.context({('s','public','t')})==[]
        queue.record(DiscoveryResult(pending_candidates=[closed]))
        queue.record(DiscoveryResult(pending_candidates=[closed.model_copy(update={'status':'pending'})]))
        queue.record(DiscoveryResult(resolved_candidate_ids=[closed.candidate_id]))
        assert queue.listing()['candidates'][0]['revision']==item['revision']
        assert queue.listing()['candidates'][0]['state']=='rejected'
        with pytest.raises(LightGraphError,match='discovery_candidate_closed'):
            queue.decide(closed.candidate_id,'reopen',item['revision'])
        queue.decide(closed.candidate_id,'retry',item['revision'])
        assert len(queue.retry_sources())==1
        assert queue.context({('s','public','t')})[0]['status']=='pending'
        queue.consume_retries('s',{'public.t'})
        # A new API view must not consume an already running explicit retry.
        again=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        assert again.listing()['candidates'][0]['state']=='pending'
        queue.record(DiscoveryResult(pending_candidates=[closed]))
        assert queue.listing()['candidates'][0]['state']=='rejected'
        assert not store.heads()


def test_legacy_pending_contract_migrates_but_unfinished_semantic_review_does_not():
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        old=rejected_candidate().model_copy(update={'status':'pending'})
        queue.record(DiscoveryResult(pending_candidates=[old]))
        review=candidate().model_copy(update={'candidate_id':'candidate_'+'b'*24,
            'failure_code':'semantic_review_protocol_invalid'})
        queue.record(DiscoveryResult(pending_candidates=[review]))
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        assert queue.counts()=={'pending':1,'rejected':1}
        assert [c['failure_code'] for c in queue.context({('s','public','t')})]==['semantic_review_protocol_invalid']
        queue.refresh_context({('s','public','t'):'b'*64})
        assert queue.counts()=={'pending':1,'stale':1}
        changed=old.model_copy(update={'dependency_fingerprint':'b'*64})
        queue.record(DiscoveryResult(pending_candidates=[changed]))
        assert queue.counts()=={'pending':2}


def test_semantic_or_operational_failure_cannot_be_labeled_as_a_closed_contract():
    from pydantic import ValidationError
    for code in ('semantic_claim_unresolved','semantic_review_protocol_invalid'):
        value=candidate().model_copy(update={'failure_code':code,'status':'rejected'})
        with GraphStore() as store:
            queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
            with pytest.raises(ValidationError,match='rejected_candidate_requires_closed_reference_contract'):
                queue.record(DiscoveryResult(pending_candidates=[value]))
            assert queue.counts()=={} and not store.heads()


def test_completed_empty_retry_restores_rejection_but_running_retry_is_not_retired():
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d');closed=rejected_candidate()
        queue.record(DiscoveryResult(pending_candidates=[closed]))
        queue.decide(closed.candidate_id,'retry',1)
        queue.consume_retries('s',{'public.t'})
        assert CandidateQueue(store,ExtractionGoal(),['public'],'d').counts()=={'pending':1}
        queue.record(DiscoveryResult())
        assert queue.finish_retries('other',{'public.t'})==0
        assert queue.finish_retries('s',{'public.other'})==0
        assert queue.counts()=={'pending':1}
        assert queue.finish_retries('s',{'public.t'})==1
        assert queue.finish_retries('s',{'public.t'})==0
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        item=queue.listing()['candidates'][0]
        assert item['state']=='rejected'
        assert item['history'][0]['action']=='retry_completed_without_replacement'
        assert queue.context({('s','public','t')})==[] and not store.heads()


def test_retry_completion_does_not_hide_a_new_semantic_gap_or_proposed_replacement():
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d');closed=rejected_candidate()
        queue.record(DiscoveryResult(pending_candidates=[closed]))
        queue.decide(closed.candidate_id,'retry',1)
        queue.consume_retries('s',{'public.t'})
        revised=closed.model_copy(update={'status':'pending','failure_code':'semantic_claim_unresolved',
            'failure_details':[]})
        queue.record(DiscoveryResult(pending_candidates=[revised]))
        assert queue.finish_retries('s',{'public.t'})==0
        assert queue.listing()['candidates'][0]['state']=='pending'
        queue.record(DiscoveryResult(resolved_candidate_ids=[closed.candidate_id]))
        assert queue.finish_retries('s',{'public.t'})==0
        assert queue.listing()['candidates'][0]['state']=='revised'


def test_decisions_are_revision_checked_isolated_and_never_publish(tmp_path):
    path=tmp_path/'graph.sqlite'
    with GraphStore(path) as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        queue.record(DiscoveryResult(pending_candidates=[candidate()]))
        item=queue.listing()['candidates'][0]
        queue.decide(item['candidate_id'],'dismiss',item['revision'])
        with pytest.raises(LightGraphError,match='revision_conflict'):
            queue.decide(item['candidate_id'],'retry',item['revision'])
        assert not store.view({'public'},'d')['nodes']
        assert CandidateQueue(store,ExtractionGoal(),['private'],'d').listing()['total_candidates']==0
        assert CandidateQueue(store,ExtractionGoal(purpose='new'),['public'],'d').listing()['total_candidates']==0
    with GraphStore(path) as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        item=queue.listing()['candidates'][0]
        assert item['state']=='dismissed'
        queue.decide(item['candidate_id'],'retry',item['revision'])
        assert len(queue.retry_sources())==1
        queue.consume_retries('s',{'public.t'})
        assert queue.retry_sources()==[]
        queue.record(DiscoveryResult(resolved_candidate_ids=[item['candidate_id']]))
        assert queue.listing()['candidates'][0]['state']=='revised'
        assert not store.heads()


def test_retry_request_reopens_unresolved_table_without_requiring_global_rediscovery(monkeypatch,tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.sources import MemorySource
    from lightgraph.tests.test_agent_pipeline import client,input_packet,response,table,group
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    t=table('t',['code','name']);calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet['task'])
        return response(group(packet,'code','name') if len(calls)>1 else {'task':packet['task'],'abstain':True})
    with MemorySource([t],{'public.t':[('1','item')]}) as source:
        run_pipeline(config,[source],model=client(monkeypatch,handler))
    with GraphStore(tmp_path/'graph.sqlite') as store:
        queue=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources)
        queue.record(DiscoveryResult(pending_candidates=[candidate()]))
        queue.decide(candidate().candidate_id,'retry',1)
    with MemorySource([t],{'public.t':[('1','item')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==1
    assert calls==['T0_discovery','T0_discovery']


def test_replacement_is_resolved_only_for_its_published_revision():
    from lightgraph.tests.test_relative_hierarchy import rule, execute, path
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        replacement=rule();cid=candidate().candidate_id
        queue.record(DiscoveryResult(pending_candidates=[candidate()],resolved_candidate_ids=[cid],
            rules=[replacement],candidate_replacement_rules={cid:[replacement.rule_id]}))
        queue.confirm_publications([])
        assert queue.listing()['candidates'][0]['state']=='revised'
        assert execute(store,replacement,[path('A','B')]).published
        queue.confirm_publications([replacement.rule_id])
        assert queue.listing()['candidates'][0]['state']=='resolved'


def test_stored_new_revision_does_not_resolve_candidate_whose_head_is_old():
    from lightgraph.tests.test_relative_hierarchy import rule,execute,path
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        old=rule();cid=candidate().candidate_id
        assert execute(store,old,[path('A','B')]).published
        replacement=store.get_rules()[0].model_copy(update={'revision':2})
        store.put_rule(replacement)
        queue.record(DiscoveryResult(pending_candidates=[candidate()],resolved_candidate_ids=[cid],
            rules=[replacement],candidate_replacement_rules={cid:[replacement.rule_id]}))
        queue.confirm_publications([replacement.rule_id])
        assert queue.listing()['candidates'][0]['state']=='revised'
        assert store._publication(store.heads()[0]['publication_id'])['revision']==1
        assert execute(store,replacement,[path('A','B')]).published
        queue.confirm_publications([replacement.rule_id])
        assert queue.listing()['candidates'][0]['state']=='resolved'


def test_reobserved_resolved_failure_reopens_but_dismissed_choice_is_preserved():
    from lightgraph.tests.test_relative_hierarchy import rule,execute,path
    with GraphStore() as store:
        queue=CandidateQueue(store,ExtractionGoal(),['public'],'d')
        replacement=rule();cid=candidate().candidate_id
        queue.record(DiscoveryResult(pending_candidates=[candidate()],resolved_candidate_ids=[cid],
            rules=[replacement],candidate_replacement_rules={cid:[replacement.rule_id]}))
        assert execute(store,replacement,[path('A','B')]).published
        queue.confirm_publications([replacement.rule_id])
        queue.record(DiscoveryResult(pending_candidates=[candidate()]))
        item=queue.listing()['candidates'][0]
        assert item['state']=='pending'
        # A newly compiled result with no surviving replacement rules cannot
        # reuse the previous generation's now-obsolete replacement mapping.
        queue.record(DiscoveryResult(resolved_candidate_ids=[cid]))
        queue.confirm_publications([replacement.rule_id])
        assert queue.listing()['candidates'][0]['state']=='revised'
        queue.record(DiscoveryResult(pending_candidates=[candidate().model_copy(update={
            'failure_code':'semantic_claim_refuted'})]))
        item=queue.listing()['candidates'][0]
        queue.decide(cid,'dismiss',item['revision'])
        queue.record(DiscoveryResult(pending_candidates=[candidate().model_copy(update={
            'failure_code':'semantic_claim_refuted'})]))
        assert queue.listing()['candidates'][0]['state']=='dismissed'

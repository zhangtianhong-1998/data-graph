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

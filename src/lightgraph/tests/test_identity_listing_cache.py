"""Per-call listing reuse must stop at every internal or external store change."""
import pytest

from lightgraph.contracts import ExtractionGoal, GraphFact, PartitionCompletion, SupportSpec
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import LightGraphError
from lightgraph.identity import reconciliation
from lightgraph.store import GraphStore
from lightgraph.tests.test_identity_cluster_review import OfflineModel


def seed_many(store, number=10):
    publication=store.begin_publication('occurrences',1,'all',{})
    support=SupportSpec(origin='user_input',source_binding='occurrences',scope_ref='public',
        partition_id='all',rule_id='occurrences',rule_revision=1)
    for index in range(number):
        entity=f'node_{index:02}'
        store.stage_fact(publication,GraphFact(fact_kind='NodeExistence',payload={
            'entity_id':entity,'kind':'ReportItem','layer':'business'}),support)
        for name,value in [('label','Shared label'),('definition','Shared definition')]:
            store.stage_fact(publication,GraphFact(fact_kind='PropertyValue',payload={
                'entity_id':entity,'name':name,'value':value}),support)
    store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,
        consistent=True,dependencies_valid=True))
    return reconciliation.record_candidates(store,store.view({'public'},apply_resolutions=False),{'public'})


def test_over_review_cap_candidates_do_not_repeat_full_listing(monkeypatch):
    calls=[]
    original=reconciliation.list_candidates
    def counted(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(reconciliation,'list_candidates',counted)
    def review(*args,**kwargs):raise LightGraphError('semantic_claim_refuted')
    monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics',review)
    with GraphStore() as store:
        recorded=seed_many(store)
        counts,issues=reconciliation.review_with_agent(store,OfflineModel(),ExtractionGoal(),
            DocumentCorpus([]),scopes={'public'},max_reviews=2)
        assert recorded['candidate_pairs']>20
        assert counts['reviewed']==counts['rejected']==2
        assert counts['deferred']==recorded['candidate_pairs']-2
        assert len(issues)==2
        # Initial listing and one refresh per review-attempt write, no refresh
        # for the dozens of unchanged candidates after the review allowance.
        assert len(calls)==3
        calls.clear()
        reconciliation.review_with_agent(store,OfflineModel(),ExtractionGoal(),
            DocumentCorpus([]),scopes={'public'},max_reviews=1)
        assert len(calls)==2  # A new invocation owns a new cache.


@pytest.mark.parametrize('connection_kind',['same','external'])
def test_user_decision_written_during_listing_invalidates_cache(monkeypatch,tmp_path,connection_kind):
    database=tmp_path/'graph.sqlite'
    with GraphStore(database) as store:
        seed_many(store,4)
        other=GraphStore(database) if connection_kind=='external' else store
        original=reconciliation.list_candidates;list_calls=[];reviewed=[];human_members=[]
        local_changes=[]
        def listing(*args,**kwargs):
            value=original(*args,**kwargs);list_calls.append(1)
            if len(list_calls)==1:
                candidate=value['candidates'][0];human_members.extend(candidate['members'])
                local_changes.append(store.connection.total_changes)
                reconciliation.decide(other,candidate['candidate_id'],'different',scopes={'public'},
                    expected_revision=value['revision'],origin='user')
                local_changes.append(store.connection.total_changes)
            return value
        def review(model,packet,*args,**kwargs):
            reviewed.append(packet['authorized_entities'])
            raise LightGraphError('semantic_claim_refuted')
        monkeypatch.setattr(reconciliation,'list_candidates',listing)
        monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics',review)
        try:
            counts,_=reconciliation.review_with_agent(store,OfflineModel(),ExtractionGoal(),
                DocumentCorpus([]),scopes={'public'},max_reviews=1)
            assert counts['reviewed']==1 and reviewed[0]!=human_members
            assert len(list_calls)==3
            if connection_kind=='external':assert local_changes[0]==local_changes[1]
            else:assert local_changes[0]<local_changes[1]
            decisions=original(store,{'public'})['decisions']
            assert len(decisions)==1 and decisions[0]['origin']=='user' and decisions[0]['status']=='active'
        finally:
            if other is not store:other.close()


def test_merge_refresh_expands_next_review_to_whole_cluster(monkeypatch):
    reviewed=[];list_calls=[]
    original=reconciliation.list_candidates
    def listing(*args,**kwargs):
        list_calls.append(1);return original(*args,**kwargs)
    def review(model,packet,*args,**kwargs):
        reviewed.append(packet['authorized_entities'])
        return [fact['reference'] for fact in packet['graph_facts']
                if fact['payload'].get('name')=='definition']
    monkeypatch.setattr(reconciliation,'list_candidates',listing)
    monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics',review)
    with GraphStore() as store:
        seed_many(store,3)
        counts,issues=reconciliation.review_with_agent(store,OfflineModel(),ExtractionGoal(),
            DocumentCorpus([]),scopes={'public'},max_reviews=2)
        assert counts['accepted']==2 and not issues
        assert len(reviewed[0])==2 and reviewed[1]==['node_00','node_01','node_02']
        assert len(list_calls)==3 and len(store.view({'public'})['nodes'])==1

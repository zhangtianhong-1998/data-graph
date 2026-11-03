"""Core hierarchy coverage precedes optional relation/identity exploration."""
from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import BudgetExceeded
from lightgraph.identity.reconciliation import review_with_agent
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_identity_cluster_review import OfflineModel
from lightgraph.tests.test_identity_listing_cache import seed_many


def hierarchy(packet, aliases=False):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    return {'kind':'wide_hierarchy','claim':{'group_id':'tree','entity_kind_hint':'ReportItem',
        'levels':[{'position':i,'key':ids[f'c{i}'],'label':ids[f'n{i}'],
                   **({'alias':ids[f'e{i}']} if aliases else {})} for i in (1,2)],
        'path_semantics':'relative_path','identity_domain':'items','taxonomy':'path',
        'position_semantics':'ordinal_slots','ordering_evidence':[ids['c1']]},
        'evidence_cited':[ids['c1']]}


def supported(packet):
    return response({'proposition_ref':packet['semantic_proposition_ref'], 'decision':'supported',
        'basis':'data_inference','evidence':[{'reference':packet['metadata_digest']['columns'][0]['id'],
                                            'stance':'supports'}]})






def test_expired_identity_budget_returns_one_issue_and_no_fake_reviews(monkeypatch):
    model=OfflineModel();model.budget=BudgetAllocator();model.budget.tighten_deadline(0)
    with GraphStore() as store:
        candidates=seed_many(store,6)
        counts,issues=review_with_agent(store,model,ExtractionGoal(),DocumentCorpus([]),scopes={'public'})
        assert counts['reviewed']==0 and counts['deferred']==candidates['candidate_pairs']
        assert issues==[{'code':'wall_time_budget_exhausted','count':1}]
        assert len(store.view({'public'})['nodes'])==6


def test_identity_budget_expiring_during_review_stops_subsequent_calls(monkeypatch):
    calls=[]
    def review(*args,**kwargs):calls.append(1);raise BudgetExceeded('llm_tokens_budget_exhausted')
    monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics',review)
    with GraphStore() as store:
        candidates=seed_many(store,6)
        counts,issues=review_with_agent(store,OfflineModel(),ExtractionGoal(),DocumentCorpus([]),scopes={'public'})
        assert counts['reviewed']==1 and len(calls)==1
        assert counts['deferred']==candidates['candidate_pairs'] and len(issues)==1

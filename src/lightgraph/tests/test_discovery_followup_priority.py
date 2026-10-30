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


def test_single_hierarchy_does_not_start_optional_cross_rule_discovery(monkeypatch,tmp_path):
    source_table=table('paths',['c1','n1','c2','n2']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':return supported(packet)
        assert packet['task']=='T0_discovery'
        return response({'task':packet['task'],'proposals':[hierarchy(packet)]})
    with MemorySource([source_table],{'public.paths':[('A','Parent','B','Child')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
                            [source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==2 and report.counts['business_edges']==1
    assert len(report.rules_published)==1
    assert tasks==['T0_discovery','semantic_review']
    assert not report.counts.get('relation_discovery_tasks')
    assert not any(i['code']=='relation_discovery_pending' for i in report.issues)


def test_hierarchy_alias_coverage_keeps_complete_mapping_and_precedes_relations(monkeypatch,tmp_path):
    source_table=table('paths',['c1','n1','e1','c2','n2','e2','other','other_name']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':return supported(packet)
        if packet['task']=='T0_discovery':
            batch=group(packet,'other','other_name','Metric');batch['proposals'].append(hierarchy(packet))
            return response(batch)
        if packet['task']=='T1_table_discovery':
            assert set(packet['iteration_context']['coverage_columns'])=={'e1','e2'}
            known=next(r for r in packet['iteration_context']['known_rules'] if r['kind']=='wide_hierarchy')
            assert [l['code'] for l in known['hierarchy_levels']]==['c1','c2']
            assert known['namespace_columns']==[]
            assert known['identity_contract']['identity_domain']=='items'
            assert known['hierarchy_contract']['taxonomy']=='path'
            assert '完整提案并补映射' in packet['subtask']
            return response({'task':packet['task'],'proposals':[hierarchy(packet,aliases=True)]})
        assert packet['task']=='T2_cross_table'
        return response({'task':packet['task'],'abstain':True})
    with MemorySource([source_table],{'public.paths':[('A','Parent','PARENT','B','Child','CHILD','M','Metric')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
                            [source],model=client(monkeypatch,handler))
    assert tasks.index('T1_table_discovery')<tasks.index('T2_cross_table')
    assert report.counts['coverage_attribute_revisions']==1
    assert len(report.rules_published)==2
    assert not any(i['code']=='unexplained_columns' for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        nodes=[n for n in store.view({'public'})['nodes'] if n['kind']=='ReportItem']
        assert {a for n in nodes for a in n['aliases']}=={'PARENT','CHILD'}
        assert len({r.rule_id for r in store.get_rules()})==2  # Old revision is retained for history.


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

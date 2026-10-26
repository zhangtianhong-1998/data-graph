"""Public graph state, authorized provenance and reversible identity review."""
import pytest
from fastapi.testclient import TestClient

from lightgraph.contracts import GraphFact, PartitionCompletion, RuleSpec, SupportSpec
from lightgraph.identity.reconciliation import record_candidates
from lightgraph.store import GraphStore
from lightgraph.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'workspace',frontend_dir=tmp_path/'missing')) as client:
        assert client.post('/api/projects',json={'name':'graph review','project_id':'p'}).status_code==201
        yield client


def revision(client):
    return {'If-Match':client.get('/api/projects/p/config').headers['etag']}


def seed(client):
    config=client.app.state.workspace.resolved_config('p')
    with GraphStore(config.output_dir+'/graph.sqlite') as store:
        for name,scope,status,entities in [('public','public','accepted',[('a','Shared'),('b','Shared')]),
                                          ('candidate','public','candidate',[('c','Maybe')]),
                                          ('hidden','private','accepted',[('x','PRIVATE'),('y','PRIVATE')])]:
            rule=RuleSpec(rule_id=name,kind='column_group_entity',source={'source_id':'input','table':'people'},
                identity={'key_columns':['code'],'entity_kind':'ReportItem'},attributes={'single_valued':{'label':'name'}},
                evidence={'semantic_basis':['fixture:approved']})
            store.put_rule(rule)
            pub=store.begin_publication(name,1,'all',{})
            support=SupportSpec(origin='user_input',source_binding=name,scope_ref=scope,partition_id='all',rule_id=name,rule_revision=1,status=status)
            for entity,label in entities:
                for kind,payload in [('NodeExistence',{'entity_id':entity,'kind':'ReportItem','layer':'business'}),
                                     ('PropertyValue',{'entity_id':entity,'name':'label','value':label})]:
                    store.stage_fact(pub,GraphFact(isolation_domain='p',fact_kind=kind,payload=payload),support)
            store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
        record_candidates(store,store.view({'public'},domain='p',include_candidates=True),{'public'},'p')
        record_candidates(store,store.view({'public','private'},domain='p',include_candidates=True),{'public','private'},'p')


def test_new_workbench_project_defaults_to_goal_driven_vocabulary(client):
    config=client.get('/api/projects/p/config').json()
    assert config['goal']['entity_kinds']==config['goal']['relation_kinds']==[]


def test_default_graph_excludes_candidates_and_details_authorize_each_support(client):
    seed(client)
    normal=client.get('/api/projects/p/graph').json()
    assert {n['id'] for n in normal['nodes']}=={'a','b'}
    assert all(n['status']=='accepted' for n in normal['nodes'])
    preview=client.get('/api/projects/p/graph?include_candidates=true').json()
    assert {n['id'] for n in preview['nodes']}=={'a','b','c'}
    assert next(n for n in preview['nodes'] if n['id']=='c')['status']=='candidate'
    details=client.get('/api/projects/p/graph/details?entity_id=a').json()
    assert len(details['facts'])==2
    assert details['supports'] and all(s['scope_ref']=='public' for s in details['supports'])
    assert details['rules'][0]['identity_columns']==['code']
    assert details['rules'][0]['attribute_columns']['single_valued']=={'label':'name'}
    assert client.get('/api/projects/p/graph/details?entity_id=c').status_code==404
    candidate_details=client.get('/api/projects/p/graph/details?entity_id=c&include_candidates=true')
    assert candidate_details.status_code==200
    assert candidate_details.json()['rules'][0]['rule_id']=='candidate'
    assert client.get('/api/projects/p/graph/details?entity_id=x&include_candidates=true').status_code==404
    assert 'PRIVATE' not in str([normal,preview,details])
    assert client.get('/api/projects/p/graph/details').status_code==400
    assert client.get('/api/projects/p/graph/details?entity_id=a&offset=-1').status_code==400


def test_identity_review_cas_mutability_and_revoke_preserve_original_evidence(client,monkeypatch):
    seed(client)
    listing=client.get('/api/projects/p/graph/identity-candidates').json()
    assert listing['revision']==0 and len(listing['candidates'])==1
    candidate=listing['candidates'][0]
    assert set(candidate['members'])=={'a','b'}
    assert len(candidate['nodes'])==2 and candidate['facts'] and candidate['supports']
    assert 'PRIVATE' not in str(listing)
    body={'candidate_id':candidate['candidate_id'],'verdict':'same','revision':0}
    url='/api/projects/p/graph/identity-decisions'
    assert client.post(url,json=body).status_code==428
    monkeypatch.setattr(client.app.state.manager,'active',lambda _:True)
    assert client.post(url,json=body,headers=revision(client)).status_code==409
    monkeypatch.setattr(client.app.state.manager,'active',lambda _:False)
    response=client.post(url,json=body,headers=revision(client))
    assert response.status_code==200,response.text
    decision=response.json()
    assert len(client.get('/api/projects/p/graph').json()['nodes'])==1
    again=client.get('/api/projects/p/graph/identity-candidates').json()
    assert len(again['candidates'][0]['nodes'])==2  # Review always uses originals.
    assert again['candidates'][0]['has_active_decision'] is True
    assert again['decisions'][0]['status']=='active'
    stale=client.post(url,json=body,headers=revision(client))
    assert stale.status_code==409 and stale.json()['error']['code']=='resolution_revision_conflict'
    revoke=f"{url}/{decision['decision_id']}/revoke"
    response=client.post(revoke,json={'revision':1},headers=revision(client))
    assert response.status_code==200,response.text
    assert len(client.get('/api/projects/p/graph').json()['nodes'])==2
    assert client.get('/api/projects/p/graph/identity-candidates').json()['decisions'][0]['status']=='revoked'

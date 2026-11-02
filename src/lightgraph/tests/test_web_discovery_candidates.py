from lightgraph.tests.test_web_graph_review import client, revision
from lightgraph.tests.test_discovery_candidate_queue import candidate, rejected_candidate
from lightgraph.discovery.models import DiscoveryResult
from lightgraph.runtime.candidates import CandidateQueue
from lightgraph.store import GraphStore


def test_candidate_actions_require_revision_and_never_publish(client,monkeypatch):
    config=client.app.state.workspace.resolved_config('p')
    with GraphStore(config.output_dir+'/graph.sqlite') as store:
        queue=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources)
        queue.record(DiscoveryResult(pending_candidates=[candidate()]))
    listing=client.get('/api/projects/p/discovery/candidates').json()
    assert listing['total_candidates']==1
    body={'candidate_id':candidate().candidate_id,'action':'retry','revision':1}
    assert client.post('/api/projects/p/discovery/decisions',json=body).status_code==428
    result=client.post('/api/projects/p/discovery/decisions',json=body,headers=revision(client))
    assert result.status_code==200 and result.json()['published'] is False
    assert client.post('/api/projects/p/discovery/decisions',json=body,headers=revision(client)).status_code==409
    assert not client.get('/api/projects/p/graph').json()['nodes']
    monkeypatch.setattr(client.app.state.manager,'active',lambda pid:True)
    body['revision']=2
    assert client.post('/api/projects/p/discovery/decisions',json=body,headers=revision(client)).status_code==409


def test_rejected_candidate_is_terminal_but_explicit_investigation_is_not_publication(client):
    config=client.app.state.workspace.resolved_config('p')
    rejected=rejected_candidate()
    with GraphStore(config.output_dir+'/graph.sqlite') as store:
        queue=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources)
        queue.record(DiscoveryResult(pending_candidates=[rejected]))
    listing=client.get('/api/projects/p/discovery/candidates').json()
    current=listing['candidates'][0]
    assert current['state']=='rejected'
    assert current['failure_details']==['relation_endpoint_kind_mismatch']
    for action in ('reopen','dismiss'):
        response=client.post('/api/projects/p/discovery/decisions',json={
            'candidate_id':rejected.candidate_id,'revision':current['revision'],'action':action},
            headers=revision(client))
        assert response.status_code==400
        assert response.json()['error']['code']=='discovery_candidate_closed'
    response=client.post('/api/projects/p/discovery/decisions',json={
        'candidate_id':rejected.candidate_id,'revision':current['revision'],'action':'retry'},
        headers=revision(client))
    assert response.status_code==200
    assert response.json()['state']=='retry_requested' and response.json()['published'] is False
    assert not client.get('/api/projects/p/graph').json()['nodes']

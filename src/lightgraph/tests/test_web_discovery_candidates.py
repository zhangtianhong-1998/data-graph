from lightgraph.tests.test_web_graph_review import client, revision
from lightgraph.tests.test_discovery_candidate_queue import candidate
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

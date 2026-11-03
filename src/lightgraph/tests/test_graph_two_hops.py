"""Two-hop views traverse authorized publications, not just the first UI page."""
import pytest
from fastapi.testclient import TestClient

from lightgraph.contracts import GraphFact, PartitionCompletion, RuleSpec, SupportSpec
from lightgraph.errors import LightGraphError
from lightgraph.identity.reconciliation import record_candidates
from lightgraph.store import GraphStore
from lightgraph.store.paging import graph_page
from lightgraph.web.app import create_app
from lightgraph.web.execution import _metadata_neighborhood


def publish(store, name, nodes, edges, *, scope='public', status='accepted', domain='d'):
    store.put_rule(RuleSpec(rule_id=name, kind='column_group_entity',
        source={'source_id':'s','table':'items'},
        identity={'key_columns':['code'],'entity_kind':'ReportItem'}))
    publication=store.begin_publication(name,1,'all',{})
    support=SupportSpec(origin='user_input',source_binding=name,scope_ref=scope,
        partition_id='all',rule_id=name,rule_revision=1,status=status)
    for nid in nodes:
        store.stage_fact(publication,GraphFact(isolation_domain=domain,fact_kind='NodeExistence',
            payload={'entity_id':nid,'kind':'ReportItem','layer':'business'}),support)
    for src,dst in edges:
        store.stage_fact(publication,GraphFact(isolation_domain=domain,fact_kind='Edge',
            payload={'source':src,'target':dst,'relation':'parent_of','qualifiers':{'allow_dag':True}}),support)
    store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,
        consistent=True,dependencies_valid=True))


def sample(store, domain='d'):
    publish(store,'visible',list('abcdef'),[('a','b'),('b','c'),('c','d'),('e','a'),('f','e')],domain=domain)
    # Traversal may not use a private bridge to reach another visible node.
    publish(store,'private',['hidden'],[('a','hidden'),('hidden','d')],scope='private',domain=domain)
    publish(store,'tentative',['candidate'],[('a','candidate'),('candidate','d')],status='candidate',domain=domain)


def ids(graph):
    return {node['id'] for node in graph['nodes']}


def test_two_hops_bidirectional_induced_subgraph_filters_before_traversal(monkeypatch):
    with GraphStore() as store:
        sample(store)
        monkeypatch.setattr(store,'view',lambda *a,**k: pytest.fail('full graph aggregation'))
        one=graph_page(store,{'public'},'d',focus='a')
        two=graph_page(store,{'public'},'d',focus='a',hops=2)
        assert ids(one)=={'a','b','e'}
        assert ids(two)=={'a','b','c','e','f'}
        assert {(e['source'],e['target']) for e in two['edges']}=={('a','b'),('b','c'),('e','a'),('f','e')}
        assert two['focus_hops']==2 and two['focus_neighbor_count']==4
        assert two['boundary_edges']==1  # The next edge to d is outside two hops.
        assert two['matched_nodes']==4 and not two['truncated']
        assert all(s['scope_ref']=='public' for s in two['supports'])
        assert 'hidden' not in str(two) and 'candidate' not in str(two)
        assert not ids(graph_page(store,{'public'},'d',focus='hidden',hops=2))
        assert not ids(graph_page(store,{'public'},'other',focus='a',hops=2))
        preview=graph_page(store,{'public'},'d',focus='a',hops=2,include_candidates=True)
        assert ids(preview)=={'a','b','c','d','e','f','candidate'}


def test_two_hop_pages_preserve_bridge_edges_and_cap_is_explicit():
    with GraphStore() as store:
        sample(store)
        first=graph_page(store,{'public'},'d',focus='a',hops=2,limit=2)
        second=graph_page(store,{'public'},'d',focus='a',hops=2,limit=2,offset=first['next_offset'])
        assert ids(first)=={'a','b','e'}
        assert ids(second)=={'a','b','c','e','f'}
        assert len(second['edges'])==4 and second['next_offset'] is None
        publish(store,'large',[f'n{i:04}' for i in range(1001)], [('c',f'n{i:04}') for i in range(1001)])
        bounded=graph_page(store,{'public'},'d',focus='b',hops=2,limit=1000)
        assert len(bounded['nodes'])==1000
        assert bounded['focus_neighbor_count']==1005
        assert bounded['neighborhood_truncated'] and bounded['truncated']
        assert bounded['next_offset'] is None
        assert {'a','b','c','d','e'} <= ids(bounded)


@pytest.mark.parametrize('hops',[0,3,-1,True,'2'])
def test_hop_depth_is_bounded(hops):
    with GraphStore() as store, pytest.raises(LightGraphError,match='graph_page_invalid'):
        graph_page(store,{'public'},'d',focus='a',hops=hops)


def test_metadata_traversal_happens_before_response_limit():
    view={'nodes':[{'id':f'n{i}','kind':'Column','layer':'metadata'} for i in range(1005)],
        'edges':[{'source':'n0','target':'n1002'},{'source':'n1002','target':'n1003'},
                 {'source':'n1003','target':'n1004'}]}
    focused=_metadata_neighborhood(view,'n0',2)
    assert ids(focused)=={'n0','n1002','n1003'}
    assert len(focused['edges'])==2 and focused['focus_neighbor_count']==2
    assert focused['total_nodes']==1005 and not focused['truncated']


def test_web_passes_hop_depth_and_preserves_scope(tmp_path):
    with TestClient(create_app(tmp_path/'workspace',frontend_dir=tmp_path/'missing')) as client:
        assert client.post('/api/projects',json={'name':'two hops','project_id':'p'}).status_code==201
        config=client.app.state.workspace.resolved_config('p')
        with GraphStore(config.output_dir+'/graph.sqlite') as store:
            sample(store,'p')
        graph=client.get('/api/projects/p/graph?focus=a&hops=2').json()
        assert ids(graph)=={'a','b','c','e','f'}
        assert graph['focus_hops']==2 and graph['focus_neighbor_count']==4
        assert not graph['truncated'] and graph['include_candidates'] is False
        assert client.get('/api/projects/p/graph?focus=a&hops=3').status_code==400
        assert not client.get('/api/projects/p/graph?focus=hidden&hops=2').json()['nodes']


def test_two_hops_follow_reviewed_identity_and_stop_at_two_edges(tmp_path):
    with TestClient(create_app(tmp_path/'workspace',frontend_dir=tmp_path/'missing')) as client:
        client.post('/api/projects',json={'name':'reviewed graph','project_id':'p'})
        config=client.app.state.workspace.resolved_config('p')
        with GraphStore(config.output_dir+'/graph.sqlite') as store:
            publish(store,'visible',['a','alias','b','c','d'],[('alias','b'),('b','c'),('c','d')],domain='p')
            publication=store.begin_publication('visible',1,'names',{})
            support=SupportSpec(origin='user_input',source_binding='visible',scope_ref='public',
                partition_id='names',rule_id='visible',rule_revision=1)
            for nid in ('a','alias'):
                store.stage_fact(publication,GraphFact(isolation_domain='p',fact_kind='PropertyValue',
                    payload={'entity_id':nid,'name':'label','value':'同一项目'}),support)
            store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,
                consistent=True,dependencies_valid=True))
            record_candidates(store,store.view({'public'},domain='p'),{'public'},'p')
        candidates=client.get('/api/projects/p/graph/identity-candidates').json()
        candidate=next(c for c in candidates['candidates'] if set(c['members'])=={'a','alias'})
        response=client.post('/api/projects/p/graph/identity-decisions',
            json={'candidate_id':candidate['candidate_id'],'verdict':'same','revision':0},
            headers={'If-Match':client.get('/api/projects/p/config').headers['etag']})
        assert response.status_code==200,response.text
        graph=client.get('/api/projects/p/graph').json()
        center=next(n['id'] for n in graph['nodes'] if n.get('label')=='同一项目')
        two=client.get('/api/projects/p/graph',params={'focus':center,'hops':2}).json()
        assert ids(two)=={center,'b','c'} and len(two['edges'])==2
        assert two['query_strategy']=='validated_identity_projection'
        assert two['focus_neighbor_count']==2

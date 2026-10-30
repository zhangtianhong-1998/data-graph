from lightgraph.contracts import GraphFact, PartitionCompletion, RuleSpec, SupportSpec
from lightgraph.store import GraphStore
from lightgraph.store.paging import graph_page


def test_page_and_search_beyond_old_cap_only_aggregate_authorized_current_rows(monkeypatch):
    with GraphStore() as store:
        for name,scope,count in [('shown','public',1005),('hidden','private',2)]:
            store.put_rule(RuleSpec(rule_id=name,kind='column_group_entity',source={'source_id':'s','table':'items'},
                identity={'key_columns':['code'],'entity_kind':'ReportItem'}))
            pub=store.begin_publication(name,1,'all',{})
            support=SupportSpec(origin='user_input',source_binding=name,scope_ref=scope,
                partition_id='all',rule_id=name,rule_revision=1)
            for i in range(count):
                eid=f'{name}_{i:04}'
                for kind,payload in [('NodeExistence',{'entity_id':eid,'kind':'ReportItem','layer':'business'}),
                    ('PropertyValue',{'entity_id':eid,'name':'label','value':f'item {i}'})]:
                    store.stage_fact(pub,GraphFact(isolation_domain='d',fact_kind=kind,payload=payload),support)
            store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
        monkeypatch.setattr(store,'view',lambda *a,**k: (_ for _ in ()).throw(AssertionError('full view')))
        first=graph_page(store,{'public'},'d',limit=240)
        last=graph_page(store,{'public'},'d',offset=960,limit=240)
        found=graph_page(store,{'public'},'d',q='item 1004')
        assert first['total_nodes']==last['total_nodes']==1005
        assert len(first['nodes'])==240 and len(last['nodes'])==45
        assert first['next_offset']==240 and last['next_offset'] is None
        assert {n['id'] for n in found['nodes']}=={'shown_1004'} and found['matched_nodes']==1
        assert all(s['scope_ref']=='public' for s in last['supports'])
        assert len(first['facts'])==480 and 'hidden' not in str(first)

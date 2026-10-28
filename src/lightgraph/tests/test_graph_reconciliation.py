"""Review prospective graphs and resolve identity without destroying evidence."""
import pytest

from lightgraph.contracts import GraphFact,PartitionCompletion,SupportSpec
from lightgraph.errors import LightGraphError
from lightgraph.identity.reconciliation import (GenerationView,find_candidates,record_candidates,
    list_candidates,decide,revoke)
from lightgraph.store import GraphStore


def publish(store, rule, entities, edges=(), scope='public', *, deferred=False):
    pub=store.begin_publication(rule,1,'all',{},replace_rule=True)
    support=SupportSpec(origin='user_input',source_binding=rule,scope_ref=scope,
        partition_id='all',rule_id=rule,rule_revision=1)
    for entity,label in entities:
        store.stage_fact(pub,GraphFact(fact_kind='NodeExistence',payload={'entity_id':entity,'kind':'ReportItem','layer':'business'}),support)
        store.stage_fact(pub,GraphFact(fact_kind='PropertyValue',payload={'entity_id':entity,'name':'label','value':label}),support)
    for source,relation,target in edges:
        qualifiers={'allow_dag':True} if relation=='parent_of' else {}
        store.stage_fact(pub,GraphFact(fact_kind='Edge',payload={'source':source,'relation':relation,'target':target,'qualifiers':qualifiers}),support)
    if not deferred:store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
    return pub


def candidates(store, scopes={'public'}):
    graph=store.view(scopes,include_candidates=True,apply_resolutions=False)
    record_candidates(store,graph,scopes)
    return list_candidates(store,scopes)


def candidate_for(items, *members):
    return next(c['candidate_id'] for c in items['candidates'] if set(c['members'])==set(members))


def test_prospective_view_replaces_old_unit_without_early_publication():
    with GraphStore() as store:
        publish(store,'r',[('old','Old')])
        replacement=publish(store,'r',[('new1','Same'),('new2','Same')],deferred=True)
        frozen=GenerationView.capture(store,[replacement],{'public'})
        assert {n['id'] for n in frozen.graph['nodes']}=={'new1','new2'}
        assert len(find_candidates(frozen.graph)[0])==1
        assert [n['id'] for n in store.view({'public'})['nodes']]==['old']
        publish(store,'other',[('other','Other')])
        with pytest.raises(LightGraphError,match='graph_generation_changed'):
            store.publish_partitions([PartitionCompletion(publication_id=replacement,complete=True,
                consistent=True,dependencies_valid=True)],expected_heads=frozen.heads)


def test_merge_preserves_parallel_relations_supports_history_and_revoke(tmp_path):
    from lightgraph.presentation import export_graph
    from lightgraph.presentation.importer import import_graph
    with GraphStore() as store:
        from lightgraph.contracts import ExtractionGoal
        from lightgraph.semantic_schema import SemanticSchema,RelationDefinition
        store.configure_goal(ExtractionGoal(entity_kinds=[],relation_kinds=[],semantic_schema=SemanticSchema(
            relations=[RelationDefinition(name='collaborates_with',description='Works with another object',
                source_kinds=['ReportItem'],target_kinds=['ReportItem'])])),1)
        old=publish(store,'r',[('a','Shared'),('b','Shared'),('c','Other')],
                    [('a','parent_of','c'),('b','parent_of','c'),('a','collaborates_with','c')])
        listing=candidates(store)
        decision=decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        graph=store.view({'public'})
        assert {n['id'] for n in graph['nodes']}=={'a','c'}
        parent=[e for e in graph['edges'] if e['relation']=='parent_of']
        assert len(parent)==1 and len(parent[0]['support_ids'])==2
        assert any(e['relation']=='collaborates_with' for e in graph['edges'])
        assert len(store.view({'public'},publication_id=old)['nodes'])==3
        export_graph(store,tmp_path/'export',scopes={'public'})
        restored=import_graph(tmp_path/'export/graph.json',tmp_path/'import.sqlite')
        assert restored['nodes']==2
        revoke(store,decision['decision_id'],scopes={'public'},expected_revision=1)
        assert len(store.view({'public'})['nodes'])==3
        assert len([e for e in store.view({'public'})['edges'] if e['relation']=='parent_of'])==2


def test_scope_hidden_decision_cannot_change_public_identity_projection():
    with GraphStore() as store:
        publish(store,'r',[('a','Shared'),('b','Shared')])
        publish(store,'private',[('a','Private alias')],scope='private')
        listing=candidates(store,{'public','private'})
        decide(store,candidate_for(listing,'a','b'),'same',scopes={'public','private'},expected_revision=0)
        assert len(store.view({'public','private'})['nodes'])==1
        public=store.view({'public'})
        assert len(public['nodes'])==2 and 'Private alias' not in str(public)
        assert list_candidates(store,{'public'})['decisions']==[]


def test_cluster_review_blocks_transitive_merge_and_explicit_cannot_link():
    with GraphStore() as store:
        publish(store,'r',[('a','Shared'),('b','Shared'),('c','Shared')])
        listing=candidates(store)
        decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        with pytest.raises(LightGraphError,match='resolution_cluster_review_required'):
            decide(store,candidate_for(listing,'b','c'),'same',scopes={'public'},expected_revision=1)
        decide(store,candidate_for(listing,'a','c'),'different',scopes={'public'},expected_revision=1)
        with pytest.raises(LightGraphError,match='resolution_cannot_link_conflict'):
            decide(store,candidate_for(listing,'b','c'),'same',scopes={'public'},expected_revision=2,members=['a','b','c'])


def test_upstream_withdrawal_deactivates_dependent_identity_without_rewriting_history():
    with GraphStore() as store:
        original=publish(store,'r',[('a','Shared'),('b','Shared')])
        listing=candidates(store)
        decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        assert len(store.view({'public'})['nodes'])==1
        publish(store,'r',[('a','Shared'),('b','Different')])
        assert len(store.view({'public'})['nodes'])==2
        assert list_candidates(store,{'public'})['decisions'][0]['status']=='needs_review'
        assert len(store.view({'public'},publication_id=original)['nodes'])==2


def test_common_names_are_bounded_before_pair_generation():
    graph={'nodes':[{'id':str(i),'layer':'business'} for i in range(1000)],
        'facts':[{'id':'fact'+str(i),'fact_kind':'PropertyValue','payload':
                  {'entity_id':str(i),'name':'label','value':'common'}} for i in range(1000)]}
    pairs,coverage=find_candidates(graph,per_entity=2,bucket_limit=4,max_candidates=20,max_entities=100)
    assert len(pairs)<=20
    assert coverage['entities_checked']==100 and coverage['entity_limit_omissions']==900
    assert coverage['high_frequency_omitted_entities']>0 and coverage['candidate_limit_omissions']>0


@pytest.mark.parametrize('new_edges',[
    [('a','parent_of','b')],
    [('a','parent_of','c'),('c','parent_of','b')],
])
def test_new_incident_relation_requires_review_before_old_merge_can_apply(new_edges):
    with GraphStore() as store:
        publish(store,'r',[('a','Shared'),('b','Shared'),('c','Other')])
        listing=candidates(store)
        decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        assert len(store.view({'public'})['nodes'])==2
        publish(store,'r',[('a','Shared'),('b','Shared'),('c','Other')],new_edges)
        current=store.view({'public'})
        assert len(current['nodes'])==3
        assert not any(e['source']==e['target'] for e in current['edges'])
        assert list_candidates(store,{'public'})['decisions'][0]['status']=='needs_review'


def test_merge_cannot_close_a_long_hierarchy_cycle():
    with GraphStore() as store:
        publish(store,'r',[('a','Shared'),('b','Shared'),('c','Other')],
                [('a','parent_of','c'),('c','parent_of','b')])
        listing=candidates(store)
        with pytest.raises(LightGraphError,match='hierarchy_cycle'):
            decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        assert len(store.view({'public'})['nodes'])==3


def test_new_nonincident_path_cannot_create_a_projected_cycle():
    with GraphStore() as store:
        publish(store,'nodes',[('a','Shared'),('b','Shared'),('c','C'),('d','D')])
        publish(store,'edges',[],[('a','parent_of','c'),('d','parent_of','b')])
        listing=candidates(store)
        decide(store,candidate_for(listing,'a','b'),'same',scopes={'public'},expected_revision=0)
        assert len(store.view({'public'})['nodes'])==3
        publish(store,'new_edges',[],[('c','parent_of','d')])
        assert len(store.view({'public'})['nodes'])==4
        assert list_candidates(store,{'public'})['decisions'][0]['status']=='needs_review'


def test_visible_scope_union_preserves_cannot_link_conflicts():
    with GraphStore() as store:
        publish(store,'scope_A',[('a','Shared'),('b','Shared')],scope='A')
        publish(store,'scope_B',[('a','Shared'),('b','Shared')],scope='B')
        a=candidates(store,{'A'});b=candidates(store,{'B'})
        decide(store,candidate_for(a,'a','b'),'same',scopes={'A'},expected_revision=0)
        decide(store,candidate_for(b,'a','b'),'different',scopes={'B'},expected_revision=1)
        assert len(store.view({'A'})['nodes'])==1
        assert len(store.view({'B'})['nodes'])==2
        assert len(store.view({'A','B'})['nodes'])==2
        decisions=list_candidates(store,{'A','B'})['decisions']
        assert next(d for d in decisions if d['verdict']=='same')['status']=='needs_review'


def review_graph(store):
    """Two differently coded occurrences with the same allowed definition."""
    publish(store,'nodes',[('a','Sales expense'),('b','Sales expense')])
    pub=store.begin_publication('definitions',1,'all',{})
    support=SupportSpec(origin='user_input',source_binding='definitions',scope_ref='public',partition_id='all',
                        rule_id='definitions',rule_revision=1)
    for entity,code in [('a','A-1'),('b','SECOND-CODE')]:
        for name,value in [('business_code',code),('definition','Expense incurred in selling goods')]:
            store.stage_fact(pub,GraphFact(fact_kind='PropertyValue',payload={'entity_id':entity,'name':name,'value':value}),support)
    store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
    candidates(store)


@pytest.mark.parametrize('verdict,expected_nodes',[('supported',1),('refuted',2),('unresolved',2)])
def test_agent_graph_review_uses_original_facts_and_does_not_repeat(monkeypatch,verdict,expected_nodes):
    from lightgraph.tests.test_agent_pipeline import client,input_packet,response
    from lightgraph.identity.reconciliation import review_with_agent
    from lightgraph.documents import DocumentCorpus
    from lightgraph.contracts import ExtractionGoal
    seen=[]
    def handler(request):
        _,packet=input_packet(request);seen.append(packet)
        assert packet['semantic_proposition']['predicate']=='same_entity'
        assert {f['payload'].get('entity_id') for f in packet['graph_facts']}=={'a','b'}
        support=next(f['reference'] for f in packet['graph_facts'] if f['payload'].get('name')=='definition')
        return response({'proposition':packet['semantic_proposition'],'decision':verdict,'basis':'data_inference',
            'evidence':[{'reference':support,'stance':'supports' if verdict=='supported' else 'refutes' if verdict=='refuted' else 'context'}]})
    with GraphStore() as store:
        review_graph(store);model=client(monkeypatch,handler)
        counts,issues=review_with_agent(store,model,ExtractionGoal(purpose='Find business objects'),DocumentCorpus([]),scopes={'public'})
        assert counts['reviewed']==1 and len(store.view({'public'})['nodes'])==expected_nodes
        assert counts['rejected']==int(verdict=='refuted')
        assert counts['unresolved']==int(verdict=='unresolved')
        if verdict=='refuted':
            assert issues==[{'code':'semantic_claim_refuted','count':1,'status':'rejected'}]
            assert not list_candidates(store,{'public'})['decisions']
        review_with_agent(store,model,ExtractionGoal(),DocumentCorpus([]),scopes={'public'})
        assert len(seen)==1
        assert seen[0]['goal']['purpose']=='Find business objects'


def test_rejected_merge_is_cached_only_until_evidence_changes(monkeypatch):
    from lightgraph.tests.test_agent_pipeline import client,input_packet,response
    from lightgraph.identity.reconciliation import review_with_agent
    from lightgraph.documents import DocumentCorpus
    from lightgraph.contracts import ExtractionGoal
    verdict='refuted';seen=[]
    def handler(request):
        _,packet=input_packet(request);seen.append(packet)
        evidence=next(f['reference'] for f in packet['graph_facts'] if f['payload'].get('name')=='definition')
        return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':verdict,
            'basis':'data_inference','evidence':[{'reference':evidence,
                'stance':'refutes' if verdict=='refuted' else 'supports'}]})
    with GraphStore() as store:
        review_graph(store);before=store.view({'public'});model=client(monkeypatch,handler)
        args=(store,model,ExtractionGoal(),DocumentCorpus([]))
        first,_=review_with_agent(*args,scopes={'public'})
        assert first['rejected']==1 and first['unresolved']==0
        assert store.view({'public'})==before
        assert not list_candidates(store,{'public'})['decisions']
        verdict='supported'
        repeated,issues=review_with_agent(*args,scopes={'public'})
        assert repeated['reviewed']==0 and not issues and len(seen)==1
        publication=store.begin_publication('additional_context',1,'all',{})
        store.stage_fact(publication,GraphFact(fact_kind='PropertyValue',payload={
            'entity_id':'a','name':'description','value':'New approved evidence for review'}),
            SupportSpec(origin='user_input',source_binding='additional_context',scope_ref='public',
                partition_id='all',rule_id='additional_context',rule_revision=1))
        store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,
            consistent=True,dependencies_valid=True))
        reviewed,issues=review_with_agent(*args,scopes={'public'})
        assert reviewed['reviewed']==reviewed['accepted']==1 and reviewed['rejected']==0
        assert len(seen)==2 and not issues
        assert any(f['payload'].get('name')=='description' for f in seen[-1]['graph_facts'])
        assert len(store.view({'public'})['nodes'])==1
        assert [d['verdict'] for d in list_candidates(store,{'public'})['decisions']]==['same']


def test_agent_cannot_approve_a_generation_changed_during_review(monkeypatch):
    from lightgraph.tests.test_agent_pipeline import client,input_packet,response
    from lightgraph.identity.reconciliation import review_with_agent
    from lightgraph.documents import DocumentCorpus
    from lightgraph.contracts import ExtractionGoal
    with GraphStore() as store:
        review_graph(store)
        def handler(request):
            _,packet=input_packet(request)
            publish(store,'new_relation',[],[('a','parent_of','b')])
            evidence=next(f['reference'] for f in packet['graph_facts'] if f['payload'].get('name')=='definition')
            return response({'proposition':packet['semantic_proposition'],'decision':'supported','basis':'data_inference',
                'evidence':[{'reference':evidence,'stance':'supports'}]})
        counts,issues=review_with_agent(store,client(monkeypatch,handler),ExtractionGoal(),DocumentCorpus([]),scopes={'public'})
        assert counts['accepted']==0
        assert any(i['code']=='graph_generation_changed' for i in issues)
        assert len(store.view({'public'})['nodes'])==2


def test_agent_commit_cannot_replace_a_visible_human_same_decision():
    with GraphStore() as store:
        review_graph(store)
        listing=list_candidates(store,{'public'})
        cid=candidate_for(listing,'a','b')
        original=decide(store,cid,'same',scopes={'public'},expected_revision=0)
        with pytest.raises(LightGraphError,match='identity_human_decision_exists'):
            decide(store,cid,'same',scopes={'public'},expected_revision=1,origin='agent')
        decisions=list_candidates(store,{'public'})['decisions']
        assert len(decisions)==1 and decisions[0]['decision_id']==original['decision_id'] and decisions[0]['active']

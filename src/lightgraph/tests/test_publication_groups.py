"""Failures isolate actual replacement dependencies, not tables or whole runs."""
import pytest

from lightgraph.contracts import RuleSpec,GraphFact
from lightgraph.semantic_schema import SemanticSchema
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource
from lightgraph.rules import RuleEngine
from lightgraph.store import GraphStore
from lightgraph.store.publication_groups import dependency_groups, inspect_group
from lightgraph.tests.test_graph_store import node, prop, edge, support, complete, publish
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_concept_identity_domains import shared_rules


def node(entity):
    return GraphFact(fact_kind="NodeExistence",payload={"entity_id":entity,"kind":"ReportItem","layer":"business"})

def ordinary_relation(store):
    store.register_import_schema(SemanticSchema(relations=[{'name':'related_to','description':'Ordinary association',
        'source_kinds':['ReportItem'],'target_kinds':['ReportItem']}]))

def stage(store,rid,facts,revision=1):
    pub=store.begin_publication(rid,revision,'all',{},replace_rule=True)
    for fact in facts:store.stage_fact(pub,fact,support(rule=rid,revision=revision,partition="all"))
    return pub


def groups(store,pubs):return {frozenset(g.rule_ids):g for g in dependency_groups(store,pubs)}


def test_shared_label_conflict_does_not_block_independent_same_domain_object():
    a=shared_rules()[0];a.dependencies.documents=[]
    b=a.model_copy(deep=True,update={'rule_id':'b','source':a.source.model_copy(update={'table':'b'})})
    c=a.model_copy(deep=True,update={'rule_id':'c','source':a.source.model_copy(update={'table':'c'})})
    tables=[table(name,['code','name']) for name in ('metric','b','c')]
    data={'public.metric':[('M1','First')],'public.b':[('M1','Contradiction')],'public.c':[('M2','Independent')]}
    with GraphStore() as store,MemorySource(tables,data) as source:
        engine=RuleEngine(store);pubs=[]
        for rule in (a,b,c):
            outcome=engine.execute(source,rule,defer_publish=True)
            assert outcome.prepared,outcome.codes
            pubs+=outcome.publication_ids
        planned=groups(store,pubs)
        assert set(planned)=={frozenset({a.rule_id,b.rule_id}),frozenset({'c'})}
        conflict=inspect_group(store,planned[frozenset({a.rule_id,b.rule_id})])
        assert conflict.code=='identity_domain_attribute_conflict'
        assert set(conflict.rule_ids)=={a.rule_id,b.rule_id} and conflict.fact_refs
        independent=planned[frozenset({'c'})]
        assert inspect_group(store,independent) is None
        store.publish_partitions([complete(p) for p in independent.publication_ids],expected_heads=store.heads())
        assert [n['label'] for n in store.view({'public'})['nodes']]==['Independent']
        assert all(store._publication(p)['state']=='staging' for p in planned[frozenset({a.rule_id,b.rule_id})].publication_ids)


def test_stable_reference_chain_does_not_join_attribute_updates_or_reexecuted_rules():
    with GraphStore() as store:
        ordinary_relation(store)
        publish(store,[node('a')],rule='a');publish(store,[node('b')],rule='b');publish(store,[node('c')],rule='c')
        publish(store,[edge('a','b','related_to'),edge('b','c','related_to')],rule='ref')
        pubs=[stage(store,'a',[node('a'),prop('alias','A','a')]),stage(store,'b',[node('b')]),
              stage(store,'c',[node('c'),prop('alias','C','c')]),
              stage(store,'ref',[edge('a','b','related_to'),edge('b','c','related_to')])]
        assert set(groups(store,pubs))=={frozenset({rid}) for rid in ('a','b','c','ref')}


def test_deletion_groups_reference_with_removed_endpoint_and_preserves_old_heads_until_commit():
    with GraphStore() as store:
        ordinary_relation(store)
        publish(store,[node('a')],rule='a');publish(store,[node('b')],rule='b')
        publish(store,[edge('a','b','related_to')],rule='ref')
        old=store.heads()
        remove_a=stage(store,'a',[]);keep_ref=stage(store,'ref',[edge('a','b','related_to')])
        planned=groups(store,[remove_a,keep_ref]);assert set(planned)=={frozenset({'a','ref'})}
        conflict=inspect_group(store,next(iter(planned.values())))
        assert conflict.code=='dangling_edge_requires_dependency_publication'
        assert set(conflict.rule_ids)=={'a','ref'}
        assert store.heads()==old and len(store.view({'A'})['nodes'])==2
        store.abort(keep_ref);remove_ref=stage(store,'ref',[])
        group=dependency_groups(store,[remove_a,remove_ref])[0]
        assert set(group.rule_ids)=={'a','ref'} and inspect_group(store,group) is None
        store.publish_partitions([complete(p) for p in group.publication_ids],expected_heads=old)
        assert [n['id'] for n in store.view({'A'})['nodes']]==['b']


def test_same_taxonomy_disconnected_trees_independent_but_cycle_path_is_joint():
    with GraphStore() as store:
        # Same taxonomy/relation does not make these trees one unit.
        pubs=[stage(store,'a',[node('a'),node('b'),edge('a','b')]),
              stage(store,'b',[node('b'),node('c'),edge('b','c')]),
              stage(store,'c',[node('x'),node('y'),edge('x','y')])]
        planned=groups(store,pubs)
        assert set(planned)=={frozenset({'a','b'}),frozenset({'c'})}
        for group in planned.values():store.publish_partitions([complete(p) for p in group.publication_ids])
        reverse=stage(store,'b',[node('b'),node('c'),edge('b','a')])
        independent=stage(store,'c',[node('x'),node('y'),edge('y','x')])
        planned=groups(store,[reverse,independent]);assert len(planned)==2
        conflict=inspect_group(store,planned[frozenset({'b'})])
        assert conflict.code=='hierarchy_cycle' and set(conflict.rule_ids)=={'a','b'}
        assert inspect_group(store,planned[frozenset({'c'})]) is None


def test_complete_rule_with_two_objects_remains_one_unit_and_cas_is_retained():
    with GraphStore() as store:
        p=stage(store,'a',[node('a'),node('b')]);q=stage(store,'b',[node('b')])
        assert len(dependency_groups(store,[p,q]))==1
        group=dependency_groups(store,[p,q])[0];heads=store.heads()
        publish(store,[node('z')],rule='other')
        with pytest.raises(LightGraphError,match='graph_generation_changed'):
            store.publish_partitions([complete(x) for x in group.publication_ids],expected_heads=heads)
        assert len(store.view({'A'})['nodes'])==1


def pipeline_rules():
    base=shared_rules()[0];base.dependencies.documents=[]
    return [base.model_copy(deep=True,update={'rule_id':rid,'source':base.source.model_copy(update={'table':rid})}) for rid in ('a','b','c')]


def test_pipeline_publishes_independent_c_and_keeps_failed_mapping_for_retry(tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.runtime.candidates import CandidateQueue
    rules=pipeline_rules();tables=[table(rid,['code','name']) for rid in ('a','b','c')]
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    data={'public.a':[('M1','First')],'public.b':[('M1','Other')],'public.c':[('M2','Independent')]}
    with MemorySource(tables,data) as source:report=run_pipeline(config,[source],rules=rules)
    assert report.status=='PARTIAL' and report.rules_published==['c']
    assert set(report.rules_skipped)=={'a','b'}
    assert report.counts['publication_groups']==2 and report.counts['publication_groups_failed']==1
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert [n['label'] for n in store.view({'public'})['nodes'] if n['layer']=='business']==['Independent']
        candidates=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources).listing()['candidates']
        assert {r for c in candidates for r in c['rule_refs']}=={'a','b'}
        assert all(c['mappings'] for c in candidates)


def test_new_global_conflict_preserves_old_head_and_independent_update(tmp_path,monkeypatch):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.discovery.models import DiscoveryResult
    rules=pipeline_rules();tables=[table(rid,['code','name','bad']) for rid in ('a','b','c')]
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    data={'public.a':[('M1','Same','Wrong')],'public.b':[('M1','Same','Wrong')],'public.c':[('M2','Independent','Independent')]}
    monkeypatch.setattr('lightgraph.discovery.discover',lambda *a,**kw:DiscoveryResult(rules=[r.model_copy(deep=True) for r in rules]))
    monkeypatch.setattr('lightgraph.discovery.discover_references',lambda *a,**kw:DiscoveryResult())
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kw:([],{},[]))
    with MemorySource(tables,data) as source:first=run_pipeline(config,[source])
    assert set(first.rules_published)=={'a','b','c'}
    with GraphStore(tmp_path/'graph.sqlite') as store:old={h['logical_rule_id']:h['publication_id'] for h in store.heads() if h['valid']}
    rules[0].attributes.single_valued={'label':'bad'}
    config.rediscover=True
    with MemorySource(tables,data) as source:again=run_pipeline(config,[source])
    assert 'a' not in again.rules_published and 'c' in again.rules_published
    with GraphStore(tmp_path/'graph.sqlite') as store:
        heads={h['logical_rule_id']:h['publication_id'] for h in store.heads() if h['valid']}
        assert heads['a']==old['a']
        assert {n['label'] for n in store.view({'public'})['nodes'] if n['layer']=='business'}=={'Same','Independent'}
        feedback=[dict(r) for r in store.connection.execute("SELECT pattern_codes,rule_revisions FROM iteration_work WHERE task='review_pattern'")]
        assert feedback and all('"c"' not in r['rule_revisions'] for r in feedback)


def test_failed_new_local_mapping_does_not_withdraw_previous_valid_head(tmp_path,monkeypatch):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.discovery.models import DiscoveryResult
    rules=pipeline_rules()[:1];tables=[table('a',['code','name','bad'])]
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    data={'public.a':[('M1','Same','Wrong1'),('M1','Same','Wrong2')]}
    monkeypatch.setattr('lightgraph.discovery.discover',lambda *a,**kw:DiscoveryResult(rules=[r.model_copy(deep=True) for r in rules]))
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kw:([],{},[]))
    with MemorySource(tables,data) as source:assert run_pipeline(config,[source]).rules_published==['a']
    with GraphStore(tmp_path/'graph.sqlite') as store:old=next(h['publication_id'] for h in store.heads() if h['logical_rule_id']=='a')
    rules[0].attributes.single_valued={'label':'bad'};config.rediscover=True
    with MemorySource(tables,data) as source:again=run_pipeline(config,[source])
    assert not again.rules_published
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert any(h['logical_rule_id']=='a' and h['publication_id']==old and h['valid'] for h in store.heads())
        assert [n['label'] for n in store.view({'public'})['nodes'] if n['layer']=='business']==['Same']


def test_shared_binding_revision_migration_moves_all_contributors_together():
    from lightgraph.tests.test_graph_store import node as metric_node
    with GraphStore() as store:
        args=dict(kind='Metric',source_ref='source',namespace='A',business_key='001')
        previous=store.identity.resolve(**args,definition={'formula':'SUM(x)'})
        for rid in ('a','b'):
            pub=store.begin_publication(rid,1,'all',{},replace_rule=True)
            supp=support(rule=rid,partition='all').model_copy(update={'source_binding':previous.binding_id,'binding_revision':previous.revision})
            store.stage_fact(pub,metric_node(previous.entity_id),supp);store.publish_partition(complete(pub))
        replacement=store.identity.resolve(**args,definition={'formula':'AVG(x)'})
        pubs=[]
        for rid in ('a','b'):
            pub=store.begin_publication(rid,2,'all',{},replace_rule=True);pubs.append(pub)
            supp=support(rule=rid,revision=2,partition='all').model_copy(update={'source_binding':replacement.binding_id,'binding_revision':replacement.revision})
            store.stage_fact(pub,metric_node(replacement.entity_id),supp)
        assert len(dependency_groups(store,pubs))==1
        with pytest.raises(LightGraphError,match='binding_revision_requires_dependency_publication'):
            store.publish_partition(complete(pubs[0]))
        group=dependency_groups(store,pubs)[0]
        assert inspect_group(store,group) is None
        store.publish_partitions([complete(p) for p in group.publication_ids],expected_heads=store.heads())
        assert [n['id'] for n in store.view({'A'})['nodes']]==[replacement.entity_id]


def test_actual_taxonomy_permission_conflict_is_joint_even_for_disconnected_branches():
    def hierarchy(a,b,allow):
        value=edge(a,b);value.payload['qualifiers']={'taxonomy':'t','allow_dag':allow};return value
    with GraphStore() as store:
        dag=stage(store,'dag',[node('a'),node('b'),node('c'),hierarchy('a','c',True),hierarchy('b','c',True)])
        strict=stage(store,'strict',[node('x'),node('y'),hierarchy('x','y',False)])
        groups=dependency_groups(store,[dag,strict])
        assert len(groups)==1 and set(groups[0].rule_ids)=={'dag','strict'}
        conflict=inspect_group(store,groups[0])
        assert conflict.code=='multiple_parent' and set(conflict.rule_ids)=={'dag','strict'}

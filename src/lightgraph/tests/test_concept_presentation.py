"""Context presentation is readable and unique without changing concept identity."""
import json

import pytest
from pydantic import ValidationError

from lightgraph.contracts import RuleSpec, canonical, PartitionCompletion
from lightgraph.errors import LightGraphError
from lightgraph.evaluation.concepts import evaluate_concepts
from lightgraph.rules import RuleEngine
from lightgraph.rules.concepts import component_value, valid_concept_key
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_concept_mapping import coded_row, coded_rule, execute
from lightgraph.tests.test_concept_identity_domains import shared_rules
from lightgraph.tests.test_agent_pipeline import table


def test_context_codes_cover_source_key_scope_and_assignment_and_are_stable():
    rows=[coded_row(),coded_row(id='2'),coded_row(subject='B'),
          coded_row(id='3',product_code='P02')]
    with GraphStore() as store:
        assert execute(store,rows,coded_rule()).published
        graph=store.view({'A','B'})
        contexts=[n for n in graph['nodes'] if n['kind']=='ConceptBinding']
        assert len(contexts)==4
        assert len({n['properties']['business_code'] for n in contexts})==4
        assert len({n['label'] for n in contexts})==4
        assert all(n['properties']['business_code'].startswith('ctx_') for n in contexts)
        assert all('metric=占用' in n['label'] and 'product=梨' in n['label']
                   and 'channel=批发' in n['label'] and 'subject=' in n['label'] for n in contexts)
        before={n['id']:(n['properties']['business_code'],n['label']) for n in contexts}
        assert execute(store,rows,coded_rule()).published
        after={n['id']:(n['properties']['business_code'],n['label'])
               for n in store.view({'A','B'})['nodes'] if n['kind']=='ConceptBinding'}
        assert before==after
        assert len([n for n in graph['nodes'] if n['kind']=='Metric'])==1
        assert not store.view({'public'})['nodes']


def test_numeric_codes_preserve_types_and_accept_zero():
    rows=[coded_row(id=str(index),metric_code=value) for index,value in enumerate([0,1,1.0,'1'])]
    with GraphStore() as store:
        result=execute(store,rows,coded_rule())
        assert result.published,result.codes
        metrics=[n for n in store.view({'A'})['nodes'] if n['kind']=='Metric']
        assert len(metrics)==4
        assert {canonical(n['properties']['business_code']) for n in metrics}=={'0','1','1.0','"1"'}
    component=coded_rule().concept_mapping.components[0]
    assert component_value(component,{'metric':0})==0
    for value in (True,False,float('inf'),float('nan'),None,''):
        assert not valid_concept_key(value)
        with pytest.raises(LightGraphError,match='concept_value_missing'):
            component_value(component,{'metric':value})


def test_component_edges_must_identify_distinct_roles():
    raw=coded_rule().model_dump()
    raw['concept_mapping']['components'][1]['relation']='binds_metric'
    with pytest.raises(ValidationError,match='duplicate_concept_relation'):
        RuleSpec.model_validate(raw)


def test_evaluator_rejects_unreadable_contexts_even_with_exact_assignments(tmp_path):
    gold={'version':'lightgraph.concepts-gold/1','concepts':[
        {'role':'metric','code':0,'label':'占用'},
        {'role':'product','code':'M01','label':'梨'},
        {'role':'channel','code':'C01','label':'批发'}],
        'contexts':[{'subject':subject,'metric':0,'product':'M01','channel':'C01'} for subject in ('A','B')]}
    goldpath=tmp_path/'gold.json';goldpath.write_text(json.dumps(gold))
    with GraphStore() as store:
        assert execute(store,[coded_row(metric_code=0),coded_row(subject='B',metric_code=0)],coded_rule()).published
        graph=store.view({'A','B'})
    path=tmp_path/'graph.json'
    def score():
        path.write_text(json.dumps(graph))
        return evaluate_concepts(path,goldpath,role_kinds={'metric':'Metric','product':'Product','channel':'Channel'},
            role_relations={r:'binds_'+r for r in ('metric','product','channel')},context_kind='ConceptBinding')['accepted']
    result=score()
    assert result['concepts']['exact'] and result['joint_contexts']['exact']
    assert result['context_presentation']['passed']
    ids={n['id'] for n in graph['nodes'] if n['kind']=='ConceptBinding'}
    for fact in graph['facts']:
        p=fact['payload']
        if fact['fact_kind']=='PropertyValue' and p['entity_id'] in ids:
            if p['name']=='label':p['value']='组合上下文'
            if p['name']=='business_code':p['value']='C01'
    result=score()
    assert result['joint_contexts']['exact']
    assert not result['context_presentation']['passed']
    assert len(result['context_presentation']['unreadable_label_nodes'])==2
    assert len(result['context_presentation']['duplicate_code_groups'])==1
    assert len(result['context_presentation']['duplicate_label_groups'])==1


def test_conflicting_shared_labels_reject_atomic_group_and_valid_retry_is_not_poisoned():
    definition,context=shared_rules()[0],shared_rules()[-1]
    tables=[table('metric',['code','name']),table('items',list(coded_row()))]
    data={'public.metric':[('M01','冲突定义')],'public.items':[coded_row()]}
    with MemorySource(tables,data) as source, GraphStore() as store:
        engine=RuleEngine(store)
        outcomes=[engine.execute(source,r,document_versions={'decl@1'},defer_publish=True) for r in (definition,context)]
        assert all(o.prepared for o in outcomes)
        completions=[PartitionCompletion(publication_id=p,complete=True,consistent=True,dependencies_valid=True)
                     for o in outcomes for p in o.publication_ids]
        with pytest.raises(LightGraphError,match='identity_domain_attribute_conflict'):
            store.publish_partitions(completions)
        assert not store.view({'public','A'})['nodes']
        data['public.metric']=[('M01','占用')]
        with MemorySource(tables,data) as fixed:
            assert engine.execute(fixed,definition,document_versions={'decl@1'}).published
            assert engine.execute(fixed,context,document_versions={'decl@1'}).published
        graph=store.view({'public','A'})
        assert len([n for n in graph['nodes'] if n['kind']=='Metric'])==1

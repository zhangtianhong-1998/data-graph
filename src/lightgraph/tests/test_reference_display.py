"""Accepted reference captions are complete, scoped and separate from facts."""
from copy import deepcopy
import json

import pytest

from lightgraph.contracts import RuleSpec, canonical
from lightgraph.evaluation.concepts import evaluate_concepts
from lightgraph.presentation.export import export_graph
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.store.display import display_rules, project_display_labels
from lightgraph.store.paging import graph_page
from lightgraph.tests.test_rules_engine import table


def fixture(store):
    schema={'entity_types':[{'name':'Assignment','description':'Joint assignment'},{'name':'Product','description':'Product definition'}],
        'relations':[{'name':'assigned_'+r,'description':'Assigned '+r,
            'source_kinds':['Assignment'],'target_kinds':[k]} for r,k in [('metric','Metric'),('product','Product')]]}
    definitions=[RuleSpec(rule_id=r,kind='column_group_entity',source={'source_id':'s','table':r},
        identity={'key_columns':['code'],'entity_kind':k,'semantic_resolution':'verified_identity_domain',
            'identity_domain':r+'_codes','identity_scope':'domain_global','identity_evidence':['decl@1:codes'],
            'attribute_evidence':['decl@1:names'],'global_scope_evidence':['decl@1:scope']},
        attributes={'single_valued':{'label':'name'}},semantic_schema=schema,
        dependencies={'documents':['decl@1']}) for r,k in [('metric','Metric'),('product','Product')]]
    context=RuleSpec(rule_id='assignments',kind='column_group_entity',
        source={'source_id':'s','table':'items'},scope={'namespaces':['tenant']},
        identity={'key_columns':['tenant','metric','product'],'entity_kind':'Assignment'},semantic_schema=schema)
    refs=[RuleSpec(rule_id='link_'+r,kind='reference',source=context.source,identity=context.identity,
        relation={'source_rule_id':context.rule_id,'target_rule_id':r,'source_columns':['tenant','metric','product'],
            'target_columns':[r],'relation':'assigned_'+r},semantic_schema=schema,evidence={'semantic_basis':['declared_relation']}) for r in ('metric','product')]
    tables=[table(['code','name'],r) for r in ('metric','product')]+[table(['tenant','metric','product'])]
    rows={'public.metric':[('M0','占用')],'public.product':[('P1','梨'),('P2','梨')],
          'public.items':[('A','M0','P1'),('A','M0','P2'),('B','M0','P1')]}
    with MemorySource(tables,rows) as source:
        engine=RuleEngine(store)
        for rule in [*definitions,context,*refs]:
            result=engine.execute(source,rule,document_versions={'decl@1'})
            assert result.published,result.codes
    return store.view({'public','A','B'})


def contexts(graph):return [n for n in graph['nodes'] if n['kind']=='Assignment']


def test_reference_captions_keep_ids_codes_and_facts_and_survive_export(tmp_path):
    with GraphStore() as store:
        graph=fixture(store)
        nodes=contexts(graph)
        assert len(nodes)==3 and len({n['display_label'] for n in nodes})==3
        assert all('占用 [M0]' in n['display_label'] and '梨 [P' in n['display_label'] for n in nodes)
        assert {tuple(json.loads(n['properties']['business_code'])) for n in nodes}=={
            ('A','M0','P1'),('A','M0','P2'),('B','M0','P1')}
        assert all('占用' not in n['label'] for n in nodes)
        labels=[f['payload']['value'] for f in graph['facts'] if f['fact_kind']=='PropertyValue'
                and f['payload']['name']=='label' and f['payload']['entity_id'] in {n['id'] for n in nodes}]
        assert all('占用' not in label for label in labels)
        assert len([n for n in graph['nodes'] if n['kind']=='Product'])==2
        exported=export_graph(store,tmp_path/'export',scopes={'public','A','B'})
        assert contexts(exported)==nodes
        assert 'display_label' in (tmp_path/'export/nodes.csv').read_text()
        gold={'version':'lightgraph.concepts-gold/1','concepts':[
            {'role':'metric','code':'M0','label':'占用'},
            {'role':'product','code':'P1','label':'梨'},{'role':'product','code':'P2','label':'梨'}],
            'contexts':[{'subject':t,'metric':m,'product':p} for t,m,p in
                [('A','M0','P1'),('A','M0','P2'),('B','M0','P1')]]}
        path=tmp_path/'gold.json';path.write_text(json.dumps(gold),encoding='utf-8')
        score=evaluate_concepts(tmp_path/'export/graph.json',path,role_kinds={'metric':'Metric','product':'Product'},
            role_relations={'metric':'assigned_metric','product':'assigned_product'},context_kind='Assignment')['accepted']
        assert score['concepts']['exact'] and score['joint_contexts']['exact']
        assert score['context_presentation']['passed']


def test_paged_caption_uses_off_page_authorized_endpoints_without_loading_full_view(monkeypatch):
    with GraphStore() as store:
        graph=fixture(store)
        expected={n['id']:n['display_label'] for n in contexts(graph)}
        monkeypatch.setattr(store,'view',lambda *a,**k: (_ for _ in ()).throw(AssertionError('full view')))
        page=graph_page(store,{'public','A'},'default',kind='Assignment',limit=1)
        assert len(page['nodes'])==1 and not page['edges']
        node=page['nodes'][0];assert node['display_label']==expected[node['id']]
        assert all(f['payload'].get('entity_id')==node['id'] for f in page['facts'])
        hidden=graph_page(store,{'A'},'default',kind='Assignment',limit=1)
        assert 'display_label' not in hidden['nodes'][0]
        assert '占用' not in str(hidden) and '梨' not in str(hidden)


def test_caption_disappears_after_role_withdrawal():
    with GraphStore() as store:
        fixture(store)
        store.mark_rule_for_review('link_product','data_pattern_requires_review')
        graph=store.view({'public','A','B'})
        assert all('display_label' not in n for n in contexts(graph))


@pytest.mark.parametrize('damage',['candidate_edge','missing_role','conflicting_name','private_name','non_global_name'])
def test_incomplete_or_unscoped_evidence_never_becomes_a_complete_caption(damage):
    with GraphStore() as store:
        graph=fixture(store);rules=display_rules(store,graph)
    for n in graph['nodes']:n.pop('display_label',None);n.pop('display_label_basis',None)
    original=deepcopy(graph)
    if damage in ('candidate_edge','missing_role'):
        for f in graph['facts']:
            if f['fact_kind']=='Edge' and f['payload']['relation']=='assigned_product':f['status']='candidate'
        if damage=='missing_role':graph['facts']=[f for f in graph['facts'] if f['status']=='accepted']
    elif damage=='non_global_name':
        rules['product',1]['identity']['identity_scope']='namespace_local'
    else:
        label=next(f for f in graph['facts'] if f['fact_kind']=='PropertyValue' and
            f['payload']['name']=='label' and f['payload']['value']=='占用')
        changed=deepcopy(label);changed['id']='extra_label';changed['payload']['value']='SECRET'
        graph['facts'].append(changed)
        support=deepcopy(next(s for s in graph['supports'] if s['fact_id']==label['id']))
        support.update(fact_id=changed['id'],id='extra_support',rule_id='private',rule_revision=1,scope_ref='A' if damage=='conflicting_name' else 'private')
        graph['supports'].append(support)
        rules['private',1]={'identity':{'semantic_resolution':'source_scoped'}}
    project_display_labels(graph,rules)
    if damage=='private_name':
        assert len([n for n in contexts(graph) if 'display_label' in n])==3
        assert all('SECRET' not in n['display_label'] for n in contexts(graph))
    elif damage=='conflicting_name':
        assert len([n for n in contexts(graph) if 'display_label' in n])==1  # Only B is unambiguous.
    else:assert all('display_label' not in n for n in contexts(graph))
    assert [n['id'] for n in graph['nodes']]==[n['id'] for n in original['nodes']]

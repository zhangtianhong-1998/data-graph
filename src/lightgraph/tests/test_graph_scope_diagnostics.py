"""Explain an empty authorized view without disclosing or widening scopes."""
import json

import pytest

from lightgraph.contracts import RunReport
from lightgraph.pipeline import _graph_outcome, run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_graph_store import node, prop, publish, support
from lightgraph.tests.test_rules_engine import entity_rule, table


def test_published_private_business_graph_is_diagnosed_without_exposing_scopes(tmp_path):
    rows=[{'tenant':'PRIVATE_SCOPE_A','code':'A_CODE','label':'A_PRIVATE_LABEL'},
          {'tenant':'PRIVATE_SCOPE_B','code':'B_CODE','label':'B_PRIVATE_LABEL'}]
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    with MemorySource([table(['tenant','code','label'])],{'public.items':rows}) as source:
        report=run_pipeline(config,[source],rules=[entity_rule()])
    assert report.rules_published == ['metric']
    assert report.validation['business_graph']=='empty'
    assert report.validation['business_graph_reason']=='scope_filtered'
    assert report.validation['graph_export']=='written'
    assert report.status=='COMPLETED_WITH_GAPS'
    assert report.counts['business_nodes']==report.counts['business_edges']==0
    assert report.counts['candidate_business_nodes']==report.counts['candidate_business_edges']==0
    assert [i['code'] for i in report.issues].count('business_graph_filtered_by_scopes')==1
    assert config.scopes==['public']
    outputs=[report.model_dump_json(),(tmp_path/'out/graph.json').read_text(),
             (tmp_path/'out/report.json').read_text()]
    for value in ('PRIVATE_SCOPE_A','PRIVATE_SCOPE_B','A_CODE','B_CODE','A_PRIVATE_LABEL','B_PRIVATE_LABEL'):
        assert all(value not in text for text in outputs)
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert not [n for n in store.view({'public'})['nodes'] if n['layer']=='business']
        a=store.view({'PRIVATE_SCOPE_A'})
        b=store.view({'PRIVATE_SCOPE_B'})
        assert [n['label'] for n in a['nodes']]==['A_PRIVATE_LABEL']
        assert [n['label'] for n in b['nodes']]==['B_PRIVATE_LABEL']
        assert 'B_PRIVATE_LABEL' not in json.dumps(a)
        assert 'A_PRIVATE_LABEL' not in json.dumps(b)


@pytest.mark.parametrize('scope',['PRIVATE_SCOPE_A','PRIVATE_SCOPE_B'])
def test_visible_authorized_business_is_not_misreported_as_filtered(scope):
    with GraphStore() as store:
        publish(store,[node('a'),prop('label','A_PRIVATE_LABEL',entity='a')],scope='PRIVATE_SCOPE_A',rule='a')
        publish(store,[node('b'),prop('label','B_PRIVATE_LABEL',entity='b')],scope='PRIVATE_SCOPE_B',rule='b')
        config=RunConfig(sources=[],scopes=[scope])
        report=RunReport(run_id='run',rules_published=['a','b'])
        _graph_outcome(report,store,config,discover_only=False)
        assert report.validation['business_graph']=='entities_only'
        assert 'business_graph_reason' not in report.validation
        assert report.counts['business_nodes']==1 and not report.issues
        assert config.scopes==[scope]


@pytest.mark.parametrize('case',['old_run','history','staging','other_domain','metadata'])
def test_empty_view_diagnostic_ignores_output_not_published_by_this_run(case):
    with GraphStore() as store:
        fact=node(domain='other' if case=='other_domain' else 'default')
        if case=='metadata':
            fact.payload={'entity_id':'metadata','kind':'Table','layer':'metadata'}
        if case=='staging':
            publication=store.begin_publication('registry',1,'PRIVATE_SCOPE_A',{})
            store.stage_fact(publication,fact,support('PRIVATE_SCOPE_A'))
        else:
            publish(store,[fact],scope='PRIVATE_SCOPE_A')
        if case=='history':
            publish(store,[],scope='PRIVATE_SCOPE_A')
        report=RunReport(run_id='run',rules_published=[] if case=='old_run' else ['registry'])
        _graph_outcome(report,store,RunConfig(sources=[]),discover_only=False)
        assert report.validation['business_graph']=='empty'
        assert 'business_graph_reason' not in report.validation
        assert not report.issues
        assert report.status=='COMPLETED'

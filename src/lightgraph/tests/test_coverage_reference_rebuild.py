"""An identity scope replacement must rebuild already prepared reference edges."""
from types import SimpleNamespace

from lightgraph.contracts import RuleSpec
from lightgraph.discovery.models import DiscoveryResult
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_rules_engine import entity_rule, table


def test_scope_replacement_rebuilds_reference_before_atomic_publication(monkeypatch,tmp_path):
    source_table=table(['tenant','code','label','other','other_label'])
    first=entity_rule('first')
    first.scope.namespaces=[]
    second=entity_rule('second',code='other',label='other_label')
    relation=RuleSpec(rule_id='link',kind='reference',source=first.source,
        scope={'namespaces':['tenant']},
        identity={'key_columns':['tenant','code','other'],'entity_kind':'Metric'},
        relation={'source_rule_id':'first','target_rule_id':'second',
            'source_columns':['tenant','code'],'target_columns':['tenant','other'],'relation':'parent_of'},
        evidence={'semantic_basis':['confirmed_metric_relationship']})
    monkeypatch.setattr('lightgraph.discovery.discover',lambda *a,**kw:
        DiscoveryResult(rules=[first,second,relation]))
    monkeypatch.setattr('lightgraph.discovery.discover_references',lambda *a,**kw:DiscoveryResult())
    def revise(**kwargs):
        revised=first.model_copy(deep=True)
        revised.scope.namespaces=['tenant'];revised.revision=2
        assert kwargs['execute'](revised,preserve_previous=True)
        kwargs['rules'][:]=[revised if r.rule_id=='first' else r for r in kwargs['rules']]
        return [],{'coverage_identity_revisions':1},set()
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',revise)
    model=SimpleNamespace(explore=lambda:None,usage={'calls':0})
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=['public','A'],max_identity_reviews=0)
    with MemorySource([source_table],{'public.items':[('A','1','First','2','Second')]}) as source:
        report=run_pipeline(config,[source],model=model)
    assert report.rules_skipped==[],report.model_dump()
    assert report.counts['coverage_reference_rebuilds']==1
    assert set(report.rules_published)=={'first','second','link'}
    with GraphStore(tmp_path/'graph.sqlite') as store:
        graph=store.view({'A'})
        business={n['label']:n['id'] for n in graph['nodes'] if n['layer']=='business'}
        assert set(business)=={'First','Second'}
        assert [(e['source'],e['target']) for e in graph['edges'] if e['relation']=='parent_of']==[
            (business['First'],business['Second'])]
        assert not [n for n in store.view({'public'})['nodes'] if n['layer']=='business']

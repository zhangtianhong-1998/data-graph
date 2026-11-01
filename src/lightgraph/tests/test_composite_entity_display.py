"""Compound objects must not present their last dimension as their own code."""
import json

from lightgraph.contracts import RuleSpec
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_rules_engine import table


def test_composite_display_preserves_identity_and_exposes_every_key():
    current=table(['tenant','metric','product','channel'])
    rows=[('A','M1','P1','C1'),('A','M1','P2','C1'),('B','M1','P1','C1')]
    rule=RuleSpec(rule_id='context',kind='column_group_entity',source={'source_id':'s','table':'items'},
        scope={'namespaces':['tenant']},
        identity={'key_columns':['tenant','metric','product','channel'],'entity_kind':'ReportItem'})
    with MemorySource([current],{'public.items':rows}) as source, GraphStore() as store:
        engine=RuleEngine(store)
        assert engine.execute(source,rule).published
        view=store.view({'A','B'});nodes=view['nodes']
        assert len(nodes)==3
        assert len({n['label'] for n in nodes})==3
        assert {tuple(json.loads(n['properties']['business_code'])) for n in nodes}==set(rows)
        before={n['id'] for n in nodes}
        assert engine.execute(source,rule).published
        assert {n['id'] for n in store.view({'A','B'})['nodes']}==before


def test_scoped_single_code_keeps_existing_display_contract():
    current=table(['tenant','code','name'])
    rule=RuleSpec(rule_id='item',kind='column_group_entity',source={'source_id':'s','table':'items'},
        scope={'namespaces':['tenant']},identity={'key_columns':['tenant','code'],'entity_kind':'Metric'},
        attributes={'single_valued':{'label':'name'}})
    with MemorySource([current],{'public.items':[('A','K1','Name')]}) as source,GraphStore() as store:
        assert RuleEngine(store).execute(source,rule).published
        node=store.view({'A'})['nodes'][0]
        assert node['properties']['business_code']=='K1' and node['label']=='Name'

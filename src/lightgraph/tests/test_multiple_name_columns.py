"""Several names for one identity must survive execution and enrichment."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from lightgraph.contracts import AttributeSpec
from lightgraph.discovery.coverage import attribute_enrichment
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_rules_engine import entity_rule, table


def test_discovery_compiles_every_explicit_alias_and_english_mapping():
    from lightgraph.contracts import ExtractionGoal
    from lightgraph.discovery.engine import _column_id, _compile_model
    from lightgraph.discovery.models import ProposalBatch
    from lightgraph.tests.test_agent_pipeline import group
    t=table(['code','label','short','old','alternate','english','english2'])
    packet={'task':'T1_table_discovery','metadata_digest':{'columns':[
        {'id':_column_id(t,c.name),'name':c.name} for c in t.columns]}}
    batch=ProposalBatch.model_validate(group(packet,'code','label',extra=[
        (name,{'role':'alias','property_name':prop}) for prop,names in (
            ('alias',['short','old','alternate']),('english_name',['english','english2'])) for name in names]))
    rule=_compile_model(batch,t,ExtractionGoal(),[],[])[0]
    assert rule.attributes.multi_valued=={'alias':'short','english_name':'english'}
    assert rule.attributes.name_columns=={'alias':['old','alternate'],'english_name':['english2']}


def test_all_name_columns_project_without_splitting_identity():
    rule=entity_rule()
    rule.attributes.multi_valued={'alias':'short'}
    rule.attributes.name_columns={'alias':['old','alternate'],'english_name':['english','english2']}
    columns=['tenant','code','label','short','old','alternate','english','english2']
    with MemorySource([table(columns)],{'public.items':[
        ['A','1','应收账款占用','应收占用','应收款占用','应收占款','Receivables','Accounts receivable']
    ]}) as source, GraphStore() as store:
        result=RuleEngine(store).execute(source,rule)
        assert result.published,result.codes
        nodes=store.view({'A'})['nodes']
        assert len(nodes)==1
        assert set(nodes[0]['aliases'])=={'应收占用','应收款占用','应收占款'}
        assert set(nodes[0]['properties']['english_name'])=={'Receivables','Accounts receivable'}


def test_enrichment_adds_names_and_preserves_old_ones():
    rule=entity_rule();rule.attributes.name_columns={'alias':['old']}
    candidate=deepcopy(rule);candidate.attributes.name_columns={'alias':['new'],'english_name':['english']}
    merged=attribute_enrichment(rule,candidate)
    assert merged.attributes.name_columns=={'alias':['old','new'],'english_name':['english']}
    assert rule.attributes.name_columns=={'alias':['old']}
    candidate.identity.key_columns=['different']
    assert attribute_enrichment(rule,candidate) is None


def test_name_mapping_cannot_change_property_multiplicity_or_bypass_exclusion():
    with pytest.raises(ValidationError):
        AttributeSpec(single_valued={'alias':'one'},name_columns={'alias':['two']})
    rule=entity_rule();rule.attributes.name_columns={'alias':['secret']}
    rule.attributes.excluded=[{'column':'secret','reason':'fact_value'}]
    with MemorySource([table(['tenant','code','label','secret'])],
            {'public.items':[['A','1','Name','private']]}) as source,GraphStore() as store:
        result=RuleEngine(store).execute(source,rule)
        assert not result.published and 'excluded_column_projected' in result.codes
        assert not store.view({'A'})['nodes']

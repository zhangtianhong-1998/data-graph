"""One T2 table task must not re-probe other relation tables on the source."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ColumnSpec, RuleSpec, TableSpec
from lightgraph.discovery import BudgetAllocator, ProposalBatch, discover_references
from lightgraph.discovery.semantics import declaration_block
from lightgraph.documents import load_document
from lightgraph.sources import MemorySource
from lightgraph.tests.test_discovery_workflow import _complex_source


def small_budget():
    budget=BudgetAllocator()
    budget.limits=replace(budget.limits,db_cu=2)
    return budget


def test_deterministic_table_tasks_share_two_checks_without_rechecking_other_tables():
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in columns])
        for name,columns in [('left',['left_code']),('right',['right_code']),('links',['left_code','right_code'])]]
    rules=[RuleSpec(rule_id=name,kind='column_group_entity',source={'source_id':'s','table':name},
        identity={'key_columns':[code],'entity_kind':'ReportItem'},status='validated',validation_ref='v_'+name)
        for name,code in [('left','left_code'),('right','right_code')]]
    doc=load_document('relation.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n'+
        declaration_block('public.links','parent_of',['left_code'],['right_code']))
    budget=small_budget();found=[]
    with MemorySource(tables,{'public.left':[('A',)],'public.right':[('B',)],'public.links':[('A','B')]}) as source:
        for table in tables:
            result=discover_references(source,rules,[doc],budget=budget,table_refs={table.qualified_name})
            assert not result.issues
            assert all(rule.source.table==table.name for rule in result.rules)
            found.extend(result.rules)
        assert source.stats.queries==2
    assert len(found)==1 and budget.used.db_cu==2


@pytest.mark.parametrize('agent',[False,True])
def test_complex_fallback_only_reads_current_relation_table(agent):
    source,rules=_complex_source()
    doc=load_document('arrays.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n'+
        declaration_block('public.links','uses_measure',['tenant_id','resource_code'],['tenant_id','allowed_measure_codes']))
    budget=small_budget()
    class Model:
        config=SimpleNamespace(allow_source_samples=True,max_discovery_steps=8,max_tool_calls=16)
        def explore(self,packet,tools,**kwargs):
            tools.begin_context([packet['scope']['source_id']+':public.'+packet['scope']['table']])
            return ProposalBatch(task='T2_cross_table',abstain=True)
    model=Model() if agent else None
    if model:model.budget=budget
    found=[]
    try:
        for table in source.describe():
            result=discover_references(source,rules,[doc],model=model,budget=budget,table_refs={table.qualified_name})
            assert not any('budget' in issue['code'] for issue in result.issues)
            assert all(rule.source.table==table.name for rule in result.rules)
            found.extend(result.rules)
        assert source.stats.queries==2
        assert len(found)==1 and budget.used.db_cu==2
    finally:source.close()


@pytest.mark.parametrize('selection',[set(),{'public.resources'},{'s.public.resources'},{('s','public','resources')}])
def test_empty_or_unrelated_table_selection_does_not_probe_array_links(selection):
    source,rules=_complex_source()
    try:
        result=discover_references(source,rules,table_refs=selection)
        assert result.rules==[] and result.issues==[]
        assert source.stats.queries==0
    finally:source.close()

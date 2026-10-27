"""Optional references omit absent links, never malformed or dangling links."""
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ColumnSpec, RuleSpec, TableSpec
from lightgraph.cross_source import check_cross_source_reference
from lightgraph.discovery import BudgetAllocator, ProposalBatch
from lightgraph.discovery.workflow import discover_references_with_agent
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def fixtures(rows, *, composite=False, strict=False):
    keys=['namespace','code'] if composite else ['code']
    source_columns=['source_'+c for c in keys]
    target_columns=['target_'+c for c in keys]
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in columns])
        for name,columns in [('items',keys),('parents',keys),('links',source_columns+target_columns)]]
    instances=[('N','A'),('N','B')] if composite else [('A',),('B',)]
    data={'public.items':instances,'public.parents':instances,'public.links':rows}
    entities=[RuleSpec(rule_id=name,kind='column_group_entity',source={'source_id':'s','table':name},
        identity={'key_columns':keys,'entity_kind':'ReportItem'}) for name in ('items','parents')]
    relation=RuleSpec(rule_id='link',kind='reference',source={'source_id':'s','table':'links'},
        identity={'key_columns':source_columns,'entity_kind':'ReportItem'},
        relation={'source_rule_id':'items','target_rule_id':'parents',
            'source_columns':source_columns,'target_columns':target_columns,'relation':'parent_of',
            'require_full_coverage':strict},evidence={'semantic_basis':['user:explicit']})
    return tables,data,entities,relation


@pytest.mark.parametrize('absent',[(None,'B'),('B',None),(None,None)])
def test_optional_null_endpoint_keeps_valid_edges_and_counts_absence(absent):
    tables,data,entities,relation=fixtures([('A','B'),absent])
    with MemorySource(tables,data) as source, GraphStore() as store:
        engine=RuleEngine(store)
        for rule in entities:assert engine.execute(source,rule).published
        result=engine.execute(source,relation)
        assert result.published and not result.codes
        assert result.counts['edges']==1 and result.counts['null_endpoints_skipped']==1
        assert result.validation.checks['null_endpoints_skipped']==1
        assert len(store.view({'public'})['edges'])==1


@pytest.mark.parametrize('composite,strict,rows,code',[
    (False,True,[('A','B'),('B',None)],'reference_null_endpoint'),
    (True,False,[('N','A','N','B'),('N','B','N',None)],'reference_partial_null'),
    (False,False,[('A','B'),('B','missing')],'reference_missing'),
])
def test_optional_relation_still_rejects_broken_populated_or_required_endpoints(composite,strict,rows,code):
    tables,data,entities,relation=fixtures(rows,composite=composite,strict=strict)
    with MemorySource(tables,data) as source, GraphStore() as store:
        engine=RuleEngine(store)
        for rule in entities:assert engine.execute(source,rule).published
        result=engine.execute(source,relation)
        assert not result.published and code in result.codes
        assert store.view({'public'})['edges']==[]


def test_allowing_optional_relationships_does_not_allow_null_entity_identity():
    tables,data,entities,_=fixtures([])
    data['public.items']=[('A',),(None,)]
    with MemorySource(tables,data) as source,GraphStore() as store:
        result=RuleEngine(store).execute(source,entities[0])
        assert not result.published and result.codes==['null_key']


@pytest.mark.parametrize('strict,expected_rules,code',[(False,1,None),(True,0,'reference_null_endpoint')])
def test_agent_reference_verification_preserves_optional_coverage_contract(strict,expected_rules,code):
    tables,data,entities,_=fixtures([('A','B'),('B',None)],strict=strict)
    for rule in entities:rule.status='validated';rule.validation_ref='validation_'+rule.rule_id
    class Model:
        config=SimpleNamespace(allow_source_samples=True,max_discovery_steps=8,max_tool_calls=16)
        budget=BudgetAllocator()
        def explore(self,packet,tools,**kwargs):
            tools.begin_context([t for t in tables if t.name=='links'])
            ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
            return ProposalBatch.model_validate({'task':'T2_cross_table','proposals':[{
                'kind':'reference','claim':{'source_entity':'items','target_entity':'parents',
                    'source_columns':[ids['source_code']],'target_columns':[ids['target_code']],
                    'relation_kind':'parent_of','require_full_coverage':strict},'evidence_cited':[ids['source_code']]}]})
        def complete(self,task,packet,schema):
            aggregate=packet['probe_digest'][-1]['aggregates']
            assert aggregate['null_refs']==1 and aggregate['partial_null_refs']==0
            return schema.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference',
                'evidence':[{'reference':'backend_endpoint_verification','stance':'supports'}]})
    with MemorySource(tables,data) as source:
        result=discover_references_with_agent(source,entities,model=Model(),table_refs={'public.links'})
    assert len(result.rules)==expected_rules
    if code:assert code in {issue['code'] for issue in result.issues}
    else:
        assert result.rules[0].relation.require_full_coverage is False
        assert result.relation_table_outcomes=={'public.links':'resolved'}


def test_reference_abstention_is_an_unresolved_table_outcome():
    tables,data,entities,_=fixtures([('A','B')])
    for rule in entities:rule.status='validated';rule.validation_ref='validation_'+rule.rule_id
    class Model:
        config=SimpleNamespace(allow_source_samples=True,max_discovery_steps=8,max_tool_calls=16)
        budget=BudgetAllocator()
        def explore(self,packet,tools,**kwargs):
            tools.begin_context([tables[-1]])
            return ProposalBatch(task='T2_cross_table',abstain=True)
    with MemorySource(tables,data) as source:
        result=discover_references_with_agent(source,entities,model=Model(),table_refs={'public.links'})
    assert result.relation_table_outcomes=={'public.links':'unresolved'}
    assert result.issues==[{'table':'public.links','code':'model_abstained','status':'unresolved'}]


def test_composite_null_coverage_is_consistent_between_sql_and_cross_source():
    left=TableSpec(source_id='a',name='refs',columns=[ColumnSpec(name=c) for c in ('tenant','code')])
    right=left.model_copy(update={'source_id':'b','name':'objects'})
    local=right.model_copy(update={'source_id':'a'})
    rows=[('N','A'),(None,None),('N',None)]
    with MemorySource([left,local],{'public.refs':rows,'public.objects':[('N','A')]}) as source:
        with MemorySource([right],{'public.objects':[('N','A')]}) as target:
            sql=source.check_reference(left,['tenant','code'],local,['tenant','code'])
            remote=check_cross_source_reference(source,left,['tenant','code'],target,right,['tenant','code'])
    assert sql['null_refs']==remote['null_refs']==2
    assert sql['partial_null_refs']==remote['partial_null_refs']==1
    assert sql['unique_matches']==remote['unique_matches']==1


def test_declared_deterministic_reference_allows_absent_optional_endpoint():
    from lightgraph.discovery import discover_references
    from lightgraph.discovery.semantics import declaration_block
    from lightgraph.documents import load_document
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in columns]) for name,columns in
            [('items',['item_code']),('parents',['parent_code']),('links',['item_code','parent_code'])]]
    data={'public.items':[('A',),('B',)],'public.parents':[('P',)],'public.links':[('A','P'),('B',None)]}
    entities=[RuleSpec(rule_id=name,kind='column_group_entity',source={'source_id':'s','table':name},
        identity={'key_columns':[key],'entity_kind':'ReportItem'},status='validated',validation_ref='v_'+name)
        for name,key in [('items','item_code'),('parents','parent_code')]]
    document=load_document('relations.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n'+
        declaration_block('public.links','parent_of',['item_code'],['parent_code']))
    with MemorySource(tables,data) as source:
        result=discover_references(source,entities,documents=[document])
    assert len(result.rules)==1 and result.rules[0].relation.require_full_coverage is False
    assert not any(issue['code']=='reference_missing' for issue in result.issues)

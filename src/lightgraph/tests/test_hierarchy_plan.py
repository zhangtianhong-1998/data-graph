"""Simple hierarchy plans still require semantics and full execution checks."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.engine import _column_id, _compile_model, _source
from lightgraph.discovery.hierarchy_plan import (HierarchyPlan, compile_hierarchy_plan,
    hierarchy_plan_tool_schema)
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table


def fixture(source_id='s'):
    t=table('tree',['tenant','p1','n1','a1','p2','n2','a2','p3','n3','a3'])
    t.source_id=source_id
    ids={c.name:_column_id(t,c.name) for c in t.columns}
    packet={'task':'T0_discovery','scope':_source(t).model_dump(mode='json',by_alias=True),
        'metadata_digest':{'columns':[{'name':name,'id':value} for name,value in ids.items()]},
        'probe_digest':[{'probe_id':'probe_1'}]}
    plan={'plan_id':'objects','entity_type':'CatalogEntry','identity_scope':'source',
        'namespace_columns':[ids['tenant']],
        'positions':[{'position':i,'code':ids[f'p{i}'],'label':ids[f'n{i}'],'alias':ids[f'a{i}']}
                     for i in (1,2,3)],
        'position_meaning':'relative_ordinal','allow_multiple_parents':True,
        'repeat_is_fill':True,'relation_type':'parent_of','evidence':['probe_1']}
    return t,packet,plan


def row(tenant,*codes):
    return (tenant,)+tuple(value for code in codes for value in (code,'Object '+code,'Alias '+code))


def reviewed_rule(t,packet,plan):
    batch=compile_hierarchy_plan(plan,packet)
    return _compile_model(batch,t,ExtractionGoal(),[],[],
        semantic_evidence=['semantic_review:data_inference','probe_1'])[0]


def test_tool_contract_has_business_choices_and_does_not_expose_derived_fields():
    tool=hierarchy_plan_tool_schema()
    assert tool['function']['name']=='submit_hierarchy_plan'
    properties=tool['function']['parameters']['properties']
    assert {'identity_scope','positions','position_meaning','relation_type','evidence'}<=properties.keys()
    assert not {'identity_resolution','path_semantics','identity_domain','taxonomy','position_semantics'}&properties.keys()
    assert 'identity_scope' in tool['function']['parameters']['required']


def test_plan_derives_contract_without_semantic_approval_or_input_mutation():
    t,packet,plan=fixture();before=deepcopy(plan)
    batch=compile_hierarchy_plan(plan,packet);claim=batch.proposals[0].claim
    assert plan==before
    assert claim.identity_resolution=='verified_identity_domain' and claim.path_semantics=='relative_path'
    assert claim.position_semantics=='ordinal_slots' and claim.identity_scope=='namespace_local'
    assert claim.identity_domain and claim.taxonomy
    assert [definition.name for definition in batch.semantic_schema.entity_types]==['CatalogEntry']
    with pytest.raises(LightGraphError,match='identity_domain_evidence_unresolved'):
        _compile_model(batch,t,ExtractionGoal(),[],[])


def test_same_object_reuses_identity_across_positions_and_keeps_distinct_roles():
    t,packet,plan=fixture();rule=reviewed_rule(t,packet,plan)
    with MemorySource([t],{'public.tree':[row('T','A','B','C'),row('T','B','D','E')]}) as source,GraphStore() as store:
        assert RuleEngine(store).execute(source,rule).published
        graph=store.view({'T'})
    assert len(graph['nodes'])==5 and len(graph['edges'])==4
    reused=next(node for node in graph['nodes'] if 'Alias B' in node['aliases'])
    assert 'Alias B' in reused['aliases']
    assert any(edge['source']==reused['id'] for edge in graph['edges'])
    assert any(edge['target']==reused['id'] for edge in graph['edges'])


@pytest.mark.parametrize('allowed',[False,True])
def test_multiple_parents_require_explicit_agent_choice(allowed):
    t,packet,plan=fixture();plan['allow_multiple_parents']=allowed
    rule=reviewed_rule(t,packet,plan)
    with MemorySource([t],{'public.tree':[row('T','A','C','D'),row('T','B','C','D')]}) as source,GraphStore() as store:
        result=RuleEngine(store).execute(source,rule)
        assert result.published is allowed
        graph=store.view({'T'})
    if allowed:assert len(graph['nodes'])==4 and len(graph['edges'])==3
    else:assert not graph['nodes'] and not graph['edges']


def test_source_scope_separates_sources_and_namespaces_without_level_duplication():
    with GraphStore() as store:
        engine=RuleEngine(store)
        for source_id in ('one','two'):
            t,packet,plan=fixture(source_id)
            with MemorySource([t],{'public.tree':[row('T1','A','B','C'),row('T2','A','B','C')]}) as source:
                assert engine.execute(source,reviewed_rule(t,packet,plan)).published
        graph=store.view({'T1','T2'})
    assert len(graph['nodes'])==12 and len(graph['edges'])==8


@pytest.mark.parametrize('scope,expected_nodes',[('namespace',6),('global',3)])
def test_cross_source_sharing_requires_explicit_domain_and_scope(scope,expected_nodes):
    with GraphStore() as store:
        engine=RuleEngine(store)
        for source_id in ('one','two'):
            t,packet,plan=fixture(source_id)
            plan.update(identity_scope=scope,shared_identity_domain='approved_objects')
            with MemorySource([t],{'public.tree':[row('T1','A','B','C'),row('T2','A','B','C')]}) as source:
                assert engine.execute(source,reviewed_rule(t,packet,plan)).published
        graph=store.view({'T1','T2'})
    assert len(graph['nodes'])==expected_nodes


@pytest.mark.parametrize('change',[
    {'identity_scope':'unknown'}, {'position_meaning':'guess'}, {'relation_type':'related_to'},
    {'identity_scope':'namespace'}, {'identity_scope':'global'},
    {'identity_scope':'namespace','shared_identity_domain':'objects','namespace_columns':[]},
    {'shared_identity_domain':'objects'}, {'allow_multiple_parents':'true'},
    {'conditions':{'unknown_condition':'do not ignore'}},
])
def test_unknown_or_incomplete_plan_is_rejected_without_implicit_defaults(change):
    _,packet,plan=fixture();plan.update(change)
    with pytest.raises(ValidationError):compile_hierarchy_plan(plan,packet)


def test_missing_explicit_identity_scope_and_unissued_evidence_are_rejected():
    _,packet,plan=fixture();plan.pop('identity_scope')
    with pytest.raises(ValidationError):compile_hierarchy_plan(plan,packet)
    _,packet,plan=fixture();plan['evidence']=['unissued']
    with pytest.raises(LightGraphError,match='dangling_evidence_reference'):
        compile_hierarchy_plan(plan,packet)


def test_fixed_positions_stay_source_local_and_preserve_distinct_level_identities():
    t,packet,plan=fixture()
    plan.update(position_meaning='fixed_levels',allow_multiple_parents=False,repeat_is_fill=False)
    rule=reviewed_rule(t,packet,plan)
    assert rule.identity.level_policy=='level_qualified'
    with MemorySource([t],{'public.tree':[row('T','A','A','B')]}) as source,GraphStore() as store:
        assert RuleEngine(store).execute(source,rule).published
        graph=store.view({'T'})
    assert len(graph['nodes'])==3 and len(graph['edges'])==2

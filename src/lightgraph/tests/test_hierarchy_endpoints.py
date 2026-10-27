"""A relationship must resolve the selected hierarchy occurrence, including its level."""
import pytest

from lightgraph.contracts import ColumnSpec, RuleSpec, TableSpec, canonical
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.rules.endpoints import endpoint_columns
from lightgraph.semantic_schema import RelationDefinition, SemanticSchema
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def rules(level=2):
    hierarchy=RuleSpec(rule_id='hierarchy',kind='wide_hierarchy',source={'source_id':'s','table':'paths'},
        scope={'namespaces':['tenant']},identity={'key_columns':['tenant','first_code'],
            'entity_kind':'ReportItem','level_policy':'level_qualified'},
        hierarchy={'levels':[{'level':1,'code':'first_code','label':'first_name'},
            {'level':2,'code':'second_code','label':'second_name'}],
            'taxonomy':'items','order_evidence':['user:ordered_path']})
    center=RuleSpec(rule_id='center',kind='column_group_entity',source={'source_id':'s','table':'paths'},
        scope={'namespaces':['tenant']},identity={'key_columns':['tenant','center'],'entity_kind':'Dimension'},
        attributes={'single_valued':{'label':'center'}})
    reference=RuleSpec(rule_id='assignment',kind='reference',source={'source_id':'s','table':'paths'},
        scope={'namespaces':['tenant']},identity={'key_columns':['tenant','second_code'],'entity_kind':'ReportItem'},
        relation={'source_rule_id':'hierarchy','target_rule_id':'center','source_level':level,
                  'source_columns':['tenant','second_code'],'target_columns':['tenant','center'],'relation':'assigned_to'},
        semantic_schema=SemanticSchema(relations=[RelationDefinition(name='assigned_to',description='Assigned center',
            source_kinds=['ReportItem'],target_kinds=['Dimension'])]),
        evidence={'semantic_basis':['user:assignment']})
    return hierarchy,center,reference


def test_same_code_at_two_levels_resolves_exact_occurrence():
    hierarchy,center,reference=rules()
    columns=['tenant','first_code','first_name','second_code','second_name','center']
    table=TableSpec(source_id='s',name='paths',columns=[ColumnSpec(name=c) for c in columns])
    rows={'public.paths':[('A','K','Parent','K','Child','Center')]}
    with MemorySource([table],rows) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in (hierarchy,center,reference):
            result=engine.execute(source,rule)
            assert result.published,result.codes
        graph=store.view({'A'})
        labels={n['id']:n['label'] for n in graph['nodes']}
        assert [(labels[e['source']],labels[e['target']]) for e in graph['edges']
                if e['relation']=='assigned_to']==[('Child','Center')]


@pytest.mark.parametrize('level,code',[(None,'reference_hierarchy_level_required'),
                                      (99,'reference_hierarchy_level_unknown')])
def test_hierarchy_without_a_valid_level_is_never_guessed(level,code):
    hierarchy,_,_=rules()
    with pytest.raises(LightGraphError,match=code):endpoint_columns(hierarchy,level)


def test_level_selector_cannot_change_a_nonhierarchical_endpoint():
    _,center,_=rules()
    with pytest.raises(LightGraphError,match='reference_level_on_nonhierarchy'):
        endpoint_columns(center,2)


def test_reference_renamed_composite_namespace_keeps_endpoint_scope():
    hierarchy,center,reference=rules()
    for rule in (hierarchy,center):
        rule.scope.namespaces=['tenant','system']
        rule.identity.key_columns.insert(1,'system')
    reference.source.table='links'
    reference.scope.namespaces=['tenant_ref','system_ref']
    reference.identity.key_columns=['tenant_ref','system_ref','item_ref']
    reference.relation.source_columns=['tenant_ref','system_ref','item_ref']
    reference.relation.target_columns=['tenant_ref','system_ref','center_ref']
    specs={'paths':['tenant','system','first_code','first_name','second_code','second_name','center'],
           'links':['tenant_ref','system_ref','item_ref','center_ref']}
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in cols]) for name,cols in specs.items()]
    rows={'public.paths':[('A','S','K','Parent','K','Child','Center')],
          'public.links':[('A','S','K','Center')]}
    with MemorySource(tables,rows) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in (hierarchy,center,reference):
            result=engine.execute(source,rule)
            assert result.published,result.codes
        logical_scope=canonical({'tenant':'A','system':'S'})
        graph=store.view({logical_scope})
        assert len([e for e in graph['edges'] if e['relation']=='assigned_to'])==1
        assert {s['scope_ref'] for s in graph['supports']}=={logical_scope}
        aliases=canonical({'tenant_ref':'A','system_ref':'S'})
        assert not store.view({aliases})['nodes']

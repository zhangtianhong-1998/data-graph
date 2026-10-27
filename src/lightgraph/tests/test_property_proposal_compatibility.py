"""Model vocabulary references do not override contracts or suppress valid objects."""
import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.discovery.engine import _column_id, _compile_model
from lightgraph.discovery.models import ProposalBatch
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.semantic_schema import SemanticSchema
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore, PersistenceViolation
from lightgraph.store.policy import PersistencePolicy


def table():
    return TableSpec(source_id='s',name='centers',columns=[ColumnSpec(name=c) for c in
        ['CODE','CN_NAME','EN_NAME','PERIOD','CUSTOM','OTHER_CODE']])


def proposal(*, code_as_attribute=False, extra=(), definitions=()):
    t=table()
    members=[{'column_id':_column_id(t,'CODE'),'role':'semantic_attribute' if code_as_attribute else 'identity_code',
              'property_name':'business_code','in_identity':True},
             {'column_id':_column_id(t,'CN_NAME'),'role':'label'},
             {'column_id':_column_id(t,'EN_NAME'),'role':'semantic_attribute','property_name':'english_name'}]
    members.extend({'column_id':_column_id(t,column),'role':'semantic_attribute','property_name':name}
                   for column,name in extra)
    return ProposalBatch.model_validate({'task':'T0_discovery','semantic_schema':{'properties':list(definitions)},
        'proposals':[{'kind':'column_group','claim':{'group_id':'centers','entity_kind_hint':'Dimension','members':members},
                      'evidence_cited':[_column_id(t,'CODE')]}]})


def definition(name, **kwargs):
    return {'name':name,'description':'A property used by this object mapping.','value_type':'string',
            'entity_kinds':['Dimension'],**kwargs}


@pytest.mark.parametrize('code_as_attribute',[False,True])
def test_model_builtin_references_and_code_attribute_produce_actual_entities(code_as_attribute):
    batch=proposal(code_as_attribute=code_as_attribute,
        extra=[('CODE','business_code'),('PERIOD','period'),('CUSTOM','center_status')],
        definitions=[definition('label'),definition('business_code'),definition('english_name',multi_valued=True),
                     definition('center_status')])
    goal=ExtractionGoal(purpose='Extract responsibility center definitions.')
    rule=_compile_model(batch,table(),goal,[],[])[0]
    assert rule.identity.key_columns==['CODE']
    assert 'business_code' not in rule.attributes.single_valued
    assert {p.name for p in rule.semantic_schema.properties}=={'center_status'}
    rows=[('001','中心甲','Center A','2026','active','other'),
          ('001','中心甲','Center A','2026','active','other'),
          ('002','中心乙','Center B','2026','closed','other')]
    with GraphStore() as store, MemorySource([table()],{'public.centers':rows}) as source:
        store.configure_goal(goal,1)
        outcome=RuleEngine(store).execute(source,rule)
        assert outcome.published,outcome.codes
        nodes=store.view({'public'})['nodes']
        assert len(nodes)==2
        assert {n['properties']['business_code'] for n in nodes}=={'001','002'}
        assert {n['properties']['english_name'] for n in nodes}=={'Center A','Center B'}
        assert {n['properties']['center_status'] for n in nodes}=={'active','closed'}


def test_unused_invalid_schema_cannot_block_or_register_through_valid_sibling():
    batch=proposal(definitions=[definition('level_no',value_type='integer')])
    rule=_compile_model(batch,table(),ExtractionGoal(),[],[])[0]
    assert rule.semantic_schema.properties==[]
    with pytest.raises(PersistenceViolation,match='builtin_property_definition_forbidden'):
        PersistencePolicy().with_schema(batch.semantic_schema)


def test_business_code_mapping_cannot_silently_overwrite_a_different_column():
    batch=proposal(extra=[('OTHER_CODE','business_code')])
    with pytest.raises(LightGraphError,match='business_code_mapping_conflict'):
        _compile_model(batch,table(),ExtractionGoal(),[],[])


def test_unknown_property_still_requires_a_definition():
    with pytest.raises(LightGraphError,match='unregistered_attribute_semantics'):
        _compile_model(proposal(extra=[('CUSTOM','center_status')]),table(),ExtractionGoal(),[],[])


def test_builtin_metadata_and_incompatible_name_type_cannot_be_registered():
    for item in [definition('source_ref'),definition('label',value_type='integer'),definition('label',multi_valued=True)]:
        with pytest.raises(PersistenceViolation,match='builtin_property_definition_forbidden'):
            PersistencePolicy().with_schema(SemanticSchema.model_validate({'properties':[item]}))


@pytest.mark.parametrize('restriction',['excluded_columns','evidence_only_columns'])
def test_dynamic_property_and_builtin_reference_do_not_bypass_output_scope(restriction):
    batch=proposal(extra=[('CUSTOM','center_status')],definitions=[definition('label'),definition('center_status')])
    goal=ExtractionGoal(**{restriction:['CUSTOM']})
    rule=_compile_model(batch,table(),goal,[],[])[0]
    assert 'CUSTOM' not in rule.attributes.single_valued.values()
    assert rule.semantic_schema.properties==[]


def test_builtin_reference_does_not_add_a_dynamic_constraint_on_other_kinds():
    policy=PersistencePolicy().with_schema(SemanticSchema.model_validate({'properties':[definition('label')]}))
    assert policy.semantic_schema.properties==[] and not policy.property_definitions
    policy.check_property_kind('label','Metric')

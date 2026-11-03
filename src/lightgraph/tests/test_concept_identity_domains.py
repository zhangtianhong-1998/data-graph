"""Cross-table code identity needs evidence, and keeps source supports separate."""
import pytest
from pydantic import ValidationError
from lightgraph.contracts import RuleSpec, ExtractionGoal
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table, client, input_packet, response
from lightgraph.tests.test_concept_mapping import coded_rule, coded_row


def shared_rules(shared=True):
    context=coded_rule()
    definitions=[]
    for index, component in enumerate(context.concept_mapping.components):
        if shared:
            component=component.model_copy(update={'identity_domain':'fruit_'+component.role,
                'identity_evidence':['decl@1:codes']})
            context.concept_mapping.components[index]=component
        definitions.append(RuleSpec(rule_id='definition_'+component.role,kind='column_group_entity',
            source={'source_id':'s','table':component.role},
            identity={'key_columns':['code'],'entity_kind':component.entity_kind,
                'semantic_resolution':'verified_identity_domain','identity_domain':'fruit_'+component.role,
                'identity_evidence':['decl@1:codes'],'attribute_evidence':['decl@1:codes']},
            attributes={'single_valued':{'label':'name'}},semantic_schema=context.semantic_schema,
            dependencies={'documents':['decl@1']}))
    return [*definitions,context]


@pytest.mark.parametrize('reverse',[False,True])
def test_declared_domain_reuses_definitions_in_both_publication_orders(reverse):
    rules=shared_rules()
    tables=[table(role,['code','name']) for role in ('metric','product','channel')]
    tables.append(table('items',list(coded_row())))
    data={'public.items':[coded_row(),coded_row(subject='B'),coded_row(id='2',product_code='P02')]}
    data.update({'public.'+role:[(code,label)] for role,code,label in [
        ('metric','M01','占用'),('product','M01','梨'),('channel','C01','批发')]})
    data['public.product'].append(('P02','梨'))
    with MemorySource(tables,data) as source, GraphStore() as store:
        engine=RuleEngine(store)
        for rule in reversed(rules) if reverse else rules:
            result=engine.execute(source,rule,document_versions={'decl@1'})
            assert result.published,result.codes
        graph=store.view({'public','A','B'})
        assert len(graph['nodes'])==7  # 3 contexts, 4 independently coded objects.
        assert len(graph['edges'])==9
        metrics=[n for n in graph['nodes'] if n['kind']=='Metric'];assert len(metrics)==1
        products=[n for n in graph['nodes'] if n['kind']=='Product'];assert len(products)==2
        assert all(n['label']=='梨' for n in products)
        assert metrics[0]['id'] not in {n['id'] for n in products}
        # Public master data grants no access to a subject's joint observations.
        assert len(store.view({'public'})['nodes'])==4
        assert not store.view({'public'})['edges']
        assert len(store.view({'B'})['edges'])==3


def test_unshared_domains_do_not_merge_equal_codes_and_conflicting_labels_block_publication():
    tables=[table('items',list(coded_row())),table('metric',['code','name'])]
    with MemorySource(tables,{'public.items':[coded_row()],'public.metric':[('M01','占用')]}) as source, GraphStore() as store:
        rule,context=shared_rules(False)[0],shared_rules(False)[-1]
        engine=RuleEngine(store)
        assert engine.execute(source,rule,document_versions={'decl@1'}).published
        assert engine.execute(source,context,document_versions={'decl@1'}).published
        assert len([n for n in store.view({'public','A'})['nodes'] if n['kind']=='Metric'])==2
    with MemorySource(tables,{'public.items':[coded_row()],'public.metric':[('M01','不同指标')]}) as source, GraphStore() as store:
        rule,context=shared_rules()[0],shared_rules()[-1];engine=RuleEngine(store)
        assert engine.execute(source,rule,document_versions={'decl@1'}).published
        result=engine.execute(source,context,document_versions={'decl@1'})
        assert not result.published and 'identity_domain_attribute_conflict' in result.codes
        graph=store.view({'public','A'})
        assert len(graph['nodes'])==1 and graph['nodes'][0]['label']=='不同指标'
        assert not graph['edges']


@pytest.mark.parametrize('field,value,code',[
    ('key_column',None,'concept_identity_domain_evidence_required'),
    ('identity_evidence',[],'concept_identity_domain_evidence_required'),
    ('identity_evidence',['unknown@1:codes'],'concept_identity_document_required'),
])
def test_concept_domain_requires_code_and_declared_evidence(field,value,code):
    raw=shared_rules()[-1].model_dump();raw['concept_mapping']['components'][0][field]=value
    with pytest.raises(ValidationError,match=code):RuleSpec.model_validate(raw)

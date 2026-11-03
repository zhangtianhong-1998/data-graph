"""Concept reuse and exact joint context are independent acceptance outcomes."""
import pytest
from pydantic import ValidationError
from lightgraph.contracts import RuleSpec, ExtractionGoal
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_rules_engine import table


def concept_rule(split=True):
    components=[]
    for position,(role,kind) in enumerate([('metric','Metric'),('product','Product'),('channel','Channel')]):
        component=dict(role=role,entity_kind=kind,column='label' if split else role,
                       definition_ref='fruit:'+role,relation='binds_'+role)
        if split:component.update(delimiter='-',position=position,segment_count=3)
        components.append(component)
    return RuleSpec(rule_id='concepts',kind='concept_mapping',
        source={'source_id':'s','table':'items'},
        identity={'key_columns':['id'],'entity_kind':'ConceptBinding'},
        concept_mapping={'evidence_ref':'decl@1','components':components},
        dependencies={'documents':['decl@1']},
        semantic_schema={
            'entity_types':[{'name':k,'description':d} for k,d in [
                ('ConceptBinding','Joint assignment from one source report item'),
                ('Product','Product category'),('Channel','Sales channel')]],
            'relations':[{'name':'binds_'+c['role'],'description':'Assigns the declared '+c['role'],
                          'source_kinds':['ConceptBinding'],'target_kinds':[c['entity_kind']]} for c in components]})


def execute(store,rows,rule=None):
    t=table(list(rows[0]))
    with MemorySource([t],{'public.items':rows}) as source:
        return RuleEngine(store).execute(source,rule or concept_rule(),document_versions={'decl@1'})


def test_reuses_concepts_without_inventing_product_channel_combinations():
    with GraphStore() as store:
        result=execute(store,[{'id':'1','label':'应收账款占用-柑橘-批发渠道'},
                              {'id':'2','label':'应收账款占用-苹果-零售渠道'}])
        assert result.published,result.codes
        graph=store.view({'public'}); nodes=graph['nodes']; edges=graph['edges']
        assert len([n for n in nodes if n['kind']=='Metric'])==1
        assert len([n for n in nodes if n['kind']=='ConceptBinding'])==2
        labels={n['id']:n['label'] for n in nodes}
        contexts=[n for n in nodes if n['kind']=='ConceptBinding']
        observed={frozenset(labels[e['target']] for e in edges if e['source']==n['id']) for n in contexts}
        assert observed=={frozenset(['应收账款占用','柑橘','批发渠道']),frozenset(['应收账款占用','苹果','零售渠道'])}


def test_physical_columns_do_not_implicitly_split_hyphens():
    with GraphStore() as store:
        result=execute(store,[{'id':'1','metric':'收入-费用差','product':'柑橘','channel':'批发'}],concept_rule(False))
        assert result.published,result.codes
        assert next(n for n in store.view({'public'})['nodes'] if n['kind']=='Metric')['label']=='收入-费用差'


def test_bad_split_or_same_context_with_conflicting_assignment_publishes_nothing():
    for rows in [[{'id':'1','label':'占用-柑橘-批发-额外'}],
                 [{'id':'1','label':'占用-柑橘-批发'},{'id':'1','label':'占用-苹果-零售'}]]:
        with GraphStore() as store:
            result=execute(store,rows)
            assert not result.published
            assert not store.view({'public'})['nodes']


def test_same_label_with_different_definition_does_not_merge():
    with GraphStore() as store:
        first=concept_rule(); second=concept_rule()
        second.rule_id='other'
        second.concept_mapping.components[0].definition_ref='other-calculation'
        assert execute(store,[{'id':'1','label':'占用-柑橘-批发'}],first).published
        assert execute(store,[{'id':'2','label':'占用-柑橘-批发'}],second).published
        assert len([n for n in store.view({'public'})['nodes'] if n['kind']=='Metric'])==2


@pytest.mark.parametrize('different_definition',[True,False])
def test_same_source_key_cannot_mix_distinct_joint_assignments(different_definition):
    with GraphStore() as store:
        first=concept_rule(); second=concept_rule(); second.rule_id='other'
        label='占用-柑橘-批发'
        if different_definition:second.concept_mapping.components[0].definition_ref='other-definition'
        else:label='占用-苹果-零售'
        assert execute(store,[{'id':'1','label':'占用-柑橘-批发'}],first).published
        assert execute(store,[{'id':'1','label':label}],second).published
        graph=store.view({'public'})
        contexts=[n['id'] for n in graph['nodes'] if n['kind']=='ConceptBinding']
        assert len(contexts)==2
        assert all(len([e for e in graph['edges'] if e['source']==context and e['relation']=='binds_metric'])==1
                   for context in contexts)
        # Revising the original rule replaces its whole assignment; it cannot
        # leave the earlier context's role edges attached to the new one.
        first.revision=2
        assert execute(store,[{'id':'1','label':'占用-葡萄-直营'}],first).published
        graph=store.view({'public'})
        assert len([n for n in graph['nodes'] if n['kind']=='ConceptBinding'])==2
        assert len(graph['edges'])==6


def test_source_permissions_and_discovery_semantic_review_are_required():
    with GraphStore() as store:
        store.configure_goal(ExtractionGoal(entity_kinds=[],excluded_columns=['label']))
        result=execute(store,[{'id':'1','label':'占用-柑橘-批发'}])
        assert not result.published and 'excluded_column_projected' in result.codes
    with GraphStore() as store:
        rule=concept_rule(); rule.evidence.origin='discovery'
        result=execute(store,[{'id':'1','label':'占用-柑橘-批发'}],rule)
        assert not result.published and 'semantic_evidence_unresolved' in result.codes
    with GraphStore() as store:
        rule=concept_rule();rule.scope.namespaces=['tenant']
        result=execute(store,[{'id':'1','tenant':None,'label':'占用-柑橘-批发'}],rule)
        assert not result.published and 'null_identity_namespace' in result.codes




def test_declaration_is_mandatory_and_segment_contract_is_complete():
    raw=concept_rule().model_dump()
    raw['dependencies']['documents']=[]
    with pytest.raises(ValidationError,match='concept_mapping_document_required'):
        RuleSpec.model_validate(raw)
    raw=concept_rule().model_dump(); raw['concept_mapping']['components'][0]['position']=None
    with pytest.raises(ValidationError,match='concept_segment_contract_required'):
        RuleSpec.model_validate(raw)




def coded_rule():
    rule=concept_rule(False)
    rule.scope.namespaces=['subject']
    rule.identity.key_columns=['subject','id']
    for component in rule.concept_mapping.components:
        component.key_column=component.role+'_code'
        component.namespace_columns=[]
        component.scope_evidence_ref='decl@1:shared_codes'
    return RuleSpec.model_validate(rule.model_dump())


def coded_row(id='1',subject='A',metric_code='M01',metric='占用',product_code='M01'):
    return dict(id=id,subject=subject,metric_code=metric_code,metric=metric,
        product_code=product_code,product='梨',channel_code='C01',channel='批发')


def test_independent_codes_and_cross_subject_sharing_keep_row_permissions():
    with GraphStore() as store:
        rows=[coded_row(),coded_row(subject='B'),coded_row(id='2',metric_code='M02')]
        outcome=execute(store,rows,coded_rule());assert outcome.published,outcome.codes
        allnodes=store.view({'A','B'})['nodes']
        metrics=[n for n in allnodes if n['kind']=='Metric']
        assert len(metrics)==2 and {n['label'] for n in metrics}=={'占用'}
        assert {n['properties']['business_code'] for n in metrics}=={'M01','M02'}
        assert len([n for n in allnodes if n['kind']=='Product'])==1  # Same code in another type.
        assert len(allnodes)==7  # Four concepts and three distinct row contexts.
        a=store.view({'A'});b=store.view({'B'})
        assert len([n for n in a['nodes'] if n['kind']=='ConceptBinding'])==2
        assert len([n for n in b['nodes'] if n['kind']=='ConceptBinding'])==1
        assert all(s['scope_ref']=='B' for s in b['supports'])
        assert not store.view({'public'})['nodes']


@pytest.mark.parametrize('change,code',[
    ('missing_key','concept_key_missing'),('label_conflict','concept_key_label_conflict')])
def test_coded_concept_requires_populated_key_and_consistent_definition(change,code):
    rows=[coded_row()]
    rows.append(coded_row(id='2',metric_code='' if change=='missing_key' else 'M01',metric='other'))
    with GraphStore() as store:
        outcome=execute(store,rows,coded_rule())
        assert not outcome.published and code in outcome.codes
        assert not store.view({'A','B'})['nodes']


def test_concept_scope_reduction_requires_evidence_and_key_is_subject_to_column_permissions():
    raw=coded_rule().model_dump();raw['concept_mapping']['components'][0]['scope_evidence_ref']=None
    with pytest.raises(ValidationError,match='concept_scope_evidence_required'):RuleSpec.model_validate(raw)
    with GraphStore() as store:
        store.configure_goal(ExtractionGoal(excluded_columns=['metric_code']))
        outcome=execute(store,[coded_row()],coded_rule())
        assert not outcome.published and 'excluded_column_projected' in outcome.codes

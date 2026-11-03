"""Cross-table code reuse requires an explicit independently reviewed domain."""
import pytest
from pydantic import ValidationError

from lightgraph.contracts import RuleSpec
from lightgraph.discovery.engine import _check_code_domain_evidence
from lightgraph.discovery.semantics import document_ref
from lightgraph.documents import load_document
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.rules import RuleEngine
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, table
from lightgraph.tests.test_concept_mapping import coded_rule, coded_row, execute


def definition_rule(shared=True):
    identity={'key_columns':['code'],'entity_kind':'Metric'}
    if shared:identity.update(semantic_resolution='verified_identity_domain',
        identity_domain='group_metric_codes',identity_evidence=['decl@1:codes'],attribute_evidence=['decl@1:codes'])
    return RuleSpec(rule_id='definition',kind='column_group_entity',
        source={'source_id':'s','table':'definitions'},identity=identity,
        attributes={'single_valued':{'label':'name'}},dependencies={'documents':['decl@1']})


@pytest.mark.parametrize('shared',[False,True])
def test_explicit_domain_reuses_definition_node_and_keeps_occurrence_scope(shared):
    rule=coded_rule()
    if shared:
        component=rule.concept_mapping.components[0]
        rule.concept_mapping.components[0]=component.model_copy(update={
            'identity_domain':'group_metric_codes','identity_evidence':['decl@1:codes']})
    with GraphStore() as store:
        assert execute(store,[coded_row()],rule).published
        with MemorySource([table('definitions',['code','name'])],{'public.definitions':[('M01','占用')]}) as source:
            outcome=RuleEngine(store).execute(source,definition_rule(shared),document_versions={'decl@1'})
        assert outcome.published,outcome.codes
        graph=store.view({'public','A'})
        assert len([n for n in graph['nodes'] if n['kind']=='Metric'])==(1 if shared else 2)
        assert not [n for n in store.view({'public'})['nodes'] if n['kind']=='ConceptBinding']
        assert len([n for n in graph['nodes'] if n['kind']=='Product'])==1  # Same text M01, other type.


def test_shared_domain_conflicting_definition_cannot_replace_existing_graph():
    rule=coded_rule();component=rule.concept_mapping.components[0]
    rule.concept_mapping.components[0]=component.model_copy(update={
        'identity_domain':'group_metric_codes','identity_evidence':['decl@1:codes']})
    with GraphStore() as store:
        assert execute(store,[coded_row()],rule).published
        before=store.view({'public','A'})
        with MemorySource([table('definitions',['code','name'])],{'public.definitions':[('M01','不同指标')]}) as source:
            outcome=RuleEngine(store).execute(source,definition_rule(),document_versions={'decl@1'})
        assert not outcome.published
        assert set(outcome.codes)&{'identity_domain_attribute_conflict','identity_definition_conflict'}
        assert store.view({'public','A'})['nodes']==before['nodes']


def test_concept_domain_requires_code_and_document_evidence():
    raw=coded_rule().model_dump();component=raw['concept_mapping']['components'][0]
    component['identity_domain']='group_metric_codes'
    with pytest.raises(ValidationError,match='concept_identity_domain_evidence_required'):RuleSpec.model_validate(raw)
    component['identity_evidence']=['unknown@1:codes']
    with pytest.raises(ValidationError,match='concept_identity_document_required'):RuleSpec.model_validate(raw)


@pytest.mark.parametrize('change',[{'read_state':'partial'},{'derived':True},
    {'evidence_role':'analysis_guidance'},{'sources':[{'source_id':'other','table':'definitions'}]}])
def test_code_domain_cannot_use_partial_derived_analytical_or_out_of_scope_documents(change):
    source_table=table('definitions',['code','name'])
    doc={'doc_id':'decl','revision':'1','section_id':'codes',**change}
    with pytest.raises(LightGraphError,match='identity_domain_evidence_unresolved'):
        _check_code_domain_evidence(['decl@1:codes'],[doc],source_table,['semantic_review:supported'])


@pytest.mark.parametrize('refs,review',[(['decl@1:invented'],['semantic_review:supported']),(['decl@1:codes'],[])])
def test_code_domain_needs_issued_section_and_independent_review(refs,review):
    with pytest.raises(LightGraphError,match='identity_domain_evidence_unresolved'):
        _check_code_domain_evidence(refs,[{'doc_id':'decl','revision':'1','section_id':'codes'}],
                                   table('definitions',['code','name']),review)

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


@pytest.mark.parametrize('proposal_kind',['entity_key','column_group'])
def test_agent_code_domain_review_and_compilation_reuse_existing_concept(monkeypatch,tmp_path,proposal_kind):
    stages=[]
    doc=load_document('codes.md',text='---\npurpose: data_description\n---\n# 定义\n指标编码在集团全局编码域内唯一，实例表和定义表指同一对象；主体限制组合，其他类型编码独立。')
    def handler(request):
        _,packet=input_packet(request);stages.append(packet['task'])
        if packet['task']=='semantic_review':
            return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':'supported',
                'basis':'document_interpretation','evidence':[{'reference':document_ref(d),'stance':'supports'} for d in packet['document_snippets']]})
        if packet['task']!='T0_discovery':return response({'task':packet['task'],'abstain':True})
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        d=packet['document_snippets'][0];ref=document_ref(d);rule=coded_rule()
        if packet['scope']['table']=='items':
            domains=packet['iteration_context']['prior_identity_domains']
            assert domains['total']==1 and not domains['truncated']
            assert domains['items'][0]['identity_domain']=='group_metric_codes'
            assert domains['items'][0]['status']=='hypothesis_requires_local_review'
            mapping=rule.concept_mapping.model_dump();mapping['evidence_ref']=d['doc_id']+'@'+d['revision']
            for component in mapping['components']:
                component['column']=ids[component['role']];component['key_column']=ids[component['role']+'_code']
                component['definition_ref']=component['scope_evidence_ref']=ref
            mapping['components'][0].update(identity_domain='group_metric_codes',identity_evidence=[ref])
            kind='concept_mapping';claim={**mapping,'group_id':'contexts','entity_kind_hint':'ConceptBinding',
                'key_columns':[ids['id']],'namespace_columns':[ids['subject']]}
        else:
            assert packet['iteration_context']['prior_identity_domains']['total']==0
            kind=proposal_kind;claim={'group_id':'metrics','entity_kind_hint':'Metric',
                'identity_resolution':'verified_identity_domain','identity_domain':'group_metric_codes','identity_evidence':[ref]}
            if kind=='entity_key':claim.update(key_columns=[ids['code']],attributes={
                'single_valued':[ids['name']],'property_names':{ids['name']:'label'}})
            else:claim['members']=[{'column_id':ids['code'],'role':'identity_code','in_identity':True},
                                   {'column_id':ids['name'],'role':'label'}]
        return response({'task':packet['task'],'semantic_schema':rule.semantic_schema.model_dump(),
            'proposals':[{'kind':kind,'claim':claim,'evidence_cited':[ref]}]})
    row=coded_row();model=client(monkeypatch,handler);model.config.max_prompt_bytes=131072
    with MemorySource([table('items',list(row)),table('definitions',['code','name'])],
            {'public.items':[row],'public.definitions':[('M01','占用')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),scopes=['public','A'],max_identity_reviews=0),
            [source],model=model,documents=[doc])
    assert stages.count('semantic_review')==2
    assert len(report.rules_published)==2,report.model_dump()
    assert report.counts['business_nodes']==4 and report.counts['business_edges']==3
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert len([n for n in store.view({'public','A'})['nodes'] if n['kind']=='Metric'])==1

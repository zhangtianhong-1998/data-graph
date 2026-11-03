"""Semantic regressions distinguish declaration/model decisions from overlap.

Reviewer fixtures test transport and admission, not natural-language accuracy.
Negative no-model cases execute the real discovery and publication path.
"""
from types import SimpleNamespace

import pytest

from lightgraph.contracts import (ColumnSpec, TableSpec, SourceRef, RuleSpec, ExtractionGoal,
    IdentitySpec, AttributeSpec, ScopeSpec, RuleEvidence)
from lightgraph.discovery import discover
from lightgraph.discovery.semantics import (SemanticReview, declaration_block,
    declared_evidence, review_semantics, document_ref)
from lightgraph.documents import load_document, DocumentCorpus
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.rules import RuleEngine
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, group


def table(name, columns):
    return TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in columns])


def description(name, text):
    return load_document('context.md',text=(
        '---\npurpose: data_description\napplies_to: [public.'+name+']\n---\n# 说明\n'+text))






def test_exact_declaration_has_direction_condition_scope_and_denial():
    t=table('mapping',['left_code','right_code','flag'])
    positive=declaration_block('public.mapping','related_to',['left_code'],['right_code'],conditions={'flag':'active'})
    negative=declaration_block('public.mapping','related_to',['left_code'],['right_code'],
        conditions={'flag':'active'},polarity='denied')
    corpus=DocumentCorpus([description('mapping',positive)])
    docs=corpus.context_for(SourceRef(source_id='s',table='mapping'),t.column_names)
    assert declared_evidence(docs,t,'related_to',['left_code'],['right_code'],conditions={'flag':'active'})
    assert not declared_evidence(docs,t,'related_to',['right_code'],['left_code'],conditions={'flag':'active'})
    assert not declared_evidence(docs,t,'related_to',['left_code'],['right_code'])
    corpus=DocumentCorpus([description('mapping',positive+'\n'+negative)])
    docs=corpus.context_for(SourceRef(source_id='s',table='mapping'),t.column_names)
    assert not declared_evidence(docs,t,'related_to',['left_code'],['right_code'],conditions={'flag':'active'})


def review_fixture():
    docs=[{'doc_id':'context','revision':'1','section_id':'rules','text':'限定条件和反证均需要复核。',
        'evidence_role':'definition_evidence','read_state':'complete'}]
    packet={'metadata_digest':{'columns':[{'id':'c_a'},{'id':'c_b'}]},
        'probe_digest':[{'probe_id':'probe_1'}],'authorized_entities':['entity_a','entity_b']}
    proposition={'relation':'related_to','source':'entity_a','target':'entity_b'}
    response={'proposition':proposition,'decision':'supported','basis':'data_inference',
        'evidence':[{'reference':document_ref(docs[0]),'stance':'context'},
                    {'reference':'probe_1','stance':'supports'}],'conditions':[]}
    return docs,packet,proposition,response


@pytest.mark.parametrize('change,code',[
    (lambda r:r.update(proposition={'relation':'wrong'}),'semantic_review_proposition_mismatch'),
    (lambda r:r['evidence'].append({'reference':'invented','stance':'supports'}),'semantic_review_unissued_evidence'),
    (lambda r:r['evidence'].pop(0),'semantic_review_incomplete_context'),
    (lambda r:r['evidence'][0].update(stance='refutes'),'semantic_claim_refuted'),
    (lambda r:r.update(decision='unresolved'),'semantic_claim_unresolved'),
    (lambda r:r.update(conditions=[{'state':'unknown','evidence_refs':['probe_1']}]),'semantic_condition_unresolved'),
    (lambda r:r.update(conditions=[{'state':'satisfied','evidence_refs':[]}]),'semantic_condition_unresolved'),
])
def test_semantic_review_rejects_structurally_invalid_or_unresolved_claim(change,code):
    docs,packet,proposition,review=review_fixture();change(review)
    model=SimpleNamespace(complete=lambda task,payload,schema:
        schema.model_validate(review if schema is SemanticReview else {}))
    with pytest.raises(LightGraphError,match=code):review_semantics(model,packet,proposition,docs)


def test_semantic_review_allows_inference_without_explicit_statement_or_shared_code():
    docs,packet,proposition,review=review_fixture()
    model=SimpleNamespace(complete=lambda *args:SemanticReview.model_validate(review))
    refs=review_semantics(model,packet,proposition,docs)
    assert refs==['semantic_review:data_inference','probe_1']
    assert '限定条件' not in str(refs)


def test_guidance_cannot_become_confirming_evidence_by_model_stance():
    docs,packet,proposition,review=review_fixture()
    docs[0]['evidence_role']='exploration_guidance';review['evidence'][0]['stance']='supports'
    model=SimpleNamespace(complete=lambda *args:SemanticReview.model_validate(review))
    with pytest.raises(LightGraphError,match='semantic_review_missing_support'):
        review_semantics(model,packet,proposition,docs)


def test_independent_review_receives_bounded_sql_observations_only_in_memory(monkeypatch,tmp_path,capsys,caplog):
    from lightgraph.tests.test_discovery_agent import (Toolbox,CANARY,input_packet as agent_packet,
        response as agent_response,final as agent_final)
    from lightgraph.runtime.retention import retain_check
    from lightgraph.discovery.observations import ReviewObservations
    calls=[]
    def handler(request):
        payload,p=input_packet(request);calls.append(p['task'])
        if p['task']=='semantic_review':
            assert p['tool_observations'][0]['columns']==['label']
            assert p['tool_observations'][0]['row_format']=='positional_values'
            assert p['tool_observations'][0]['rows']==[[CANARY]]
            assert p['tool_observations'][0]['probe_id']=='probe_0002'
            assert not p['tool_observations'][0]['full_source_validation']
            return response({'proposition':p['semantic_proposition'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':'probe_0002','stance':'supports'}],
                'conditions':[]})
        if len(payload['messages'])==2:
            return agent_response(tool='execute_readonly_sql',args={'sql':'SELECT opaque_a FROM t'})
        return agent_response(agent_final(evidence='probe_0002'))
    model=client(monkeypatch,handler);packet=agent_packet();packet['strata_samples']=[]
    with ReviewObservations() as observations:
        model.explore(packet,Toolbox(),review_observations=observations)
        assert CANARY not in str(packet)
        borrowed=observations.values()[0]['rows']
        refs=review_semantics(model,packet,{'predicate':'related_to'},[],observations=observations)
    assert borrowed==[]
    assert CANARY not in str(packet)+repr(observations)
    assert refs==['semantic_review:data_inference','probe_0002']
    assert calls==['T1_table_discovery','T1_table_discovery','semantic_review']
    assert CANARY not in str(refs)+capsys.readouterr().out+caplog.text
    assert set(vars(model))=={'config','budget','transport','usage'}
    assert retain_check([tmp_path],[CANARY])['violations']==0


@pytest.mark.parametrize('failure',[False,True])
def test_review_handoff_clears_captured_packets_and_nested_rows_on_every_exit(failure):
    import pickle
    from lightgraph.discovery.observations import ReviewObservations
    captured=[];packet={'probe_digest':[{'probe_id':'probe_1'}]}
    def complete(task,p,response_model):
        captured.append(p)
        assert p['tool_observations'][0]['rows']==[['PRIVATE_QUERY_CANARY']]
        if failure:raise LightGraphError('model_failed')
        return SemanticReview(proposition=p['semantic_proposition'],decision='supported',basis='data_inference',
            evidence=[{'reference':'probe_1','stance':'supports'}])
    window=ReviewObservations()
    with window:
        window.replace([{'probe_id':'probe_1','columns':['name'],'rows':[['PRIVATE_QUERY_CANARY']]}])
        borrowed=window.values();nested=borrowed[0]['rows']
        with pytest.raises(LightGraphError,match='ephemeral_serialization_forbidden'):pickle.dumps(window)
        if failure:
            with pytest.raises(LightGraphError,match='model_failed'):
                review_semantics(SimpleNamespace(complete=complete),packet,{'predicate':'related_to'},[],observations=window)
        else:
            assert review_semantics(SimpleNamespace(complete=complete),packet,{'predicate':'related_to'},[],observations=window)
        assert all('tool_observations' not in p for p in captured)
    assert borrowed==[] and nested==[] and 'PRIVATE_QUERY_CANARY' not in str(packet)+repr(window)
    with pytest.raises(LightGraphError,match='observation_window_closed'):window.values()


@pytest.mark.parametrize('failure',[False,True])
def test_workflow_owns_and_closes_observations_without_mutating_shared_packets(monkeypatch,failure):
    from lightgraph.discovery import workflow
    marker='WORKFLOW_TRANSIENT_CANARY';packets=[];windows=[];borrowed=[]
    ask=workflow._ask;compile_rules=workflow._compile
    def observe_ask(model,packet,tools,observations):
        batch=ask(model,packet,tools,observations)
        packets.append(packet)
        assert marker not in str(packet)
        return batch
    def observe_compile(*args,**kwargs):
        observations=kwargs['observations'];windows.append(observations)
        borrowed.append(observations.values()[0]['rows'])
        assert marker in str(borrowed[-1])
        if failure:raise LightGraphError('semantic_claim_unresolved')
        return compile_rules(*args,**kwargs)
    monkeypatch.setattr(workflow,'_ask',observe_ask)
    monkeypatch.setattr(workflow,'_compile',observe_compile)
    def handler(request):
        payload,p=input_packet(request)
        if len(payload['messages'])==2:return response(call={'sql':'SELECT k, label FROM public.t'})
        return response(group(p,'k','label'))
    with MemorySource([table('t',['k','label'])],{'public.t':[('K',marker)]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert bool(result.rules) is not failure
    assert len(windows)==1 and borrowed==[[]]
    assert marker not in str(packets)+str(result)
    with pytest.raises(LightGraphError,match='observation_window_closed'):windows[0].values()


def test_metadata_only_reviewer_strips_transient_query_observations(monkeypatch):
    from lightgraph.tests.test_discovery_agent import CANARY
    from lightgraph.discovery.llm import StructuredModelClient,ModelConfig
    import httpx
    monkeypatch.setenv('TEST_REVIEW_KEY','offline-key')
    def handler(request):
        assert CANARY not in request.content.decode()
        _,p=input_packet(request)
        assert p['tool_observations']==[]
        return response({'proposition':p['semantic_proposition'],'decision':'supported',
            'basis':'data_inference','evidence':[{'reference':'c_a','stance':'supports'}]})
    model=StructuredModelClient(ModelConfig(base_url='https://test.invalid/v1',model='offline',
        api_key_env='TEST_REVIEW_KEY',allow_source_samples=False),transport=httpx.MockTransport(handler))
    packet={'metadata_digest':{'columns':[{'id':'c_a','name':'a'}]},
        'tool_observations':[{'probe_id':'probe_1','columns':['a'],'rows':[[CANARY]]}]}
    assert review_semantics(model,packet,{'predicate':'related_to'},[])


def test_fd_revision_starts_with_fresh_observations_and_full_check_evidence(monkeypatch):
    t=table('opaque',['x','y','realm']);calls=[]
    def handler(request):
        payload,p=input_packet(request);calls.append(p['task'])
        if p['task']=='T5_revision':
            assert p['tool_observations']==[]
            assert p['probe_digest']==[{'probe_id':'full_fd_check','operator':'check_fd',
                                      'aggregates':{'violating_groups':1}}]
            return response(group(p,'x','y',namespace='realm'))
        if len(payload['messages'])==2:
            return response(call={'sql':'SELECT x, y, realm FROM public.opaque'})
        assert p['tool_observations']
        return response(group(p,'x','y'))
    with MemorySource([t],{'public.opaque':[('K','First','A'),('K','Second','B')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert len(result.rules)==1 and result.rules[0].identity.key_columns==['realm','x']
    assert calls==['T0_discovery','T0_discovery','T5_revision']


@pytest.mark.parametrize('evidence,published',[
    (RuleEvidence(origin='discovery',semantic_basis=['nonempty']),False),
    (RuleEvidence(origin='discovery',semantic_basis=['issued'],semantic_decision='model_review',
        semantic_status='refuted',conditions_satisfied=True),False),
    (RuleEvidence(origin='discovery',semantic_basis=['issued'],semantic_decision='model_review',
        semantic_status='supported',conditions_satisfied=False),False),
    (RuleEvidence(origin='discovery',semantic_basis=['issued'],semantic_decision='model_review',
        semantic_status='supported',conditions_satisfied=True,refuting_refs=['counter']),False),
    (RuleEvidence(origin='discovery',semantic_basis=['issued'],semantic_decision='model_review',
        semantic_status='supported',conditions_satisfied=True),True),
    (RuleEvidence(semantic_basis=['user:explicit_fixed_rule']),True),
])
def test_rule_engine_requires_checked_discovery_semantics_but_keeps_fixed_rules(evidence,published):
    t=table('items',['tenant','code','definition'])
    rule=RuleSpec(rule_id='items',kind='column_group_entity',source=SourceRef(source_id='s',table='items'),
        scope=ScopeSpec(namespaces=['tenant']),identity=IdentitySpec(key_columns=['tenant','code'],
            semantic_resolution='verified_definition_registry',semantic_namespace='items'),
        attributes=AttributeSpec(single_valued={'definition':'definition'}),evidence=evidence)
    with GraphStore() as store,MemorySource([t],{'public.items':[('A','1','full definition')]}) as source:
        result=RuleEngine(store).execute(source,rule)
        assert result.published is published
        if not published:assert any(code.startswith('semantic_evidence_') for code in result.codes)




def test_revision_task_receives_goal_context_and_preserves_logical_rule(monkeypatch):
    t=table('items',['x','label','realm'])
    prior=RuleSpec(rule_id='stable-rule',kind='column_group_entity',
        source=SourceRef(source_id='s',table='items'),identity=IdentitySpec(key_columns=['x']),
        attributes=AttributeSpec(single_valued={'label':'label'}))
    seen=[]
    def handler(request):
        _,p=input_packet(request);seen.append(p)
        assert p['task']=='T5_revision'
        assert p['iteration_context']['goal_revision']==7
        return response(group(p,'x','label',namespace='realm'))
    with MemorySource([t],{'public.items':[('K','first','A'),('K','second','B')]}) as source:
        result=discover(source,model=client(monkeypatch,handler),revision_rules=[prior],
            iteration_context={'goal_revision':7,'codes':['fd_violation']})
    assert len(result.rules)==1 and result.rules[0].rule_id=='stable-rule'
    assert result.rules[0].revision==2
    assert result.rules[0].identity.key_columns==['realm','x']
    assert len(seen)==1

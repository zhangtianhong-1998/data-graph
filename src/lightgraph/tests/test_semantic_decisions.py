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


@pytest.mark.parametrize('text',[
    'mapping 中的指标不使用度量；两个编码仅为历史备份，不表示业务关系。',
    '假如指标使用度量，这两个字段可以作为关联；实际尚未确认。',
    '历史系统的指标使用度量；当前 mapping 保存的是作废关联，不再适用。',
    '其他项目中的指标使用度量，当前 mapping 不涉及该关系。',
])
def test_prose_never_promotes_overlap_to_accepted_relation(tmp_path,text):
    tables=[table('metrics',['metric_code','metric_name']),table('measures',['measure_code','measure_name']),
        table('mapping',['metric_code','measure_code'])]
    rows={'public.metrics':[('I','收入')],'public.measures':[('M','金额')],'public.mapping':[('I','M')]}
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    with MemorySource(tables,rows) as source:
        report=run_pipeline(cfg,[source],documents=[description('mapping',text)])
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert not [e for e in store.view({'public'})['edges'] if e['relation']=='uses_measure']
    assert report.counts['llm_calls']==0
    assert any(i['code'] in ('technical_overlap_semantics_unresolved','semantic_evidence_unresolved') for i in report.issues)


def test_negated_shared_definition_preserves_two_tenant_entities(tmp_path):
    t=table('items',['tenant_id','item_code','item_name','definition'])
    rows={'public.items':[('A','1','费用','同一完整定义'),('B','1','费用','同一完整定义')]}
    doc=description('items','同一完整定义在多个租户中并不共享，每个租户独立维护，不得跨租户合并。')
    with MemorySource([t],rows) as source:
        run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out'),
            goal=ExtractionGoal(entity_kinds=['BusinessConcept'])),[source],documents=[doc])
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert len([n for n in store.view({'A','B','public'})['nodes'] if n['layer']=='business'])==2
        assert store.get_rules()[0].identity.semantic_resolution=='source_scoped'


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
    model=SimpleNamespace(complete=lambda *args:SemanticReview.model_validate(review))
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


def test_agent_relation_proposal_is_refuted_by_independent_review(monkeypatch,tmp_path):
    tables=[table('metrics',['metric_code','name']),table('measures',['measure_code','name']),
        table('mapping',['left_ref','right_ref'])]
    calls=[]
    def handler(request):
        _,p=input_packet(request);calls.append(p['task']);name=p['scope']['table']
        if p['task']=='semantic_review':
            return response({'proposition':p['semantic_proposition'],'decision':'refuted',
                'basis':'document_interpretation','evidence':[
                    {'reference':document_ref(d),'stance':'refutes'} for d in p['document_snippets']]})
        if p['task']=='T0_discovery' and name in ('metrics','measures'):
            return response(group(p,'metric_code' if name=='metrics' else 'measure_code','name',
                'Metric' if name=='metrics' else 'Measure'))
        if p['task']=='T2_cross_table' and name=='mapping':
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            bindings={b['entity_kind']:b['rule_id'] for b in p['entity_bindings']}
            return response({'task':p['task'],'proposals':[{'kind':'reference','claim':{
                'source_entity':bindings['Metric'],'target_entity':bindings['Measure'],
                'source_columns':[ids['left_ref']],'target_columns':[ids['right_ref']],
                'relation_kind':'uses_measure'},'evidence_cited':[ids['left_ref']]}]})
        return response({'task':p['task'],'abstain':True})
    rows={'public.metrics':[('I','收入')],'public.measures':[('M','金额')],'public.mapping':[('I','M')]}
    with MemorySource(tables,rows) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],
            model=client(monkeypatch,handler),documents=[description('mapping','指标不使用度量；仅历史备份。')])
    assert 'semantic_review' in calls
    assert any(i['code']=='semantic_claim_refuted' for i in report.issues)
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert not [edge for edge in store.view({'public'})['edges'] if edge['relation']=='uses_measure']


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

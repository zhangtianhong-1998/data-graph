"""Exercise discovery decisions through SQL, rule validation and persisted graph output."""
import json
import httpx
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.llm import StructuredModelClient, ModelConfig
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline, _goal_violation
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.discovery.semantics import declaration_block, document_ref


def response(content=None, call=None):
    message={'content':json.dumps(content)} if content is not None else {'content':None,'tool_calls':[
        {'id':'provider_call','type':'function','function':{'name':'execute_readonly_sql','arguments':json.dumps(call)}}]}
    return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'stop' if content is not None else 'tool_calls'}],
        'usage':{'prompt_tokens':30,'completion_tokens':30}})


def client(monkeypatch, handler):
    monkeypatch.setenv('TEST_AGENT_KEY','offline-test-key')
    return StructuredModelClient(ModelConfig(base_url='https://test.example/v1',model='offline',
        api_key_env='TEST_AGENT_KEY',allow_source_samples=True),transport=httpx.MockTransport(handler))


def input_packet(request):
    payload=json.loads(request.content)
    return payload, json.loads(payload['messages'][1]['content'])['input']


def supported_semantic_review(packet):
    """Offline reviewer fixture; this verifies the contract, not model quality."""
    return {'proposition':packet['semantic_proposition'],'decision':'supported',
        'basis':'document_interpretation','evidence':[
            {'reference':document_ref(d),'stance':'supports'}
            for d in packet['document_snippets']], 'conditions':[]}


def table(name, columns):
    return TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in columns])


def group(packet, key, label, kind='ReportItem', namespace=None, extra=()):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    members=[{'column_id':ids[key],'role':'identity_code','in_identity':True},
             {'column_id':ids[label],'role':'label'}]
    if namespace:members.append({'column_id':ids[namespace],'role':'namespace'})
    members.extend({'column_id':ids[name],**role} for name,role in extra)
    return {'task':packet['task'],'proposals':[{'kind':'column_group',
        'claim':{'group_id':'local','entity_kind_hint':kind,'members':members},'evidence_cited':[ids[key]]}]}


def test_agent_reads_opaque_columns_and_emits_only_goal_values(monkeypatch,tmp_path):
    marker='SOURCE_OBSERVATION_CANARY_8712'
    t=table('demo',['field_a','sort_order','field_u','amount'])
    data={'public.demo':[('K1','销售费用','元',marker),('K1','销售费用','元',marker+'2')]}
    seen=[]
    def handler(request):
        payload,p=input_packet(request);seen.append(p['goal']['purpose'])
        if len(payload['messages'])==2:
            assert set(c['name'] for c in p['metadata_digest']['columns'])==set(t.column_names)
            return response(call={'sql':'SELECT field_a, sort_order, field_u, amount FROM public.demo','max_rows':4})
        observations=[json.loads(m['content']) for m in payload['messages'] if m['role']=='tool']
        assert observations[-1]['data']['rows'][0][1]=='销售费用'
        assert observations[-1]['data']['rows'][0][3]==marker
        return response(group(p,'field_a','sort_order',extra=[
            ('field_u',{'role':'semantic_attribute','property_name':'unit'}),
            ('amount',{'role':'fact_value','exclude_from_projection':True})]))
    model=client(monkeypatch,handler)
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'),
        goal=ExtractionGoal(purpose='只构建经营指标分析图，不输出观测金额'))
    with MemorySource([t],data) as source:
        first=run_pipeline(config,[source],model=model)
        assert first.rules_published and not first.rules_skipped
        assert model.usage['calls']==2
        second=run_pipeline(config,[source],model=model)
        assert second.counts['reused_rules']==1 and model.usage['calls']==2
        config.goal.purpose='保留指标的名称与计量单位'
        third=run_pipeline(config,[source],model=model)
        assert third.rules_published and model.usage['calls']==4
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        graph=store.view({'public'})
        business=[n for n in graph['nodes'] if n['layer']=='business']
        assert len(business)==1 and business[0]['label']=='销售费用'
        assert business[0]['properties']['unit']=='元'
        rule=store.get_rules()[0]
        assert 'amount' not in rule.identity.key_columns
        assert 'amount' not in rule.attributes.single_valued.values()
    assert retain_check([tmp_path/'out'],[marker])['violations']==0
    assert seen==['只构建经营指标分析图，不输出观测金额']*2+['保留指标的名称与计量单位']*2


def test_fd_counterexample_gets_fresh_tool_session_and_revised_key(monkeypatch):
    t=table('opaque',['x','y','realm'])
    tasks=[]
    def handler(request):
        payload,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='T0_discovery':return response(group(p,'x','y'))
        if len(payload['messages'])==2:
            assert p['known_conflicts'][0]['code']=='fd_violation'
            assert p['revision_candidate']['identity']['key_columns']==['x']
            assert p['revision_candidate']['attributes']['single_valued']=={'label':'y'}
            return response(call={'sql':'SELECT x, y, realm FROM public.opaque'})
        tool=json.loads(payload['messages'][-1]['content'])
        assert tool['status']=='ok' and len(tool['data']['rows'])==2
        return response(group(p,'x','y',namespace='realm'))
    model=client(monkeypatch,handler)
    with MemorySource([t],{'public.opaque':[('same','First','A'),('same','Second','B')]}) as source:
        result=discover(source,model=model)
    assert len(result.rules)==1
    assert result.rules[0].identity.key_columns==['realm','x']
    assert result.rules[0].scope.namespaces==['realm']
    assert tasks==['T0_discovery','T5_revision','T5_revision']
    assert model.budget.snapshot()['reserved']=={}


def test_cross_table_different_column_names_become_verified_rule(monkeypatch,tmp_path):
    tables=[table('metrics',['metric_pk','title']),table('measures',['measure_pk','caption']),
            table('mapping',['left_ref','right_ref'])]
    rows={'public.metrics':[('I1','销售收入')],'public.measures':[('M1','累计金额')],
          'public.mapping':[('I1','M1'),('I1','M1')]}
    calls=[]
    def handler(request):
        _,p=input_packet(request);name=p['scope']['table'];calls.append((p['task'],name))
        if p['task']=='semantic_review':return response(supported_semantic_review(p))
        if p['task']=='T0_discovery':
            if name=='metrics':return response(group(p,'metric_pk','title','Metric'))
            if name=='measures':return response(group(p,'measure_pk','caption','Measure'))
        if p['task']=='T2_cross_table' and name=='mapping':
            bindings={b['entity_kind']:b['rule_id'] for b in p['entity_bindings']}
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            return response({'task':p['task'],'proposals':[{'kind':'reference','claim':{
                'source_entity':bindings['Metric'],'target_entity':bindings['Measure'],
                'source_columns':[ids['left_ref']],'target_columns':[ids['right_ref']],
                'relation_kind':'uses_measure'},'evidence_cited':[ids['left_ref']]}]})
        return response({'task':p['task'],'abstain':True})
    model=client(monkeypatch,handler)
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.mapping]\n---\n# 关联\nmapping 记录指标使用度量的引用关系。')
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    with MemorySource(tables,rows) as source:
        report=run_pipeline(cfg,[source],model=model,documents=[doc])
    assert not report.rules_skipped
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        edges=[e for e in store.view({'public'})['edges'] if e['relation']=='uses_measure']
        assert len(edges)==1
        refs=[r for r in store.get_rules() if r.kind=='reference']
        assert len(refs)==1 and refs[0].relation.source_columns==['left_ref']
    assert ('T2_cross_table','mapping') in calls


def test_evidence_only_column_cannot_enter_output_even_if_model_says_label(monkeypatch):
    t=table('t',['k','aux'])
    model=client(monkeypatch,lambda request:response(group(input_packet(request)[1],'k','aux')))
    with MemorySource([t],{'public.t':[('K','source-value')]}) as source:
        result=discover(source,ExtractionGoal(evidence_only_columns=['aux']),model=model)
    assert len(result.rules)==1
    rule=result.rules[0]
    assert not rule.attributes.single_valued
    # The pipeline also enforces this for manually supplied/cached rules.
    rule.attributes.single_valued={'label':'aux'}
    assert _goal_violation(rule,ExtractionGoal(evidence_only_columns=['aux']),['k','aux'])=='evidence_only_column_projected'


def test_out_of_goal_entity_kind_is_not_silently_published(monkeypatch):
    t=table('t',['k','label'])
    model=client(monkeypatch,lambda request:response(group(input_packet(request)[1],'k','label','Measure')))
    with MemorySource([t],{'public.t':[('K','Name')]}) as source:
        result=discover(source,ExtractionGoal(entity_kinds=['Metric']),model=model)
    assert not result.rules and any(i['code']=='outside_extraction_goal' for i in result.issues)


def test_goal_change_with_no_replacement_retires_old_interpretation(monkeypatch,tmp_path):
    t=table('t',['k','label'])
    def handler(request):
        _,p=input_packet(request)
        if p['goal']['purpose']=='first':return response(group(p,'k','label'))
        return response({'task':p['task'],'abstain':True})
    model=client(monkeypatch,handler)
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'),goal=ExtractionGoal(purpose='first'))
    with MemorySource([t],{'public.t':[('K','Old interpretation')]}) as source:
        assert run_pipeline(cfg,[source],model=model).rules_published
        cfg.goal.purpose='second'
        report=run_pipeline(cfg,[source],model=model)
        assert not report.rules_published and report.counts['goal_invalidated_rules']==1
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert not store.get_rules()
        assert store.get_rules(include_stale=True)
        assert not [n for n in store.view({'public'})['nodes'] if n['layer']=='business']


def test_sibling_fact_role_cannot_be_reintroduced_as_label(monkeypatch):
    t=table('t',['k','label','amount'])
    def handler(request):
        _,p=input_packet(request)
        first=group(p,'k','label',extra=[('amount',{'role':'fact_value'})])
        second=group(p,'k','amount')
        first['proposals']+=second['proposals']
        return response(first)
    model=client(monkeypatch,handler)
    with MemorySource([t],{'public.t':[('K','Name','SHOULD_NOT_ESCAPE')]}) as source:
        result=discover(source,model=model)
    assert all('amount' not in r.attributes.single_valued.values() for r in result.rules)


def test_identity_only_exclusion_is_enforced(monkeypatch):
    t=table('t',['k','label'])
    def handler(request):
        _,p=input_packet(request);out=group(p,'k','label')
        cid=next(c['id'] for c in p['metadata_digest']['columns'] if c['name']=='k')
        out['proposals'].append({'kind':'exclusion','claim':{'exclusions':[
            {'column_id':cid,'reason':'out_of_goal','affects':['identity']}]},'evidence_cited':[cid]})
        return response(out)
    with MemorySource([t],{'public.t':[('K','Name')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert not result.rules
    assert any(i['code']=='excluded_column_in_identity' for i in result.issues)


@pytest.mark.parametrize('predicate',[
    {'amount':1}, {'not':{'column':'amount','op':'eq','value':1}},
    {'and':[{'column':'amount','op':'eq','value':1}]},
])
def test_query_exclusion_applies_to_rule_filter_not_only_projection(monkeypatch,predicate):
    from lightgraph.contracts import RuleSpec,IdentitySpec,SourceRef,ScopeSpec
    rule=RuleSpec(rule_id='r',kind='column_group_entity',source=SourceRef(source_id='s',table='t'),
        scope=ScopeSpec(predicate=predicate),identity=IdentitySpec(key_columns=['k']))
    assert _goal_violation(rule,ExtractionGoal(excluded_columns=['amount']),['k'])=='excluded_column_queried'
    assert _goal_violation(rule,ExtractionGoal(evidence_only_columns=['amount']),['k']) is None


def test_agent_long_hierarchy_with_opaque_parent_column(monkeypatch,tmp_path):
    t=table('tree',['a','b','c'])
    def handler(request):
        _,p=input_packet(request);ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
        if p['task']=='semantic_review':return response(supported_semantic_review(p))
        return response({'task':p['task'],'proposals':[{'kind':'long_hierarchy','claim':{
            'group_id':'tree','entity_kind_hint':'DimensionMember','key_column':ids['a'],
            'label_column':ids['b'],'parent_column':ids['c']},'evidence_cited':[ids['a']]}]})
    doc=load_document('tree.md',text='---\npurpose: data_description\napplies_to: [public.tree]\n---\n# 层级\ntree 的 c 指向父成员编码 a，根成员的 c 为空。')
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    with MemorySource([t],{'public.tree':[('root','根',None),('child','子','root')]}) as source:
        report=run_pipeline(cfg,[source],model=client(monkeypatch,handler),documents=[doc])
    assert report.rules_published and not report.rules_skipped
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert len([e for e in store.view({'public'})['edges'] if e['relation']=='parent_of'])==1


def test_agent_preserves_documented_shared_definition(monkeypatch,tmp_path):
    t=table('registry',['area','code_x','text_y','meaning_z'])
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='semantic_review':return response(supported_semantic_review(p))
        proposal=group(p,'code_x','text_y',namespace='area',extra=[
            ('meaning_z',{'role':'semantic_attribute','property_name':'definition'})])
        proposal['proposals'][0]['claim']['identity_resolution']='shared_definition'
        return response(proposal)
    doc=load_document('registry.md',text='---\npurpose: data_description\napplies_to: [public.registry]\n---\n# 定义\nregistry 中 meaning_z 是完整定义。同一完整定义可由多个租户引用。')
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'),scopes=['A','B','public'])
    data={'public.registry':[('A','1','费用','完整业务定义'),('B','2','费用','完整业务定义')]}
    with MemorySource([t],data) as source:
        report=run_pipeline(cfg,[source],model=client(monkeypatch,handler),documents=[doc])
    assert report.rules_published and not report.rules_skipped
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert store.get_rules()[0].identity.semantic_resolution=='verified_definition_registry'
        assert len([n for n in store.view({'A','B','public'})['nodes'] if n['layer']=='business'])==1


def test_agent_mode_keeps_documented_json_reference_verifier(monkeypatch):
    from lightgraph.tests.test_discovery_workflow import _complex_source
    from lightgraph.discovery import discover_references
    source,rules=_complex_source()
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，allowed_measure_codes 是允许引用度量的 JSON 数组。\n'+declaration_block(
        'public.links','uses_measure',['tenant_id','resource_code'],['tenant_id','allowed_measure_codes']))
    model=client(monkeypatch,lambda request:response({'task':input_packet(request)[1]['task'],'abstain':True}))
    with source:
        result=discover_references(source,rules,[doc],model=model)
    assert len(result.rules)==1
    assert result.rules[0].relation.target_json_array_columns==['allowed_measure_codes']

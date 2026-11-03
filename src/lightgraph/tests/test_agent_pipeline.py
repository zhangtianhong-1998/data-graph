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

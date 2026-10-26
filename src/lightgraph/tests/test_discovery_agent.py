"""Model-selected queries must affect later decisions without retaining source rows."""
import json

import httpx
import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.llm import ModelConfig, StructuredModelClient
from lightgraph.errors import BudgetExceeded, LightGraphError


CANARY='EPHEMERAL_SOURCE_VALUE_9271'


def packet():
    return {'task':'T1_table_discovery','goal':{'purpose':'识别经营指标及其维度，排除观测金额'},
        'metadata_digest':{'columns':[{'id':'c_a','name':'opaque_a'},
                                      {'id':'c_b','name':'opaque_b'}]},
        'strata_samples':[{'stratum':'s_1','count':1,'rows':{'c_a':CANARY}}]}


def input_packet():
    value=packet()
    value['strata_samples'][0]['rows']=[value['strata_samples'][0]['rows']]
    return value


def final(evidence='c_a', role='label'):
    return {'task':'T1_table_discovery','proposals':[{'kind':'column_group',
        'claim':{'group_id':'g','entity_kind_hint':'Metric','members':[
            {'column_id':'c_a','role':'identity_code','in_identity':True},
            {'column_id':'c_b','role':role}]},'evidence_cited':[evidence]}]}


def response(value=None, *, tool=None, args=None, raw_args=None):
    message={'content':json.dumps(value)}
    if tool is not None:
        message={'content':None,'tool_calls':[{'id':'provider-call','type':'function',
            'function':{'name':tool,'arguments':raw_args if raw_args is not None else json.dumps(args or {})}}]}
    return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'tool_calls' if tool else 'stop'}],
                                  'usage':{'prompt_tokens':30,'completion_tokens':20}})


class Toolbox:
    def __init__(self, outcome=None):
        self.outcome=outcome
        self.executed=[]
        self.closed=False

    def tool_specs(self):
        return [{'type':'function','function':{'name':name,'description':'Bounded test tool',
            'parameters':{'type':'object','additionalProperties':True}}}
            for name in ('get_metadata','execute_readonly_sql','profile_columns')]

    def execute(self,name,args):
        self.executed.append(name)
        if isinstance(self.outcome,Exception):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        if name=='get_metadata':
            return {'status':'ok','probe_id':'probe_0001','operator':name,
                    'data':{'tables':[{'columns':['opaque_a','opaque_b']}]}}
        return {'status':'ok','probe_id':'probe_0002','operator':name,
                'data':{'columns':['label'],'rows':[[CANARY]]},
                'aggregates':{'violating_groups':0},
                'coverage':{'result_complete':True,'source_scope':'query_result'}}

    def close(self):
        self.closed=True
        self.outcome=None


def client(monkeypatch,handler,*,samples=True,retries=1):
    monkeypatch.setenv('AGENT_TEST_KEY','synthetic-key')
    return StructuredModelClient(ModelConfig(base_url='https://model.invalid/v1',model='mock',
        api_key_env='AGENT_TEST_KEY',allow_source_samples=samples,retries=retries),
        BudgetAllocator(Cost(db_cu=100,llm_calls=32,llm_tokens=1000000,memory_bytes=1000000,seconds=60)),
        transport=httpx.MockTransport(handler))


def test_model_observes_tool_result_and_changes_next_query(monkeypatch,tmp_path,capsys,caplog):
    turns=[]
    def handler(request):
        body=json.loads(request.content)
        turns.append(len(body['messages']))
        assert '识别经营指标及其维度' in body['messages'][0]['content']
        assert 'Do not execute SQL' not in body['messages'][0]['content']
        assert body['tools'][0]['type']=='function'
        if len(turns)==1:
            return response(tool='get_metadata')
        observed=json.loads(body['messages'][-1]['content'])
        if len(turns)==2:
            assert observed['data']['tables'][0]['columns']==['opaque_a','opaque_b']
            return response(tool='execute_readonly_sql',args={'sql':'SELECT opaque_a, opaque_b FROM t LIMIT 8'})
        assert observed['data']['rows']==[[CANARY]]
        return response(final(evidence=observed['probe_id'],role='alias'))
    model=client(monkeypatch,handler)
    tools=Toolbox();data=input_packet()
    result=model.explore(data,tools)
    assert result.proposals[0].claim.members[1].role=='alias'
    assert turns==[2,4,6]
    assert tools.executed==['get_metadata','execute_readonly_sql'] and tools.closed
    assert data['probe_digest'][-1]['aggregates']=={'violating_groups':0}
    assert CANARY not in json.dumps(data['probe_digest'])
    assert 'tool_observations' not in data
    assert model.usage['calls']==3 and model.budget.snapshot()['reserved']=={}
    assert set(vars(model))=={'config','budget','transport','usage'}
    assert list(tmp_path.iterdir())==[]
    assert CANARY not in capsys.readouterr().out+caplog.text


def test_metadata_only_rejects_source_queries_before_executor_and_strips_samples(monkeypatch):
    turns=[]
    def handler(request):
        body=json.loads(request.content)
        assert CANARY not in request.content.decode()
        turns.append(1)
        if len(turns)==1:
            return response(tool='execute_readonly_sql',args={'sql':'SELECT opaque_a FROM t LIMIT 1'})
        assert json.loads(body['messages'][-1]['content'])['code']=='source_samples_disabled'
        return response(final())
    model=client(monkeypatch,handler,samples=False)
    tools=Toolbox()
    assert model.explore(input_packet(),tools).proposals
    assert not tools.executed and tools.closed


def test_query_observations_are_bounded_and_require_issued_probe():
    from lightgraph.discovery.agent import _record_probe,_record_observation
    from lightgraph.discovery.models import TaskPacket
    from lightgraph.contracts import canonical
    from pydantic import ValidationError
    p=input_packet();p['probe_digest']=[]
    for index in range(12):
        result={'status':'ok','probe_id':f'probe_{index:04d}',
            'data':{'columns':['a','b'],'rows':[['x'*200,'y'*200] for _ in range(20)]}}
        _record_probe(p,result,'execute_readonly_sql')
        _record_observation(p,result,'execute_readonly_sql')
    checked=TaskPacket.model_validate(p)
    assert 1<=len(checked.tool_observations)<=8
    assert all(len(o.rows)<=8 and o.truncated and not o.full_source_validation for o in checked.tool_observations)
    assert sum(len(canonical(o.model_dump()).encode()) for o in checked.tool_observations)<=24576
    p['tool_observations'][0]['probe_id']='unissued'
    with pytest.raises(ValidationError,match='unissued_observation_probe'):TaskPacket.model_validate(p)


@pytest.mark.parametrize('tool,raw_args,expected',[
    ('unknown_tool','{}','unknown_tool'),
    ('get_metadata','broken json','invalid_tool_arguments'),
    ('get_metadata','[]','invalid_tool_arguments')])
def test_bad_tool_request_becomes_safe_observation(monkeypatch,tool,raw_args,expected):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==1:
            return response(tool=tool,raw_args=raw_args)
        assert json.loads(body['messages'][-1]['content'])['code']==expected
        return response(final())
    tools=Toolbox();model=client(monkeypatch,handler)
    assert model.explore(input_packet(),tools).proposals
    assert not tools.executed and tools.closed


def test_tool_exception_message_not_exposed_to_model(monkeypatch):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==1:
            return response(tool='get_metadata')
        observation=body['messages'][-1]['content']
        assert CANARY not in observation
        assert json.loads(observation)['code']=='tool_execution_failed'
        return response(final())
    tools=Toolbox(RuntimeError(CANARY));model=client(monkeypatch,handler)
    assert model.explore(input_packet(),tools).proposals
    assert tools.closed


def test_tool_hard_budget_failure_propagates_without_next_model_turn(monkeypatch):
    model=client(monkeypatch,lambda request:response(tool='get_metadata'))
    tools=Toolbox(BudgetExceeded('db_cu_budget_exhausted'))
    with pytest.raises(BudgetExceeded,match='db_cu_budget_exhausted'):
        model.explore(input_packet(),tools)
    assert model.usage['calls']==1 and tools.closed


def test_duplicate_call_is_not_reexecuted_and_model_can_finish(monkeypatch):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)<3:
            return response(tool='get_metadata')
        assert json.loads(body['messages'][-1]['content'])['code']=='repeated_tool_call'
        return response(final(evidence='probe_0001'))
    tools=Toolbox();model=client(monkeypatch,handler)
    assert model.explore(input_packet(),tools).proposals
    assert tools.executed==['get_metadata'] and tools.closed


def test_tool_budget_requests_final_then_rejects_additional_call(monkeypatch):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==2:
            assert body['tool_choice']=='none'
        return response(tool='get_metadata')
    model=client(monkeypatch,handler);tools=Toolbox()
    with pytest.raises(BudgetExceeded,match='discovery_tool_calls_exhausted'):
        model.explore(input_packet(),tools,max_tool_calls=1)
    assert model.usage['calls']==2 and tools.executed==['get_metadata'] and tools.closed


def test_step_budget_stops_endless_exploration(monkeypatch):
    model=client(monkeypatch,lambda request:response(tool='get_metadata'))
    tools=Toolbox()
    with pytest.raises(BudgetExceeded,match='discovery_steps_exhausted'):
        model.explore(input_packet(),tools,max_steps=2)
    assert model.usage['calls']==2 and tools.closed


@pytest.mark.parametrize('evidence',['invented_probe','c_unauthorized'])
def test_final_reference_check_remains_required(monkeypatch,evidence):
    model=client(monkeypatch,lambda request:response(final(evidence=evidence)))
    tools=Toolbox()
    with pytest.raises(LightGraphError,match='dangling_evidence_reference'):
        model.explore(input_packet(),tools)
    assert tools.closed


def test_final_json_retry_and_invalid_packet_cleanup(monkeypatch):
    turns=[]
    def handler(request):
        turns.append(1)
        if len(turns)==1:
            return httpx.Response(200,json={'choices':[{'message':{'content':'bad'}}]})
        return response(final())
    model=client(monkeypatch,handler);tools=Toolbox()
    assert model.explore(input_packet(),tools).proposals
    assert model.usage['calls']==2 and tools.closed
    tools=Toolbox()
    with pytest.raises(LightGraphError,match='model_packet_schema_invalid'):
        model.explore({'task':'unknown'},tools)
    assert tools.closed


def test_no_tool_call_after_expired_deadline(monkeypatch):
    model=client(monkeypatch,lambda request:response(final()))
    model.budget.tighten_deadline(0)
    tools=Toolbox()
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        model.explore(input_packet(),tools)
    assert model.usage['calls']==0 and tools.closed

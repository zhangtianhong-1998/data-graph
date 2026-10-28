"""Exercise the installed AgentScope loop and Toolkit, not lookalike mocks."""
import asyncio
import gc
import json
from collections import OrderedDict

import httpx
import pytest
from agentscope import _config as framework_config
from agentscope.agent import AgentBase, ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.tool import Toolkit

from lightgraph.discovery.agentscope_runtime import DiscoveryChatModel, EphemeralMemory, run_agent
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import TaskPacket, ProposalBatch
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import CANARY, Toolbox, final, input_packet
from lightgraph.tests.test_model_streaming import model, streamed, wire


@pytest.fixture
def framework_spy(monkeypatch):
    """Wrap real framework entry points; execute their original implementations."""
    observed={'agents':[],'tool_names':[],'memory_messages':[]}
    original_reply=ReActAgent.reply
    original_tool=Toolkit.call_tool_function
    original_add=InMemoryMemory.add

    async def reply(self,*args,**kwargs):
        observed['agents'].append(self)
        return await original_reply(self,*args,**kwargs)

    async def call_tool(self,tool_call):
        observed['tool_names'].append(tool_call['name'])
        return await original_tool(self,tool_call)

    async def add(self,*args,**kwargs):
        await original_add(self,*args,**kwargs)
        observed['memory_messages'].extend(msg for msg,_marks in self.content)

    monkeypatch.setattr(ReActAgent,'reply',reply)
    monkeypatch.setattr(Toolkit,'call_tool_function',call_tool)
    monkeypatch.setattr(InMemoryMemory,'add',add)
    return observed


def tool_message(*names):
    return {'content':None,'tool_calls':[{'id':f'provider-{index}','type':'function',
        'function':{'name':name,'arguments':'{}'}} for index,name in enumerate(names)]}


def reply_for(message,stream):
    if stream:
        return streamed(wire(message))
    return httpx.Response(200,json={'choices':[{'message':message,
        'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop'}],
        'usage':{'prompt_tokens':30,'completion_tokens':20}})


def assert_cleared(observed):
    assert len(observed['agents'])==1
    agent=observed['agents'][0]
    assert isinstance(agent,ReActAgent) and isinstance(agent.toolkit,Toolkit)
    assert asyncio.run(agent.memory.get_memory())==[]
    assert not agent.memory._compressed_summary
    assert agent.toolkit.tools=={}
    assert agent.toolkit._argument_validators=={}
    assert agent.formatter.packet=={} and agent.formatter.executor is None
    assert not agent.sys_prompt and agent._terminal_failure is None
    assert all(msg.content==[] and msg.metadata is None
               for msg in observed['memory_messages'])
    return agent


@pytest.mark.parametrize('stream',[False,True])
def test_real_react_reply_and_toolkit_with_source_observation_cleanup(stream,framework_spy,capsys,caplog):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==1:
            message=tool_message('get_metadata')
        elif len(turns)==2:
            assert json.loads(body['messages'][-1]['content'])['probe_id']=='probe_0001'
            message=tool_message('execute_readonly_sql')
        else:
            assert json.loads(body['messages'][-1]['content'])['data']['rows']==[[CANARY]]
            message={'content':json.dumps(final(evidence='probe_0002'))}
        return reply_for(message,stream)
    client=model(handler,stream=stream,agent_name='configured-discovery')
    toolbox=Toolbox();packet=input_packet()
    result=client.explore(packet,toolbox)
    assert result.proposals
    assert framework_spy['tool_names']==['get_metadata','execute_readonly_sql']
    assert toolbox.executed==framework_spy['tool_names'] and toolbox.closed
    assert client.usage['calls']==3
    assert assert_cleared(framework_spy).name=='configured-discovery'
    assert CANARY not in json.dumps(packet['probe_digest'])
    assert CANARY not in capsys.readouterr().out+caplog.text


@pytest.mark.parametrize('stream',[False,True])
@pytest.mark.parametrize('failure',[BudgetExceeded,asyncio.CancelledError])
def test_batch_failure_drains_pending_actions_without_execution_or_extra_model(
        stream,failure,framework_spy,recwarn):
    class FailingTools(Toolbox):
        def execute(self,name,args):
            self.executed.append(name)
            raise failure('db_cu_budget_exhausted')
    toolbox=FailingTools()
    client=model(lambda request:reply_for(tool_message('get_metadata','profile_columns'),stream),stream=stream)
    with pytest.raises(failure):
        # A failed exploration action must not spend the reserved final turn.
        client.explore(input_packet(),toolbox,max_steps=2)
    assert toolbox.executed==['get_metadata'] and toolbox.closed
    assert framework_spy['tool_names']==['get_metadata'] and client.usage['calls']==1
    assert_cleared(framework_spy)
    gc.collect()
    assert not [w for w in recwarn if 'was never awaited' in str(w.message)]


def test_cancel_async_tool_cleans_runtime_and_does_not_start_second_tool(framework_spy,recwarn):
    async def scenario():
        entered=asyncio.Event()
        calls=[]
        async def dispatch(name,args):
            calls.append(name)
            entered.set()
            await asyncio.Event().wait()

        client=model(lambda request:reply_for(tool_message('get_metadata','profile_columns'),False),stream=False)
        packet=TaskPacket.model_validate(input_packet()).model_dump(mode='json',by_alias=True)
        task=asyncio.create_task(run_agent(client,packet,Toolbox(),dispatch,'system',max_steps=2,max_tool_calls=4))
        await asyncio.wait_for(entered.wait(),timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls==['get_metadata'] and client.usage['calls']==1

    asyncio.run(scenario())
    assert_cleared(framework_spy)
    gc.collect()
    assert not [w for w in recwarn if 'was never awaited' in str(w.message)]


def test_exception_after_tool_observation_drops_framework_memory(framework_spy,capsys,caplog):
    turns=[]
    def handler(request):
        turns.append(1)
        if len(turns)==1:
            return reply_for(tool_message('execute_readonly_sql'),False)
        raise httpx.ReadError(CANARY)
    toolbox=Toolbox();client=model(handler,stream=False)
    with pytest.raises(LightGraphError,match='model_transport_error') as error:
        client.explore(input_packet(),toolbox)
    assert toolbox.closed and toolbox.executed==['execute_readonly_sql']
    assert_cleared(framework_spy)
    assert CANARY not in str(error.value)+capsys.readouterr().out+caplog.text


def test_host_class_hooks_do_not_receive_discovery_messages(monkeypatch,framework_spy):
    captured=[]
    def hook(*args,**kwargs):
        captured.append((args,kwargs))
    for base in (AgentBase,ReActAgent):
        for name in ('reply','print','observe','reasoning','acting'):
            for when in ('pre','post'):
                attr=f'_class_{when}_{name}_hooks'
                if hasattr(base,attr):
                    monkeypatch.setattr(base,attr,OrderedDict({'host_persistence':hook}))
    turns=[]
    def handler(request):
        turns.append(1)
        message=tool_message('execute_readonly_sql') if len(turns)==1 else {'content':json.dumps(final())}
        return reply_for(message,False)
    assert model(handler,stream=False).explore(input_packet(),Toolbox()).proposals
    assert captured==[]
    assert_cleared(framework_spy)


def test_tracing_enabled_fails_before_framework_or_http(monkeypatch,framework_spy,tmp_path):
    monkeypatch.setattr(framework_config,'trace_enabled',True)
    requests=[]
    toolbox=Toolbox();client=model(lambda request:requests.append(request),stream=False)
    with pytest.raises(LightGraphError,match='agent_source_tracing_forbidden'):
        client.explore(input_packet(),toolbox)
    assert requests==[] and framework_spy['agents']==[] and toolbox.closed
    assert list(tmp_path.iterdir())==[]


def test_trace_policy_change_during_batch_stops_and_drains_remaining_actions(
        monkeypatch,framework_spy,recwarn):
    class PolicyChangingTools(Toolbox):
        def execute(self,name,args):
            result=super().execute(name,args)
            monkeypatch.setattr(framework_config,'trace_enabled',True)
            return result
    toolbox=PolicyChangingTools()
    client=model(lambda request:reply_for(tool_message(
        'get_metadata','profile_columns','execute_readonly_sql'),False),stream=False)
    with pytest.raises(LightGraphError,match='agent_source_tracing_forbidden'):
        client.explore(input_packet(),toolbox)
    assert toolbox.executed==['get_metadata'] and toolbox.closed
    assert framework_spy['tool_names']==['get_metadata'] and client.usage['calls']==1
    assert_cleared(framework_spy)
    gc.collect()
    assert not [w for w in recwarn if 'was never awaited' in str(w.message)]


def test_framework_agent_and_memory_cannot_be_serialized(framework_spy):
    client=model(lambda request:reply_for({'content':json.dumps(final())},False),stream=False)
    assert client.explore(input_packet(),Toolbox()).proposals
    agent=assert_cleared(framework_spy)
    for obj in (agent,agent.memory,EphemeralMemory()):
        with pytest.raises(LightGraphError,match='agent_source_persistence_forbidden'):
            obj.state_dict()
        with pytest.raises(LightGraphError,match='agent_source_persistence_forbidden'):
            obj.load_state_dict({})


def test_model_batch_above_tool_budget_submits_without_executing_batch(framework_spy):
    toolbox=Toolbox()
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(body)
        if len(turns)==1:
            return reply_for(tool_message('get_metadata','profile_columns'),False)
        assert 'tools' not in body
        feedback=json.loads(body['messages'][-1]['content'])['runtime_feedback']
        assert feedback=={'code':'tool_batch_exceeds_remaining_allowance','executed_tools':0,
            'requested_tools':2,'remaining_tool_calls':1,
            'next_action':'Submit ProposalBatch using existing evidence or abstain.'}
        return reply_for({'content':json.dumps(final())},False)
    client=model(handler,stream=False)
    assert client.explore(input_packet(),toolbox,max_tool_calls=1,max_steps=2).proposals
    assert client.usage['calls']==2
    assert toolbox.executed==[] and toolbox.closed
    assert framework_spy['tool_names']==[]
    assert_cleared(framework_spy)


@pytest.mark.parametrize('stream',[False,True])
def test_last_allowed_turn_submits_with_updated_runtime_counters(stream,framework_spy):
    seen=[]
    def handler(request):
        body=json.loads(request.content)
        packet=json.loads(next(m['content'] for m in body['messages'] if m['role']=='user'))
        seen.append(packet['runtime_control'])
        if len(seen)==1:
            assert 'tools' in body and body['tool_choice']=='auto'
            return reply_for(tool_message('get_metadata'),stream)
        assert 'tools' not in body
        assert json.loads(body['messages'][-1]['content'])['probe_id']=='probe_0001'
        return reply_for({'content':json.dumps(final(evidence='probe_0001'))},stream)
    client=model(handler,stream=stream);toolbox=Toolbox()
    assert client.explore(input_packet(),toolbox,max_steps=2,max_tool_calls=4).proposals
    assert seen==[
        {'turn':1,'max_turns':2,'remaining_turns_including_current':2,'remaining_tool_calls':4,
         'submission_required':False,'submission_reason':None},
        {'turn':2,'max_turns':2,'remaining_turns_including_current':1,'remaining_tool_calls':3,
         'submission_required':True,'submission_reason':'last_turn'}]
    assert client.usage['calls']==2 and toolbox.executed==['get_metadata']
    assert_cleared(framework_spy)


def test_nineteen_tools_then_oversized_pair_still_submits_within_turn_limit(framework_spy):
    class UniqueTools(Toolbox):
        def execute(self,name,args):
            self.executed.append(name)
            return {'status':'ok','operator':name,'probe_id':f'probe_{len(self.executed):04d}',
                    'aggregates':{'rows':1}}
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(body)
        if len(turns)==1:
            message=tool_message(*(['get_metadata']*19))
            for i,call in enumerate(message['tool_calls']):
                call['function']['arguments']=json.dumps({'index':i})
            return reply_for(message,False)
        if len(turns)==2:
            return reply_for(tool_message('profile_columns','execute_readonly_sql'),False)
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['turn']==3 and control['remaining_turns_including_current']==1
        assert control['remaining_tool_calls']==1 and control['submission_reason']=='tool_batch_rejected'
        assert 'tools' not in body
        return reply_for({'content':json.dumps(final(evidence='probe_0019'))},False)
    client=model(handler,stream=False);toolbox=UniqueTools()
    assert client.explore(input_packet(),toolbox,max_steps=3,max_tool_calls=20).proposals
    assert len(toolbox.executed)==19 and client.usage['calls']==3
    assert_cleared(framework_spy)


@pytest.mark.parametrize('respond_with_tool',[False,True])
def test_callers_none_is_honored_and_provider_cannot_override_it(respond_with_tool):
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        assert 'tools' not in body
        return reply_for(tool_message('get_metadata') if respond_with_tool
            else {'content':json.dumps(final())},False)
    client=model(handler,stream=False)
    adapter=DiscoveryChatModel(client,max_tool_calls=4,max_steps=4)
    messages=[{'role':'system','content':'test'},{'role':'user','content':json.dumps({
        'input':{'task':'T1_table_discovery'},'output_schema':ProposalBatch.model_json_schema()})}]
    call=adapter(messages,tools=Toolbox().tool_specs(),tool_choice='none')
    if respond_with_tool:
        with pytest.raises(LightGraphError,match='discovery_final_response_required'):
            asyncio.run(call)
    else:
        assert asyncio.run(call).content[0]['type']=='text'
    assert len(seen)==1 and adapter.total_calls==0 and messages==[]


def test_submission_recovery_does_not_exceed_global_model_allowance(framework_spy):
    client=model(lambda request:reply_for(tool_message('get_metadata','profile_columns'),False),stream=False)
    client.budget=BudgetAllocator(Cost(db_cu=100,llm_calls=1,llm_tokens=1000000,
                                      memory_bytes=1000000,seconds=60))
    toolbox=Toolbox()
    with pytest.raises(BudgetExceeded,match='llm_calls_budget_exhausted'):
        client.explore(input_packet(),toolbox,max_tool_calls=1,max_steps=2)
    assert client.usage['calls']==1 and toolbox.executed==[] and toolbox.closed
    assert_cleared(framework_spy)


def test_one_turn_task_is_submission_only_and_never_executes_provider_tool(framework_spy):
    def handler(request):
        assert 'tools' not in json.loads(request.content)
        return reply_for(tool_message('get_metadata'),False)
    client=model(handler,stream=False);toolbox=Toolbox()
    with pytest.raises(BudgetExceeded,match='discovery_steps_exhausted'):
        client.explore(input_packet(),toolbox,max_steps=1)
    assert client.usage['calls']==1 and toolbox.executed==[] and toolbox.closed
    assert_cleared(framework_spy)


def test_sync_entrypoint_in_running_loop_closes_executor_without_http():
    async def scenario():
        requests=[];toolbox=Toolbox()
        client=model(lambda request:requests.append(request),stream=False)
        with pytest.raises(LightGraphError,match='agent_sync_entrypoint_inside_event_loop'):
            client.explore(input_packet(),toolbox)
        assert toolbox.closed and requests==[]
    asyncio.run(scenario())

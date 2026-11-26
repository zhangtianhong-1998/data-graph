"""Exercise the public discovery loop with real tool dispatch and an offline model endpoint."""
import asyncio
import gc
import json

import httpx
import pytest
from lightgraph.discovery.model_runtime import DiscoveryChatModel, DiscoveryFormatter, run_agent
from lightgraph.discovery.tool_runtime import GuardedToolkit
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import TaskPacket, ProposalBatch
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import CANARY, Toolbox, final, input_packet
from lightgraph.tests.test_model_streaming import model, streamed, wire


@pytest.fixture
def discovery_run(monkeypatch):
    """Observe the real loop and check it released every reference to source data."""
    state = {'tool_names': [], 'formatters': [], 'histories': [], 'toolkits': [], 'models': []}
    original_format = DiscoveryFormatter.format
    original_call = GuardedToolkit.call
    original_turn = DiscoveryChatModel.next_turn

    def format_messages(formatter, history):
        state['formatters'].append(formatter)
        state['histories'].append(history)
        return original_format(formatter, history)

    async def call_tool(toolkit, name, arguments, **kwargs):
        state['tool_names'].append(name)
        state['toolkits'].append(toolkit)
        return await original_call(toolkit, name, arguments, **kwargs)

    async def run_turn(model, *args, **kwargs):
        state['models'].append(model)
        return await original_turn(model, *args, **kwargs)

    monkeypatch.setattr(DiscoveryFormatter, 'format', format_messages)
    monkeypatch.setattr(GuardedToolkit, 'call', call_tool)
    monkeypatch.setattr(DiscoveryChatModel, 'next_turn', run_turn)
    yield state
    assert state['histories'] and all(history == [] for history in state['histories'])
    assert all(formatter.packet == {} and formatter.executor is None for formatter in state['formatters'])
    assert all(toolkit.get_json_schemas() == [] and toolkit._execute is None for toolkit in state['toolkits'])


def tool_message(*names):
    return {'content':None,'tool_calls':[{'id':f'provider-{index}','type':'function',
        'function':{'name':name,'arguments':'{}'}} for index,name in enumerate(names)]}


def reply_for(message,stream):
    if stream:
        return streamed(wire(message))
    return httpx.Response(200,json={'choices':[{'message':message,
        'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop'}],
        'usage':{'prompt_tokens':30,'completion_tokens':20}})


@pytest.mark.parametrize('stream',[False,True])
def test_model_reply_and_tool_dispatch_release_source_observations(stream,discovery_run,capsys,caplog):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==1:
            message=tool_message('get_metadata')
        elif len(turns)==2:
            assert json.loads(body['messages'][-1]['content'])['probe_id']=='probe_0001'
            message=tool_message('execute_readonly_sql')
        else:
            data=json.loads(body['messages'][-1]['content'])['data']
            assert data['columns']==['label'] and data['row_format']=='positional_values'
            assert data['rows']==[[CANARY]]
            message={'content':json.dumps(final(evidence='probe_0002'))}
        return reply_for(message,stream)
    client=model(handler,stream=stream,agent_name='configured-discovery')
    toolbox=Toolbox();packet=input_packet()
    result=client.explore(packet,toolbox)
    assert result.proposals
    assert discovery_run['tool_names']==['get_metadata','execute_readonly_sql']
    assert toolbox.executed==discovery_run['tool_names'] and toolbox.closed
    assert client.usage['calls']==3
    assert CANARY not in json.dumps(packet['probe_digest'])
    assert CANARY not in capsys.readouterr().out+caplog.text


@pytest.mark.parametrize('stream',[False,True])
@pytest.mark.parametrize('failure',[BudgetExceeded,asyncio.CancelledError])
def test_batch_failure_drains_pending_actions_without_execution_or_extra_model(
        stream,failure,discovery_run,recwarn):
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
    assert discovery_run['tool_names']==['get_metadata'] and client.usage['calls']==1
    gc.collect()
    assert not [w for w in recwarn if 'was never awaited' in str(w.message)]


def test_cancel_async_tool_cleans_runtime_and_does_not_start_second_tool(discovery_run,recwarn):
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
    gc.collect()
    assert not [w for w in recwarn if 'was never awaited' in str(w.message)]


def test_transport_failure_after_observation_releases_source_data(discovery_run,capsys,caplog):
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
    assert CANARY not in str(error.value)+capsys.readouterr().out+caplog.text


def test_model_batch_above_tool_budget_submits_without_executing_batch(discovery_run):
    toolbox=Toolbox()
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(body)
        if len(turns)==1:
            return reply_for(tool_message('get_metadata','profile_columns'),False)
        assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in body.get('tools', []))
        feedback=json.loads(body['messages'][-1]['content'])['runtime_feedback']
        assert feedback=={'code':'tool_batch_exceeds_remaining_allowance','executed_tools':0,
            'requested_tools':2,'remaining_tool_calls':1,
            'next_action':'Finish retained drafts or abstain using the small final response.'}
        return reply_for({'content':json.dumps(final())},False)
    client=model(handler,stream=False)
    assert client.explore(input_packet(),toolbox,max_tool_calls=1,max_steps=2).proposals
    assert client.usage['calls']==2
    assert toolbox.executed==[] and toolbox.closed
    assert discovery_run['tool_names']==[]


@pytest.mark.parametrize('stream',[False,True])
def test_last_allowed_turn_submits_with_updated_runtime_counters(stream,discovery_run):
    seen=[]
    def handler(request):
        body=json.loads(request.content)
        packet=json.loads(next(m['content'] for m in body['messages'] if m['role']=='user'))
        seen.append(packet['runtime_control'])
        if len(seen)==1:
            assert 'tools' in body and body['tool_choice']=='auto'
            return reply_for(tool_message('get_metadata'),stream)
        assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in body.get('tools', []))
        assert json.loads(body['messages'][-1]['content'])['probe_id']=='probe_0001'
        return reply_for({'content':json.dumps(final(evidence='probe_0001'))},stream)
    client=model(handler,stream=stream);toolbox=Toolbox()
    assert client.explore(input_packet(),toolbox,max_steps=2,max_tool_calls=4).proposals
    assert seen==[
        {'turn':1,'max_turns':2,'remaining_turns_including_current':2,'remaining_tool_calls':4,'remaining_submission_calls':16,
         'submission_required':False,'submission_reason':None},
        {'turn':2,'max_turns':2,'remaining_turns_including_current':1,'remaining_tool_calls':3,'remaining_submission_calls':16,
         'submission_required':True,'submission_reason':'last_turn'}]
    assert client.usage['calls']==2 and toolbox.executed==['get_metadata']


def test_nineteen_tools_then_oversized_pair_still_submits_within_turn_limit(discovery_run):
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
        assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in body.get('tools', []))
        return reply_for({'content':json.dumps(final(evidence='probe_0019'))},False)
    client=model(handler,stream=False);toolbox=UniqueTools()
    assert client.explore(input_packet(),toolbox,max_steps=3,max_tool_calls=20).proposals
    assert len(toolbox.executed)==19 and client.usage['calls']==3


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
    call=adapter.next_turn(messages,tools=Toolbox().tool_specs(),tool_choice='none')
    if respond_with_tool:
        with pytest.raises(LightGraphError,match='discovery_final_response_required'):
            asyncio.run(call)
    else:
        assert asyncio.run(call)['final'].proposals
    assert len(seen)==1 and adapter.total_calls==0


def test_submission_recovery_does_not_exceed_global_model_allowance(discovery_run):
    client=model(lambda request:reply_for(tool_message('get_metadata','profile_columns'),False),stream=False)
    client.budget=BudgetAllocator(Cost(db_cu=100,llm_calls=1,llm_tokens=1000000,
                                      memory_bytes=1000000,seconds=60))
    toolbox=Toolbox()
    with pytest.raises(BudgetExceeded,match='llm_calls_budget_exhausted'):
        client.explore(input_packet(),toolbox,max_tool_calls=1,max_steps=2)
    assert client.usage['calls']==1 and toolbox.executed==[] and toolbox.closed


def test_one_turn_task_is_submission_only_and_never_executes_provider_tool(discovery_run):
    def handler(request):
        assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in json.loads(request.content).get('tools', []))
        return reply_for(tool_message('get_metadata'),False)
    client=model(handler,stream=False);toolbox=Toolbox()
    with pytest.raises(BudgetExceeded,match='discovery_steps_exhausted'):
        client.explore(input_packet(),toolbox,max_steps=1)
    assert client.usage['calls']==1 and toolbox.executed==[] and toolbox.closed


def test_sync_entrypoint_in_running_loop_closes_executor_without_http():
    async def scenario():
        requests=[];toolbox=Toolbox()
        client=model(lambda request:requests.append(request),stream=False)
        with pytest.raises(LightGraphError,match='agent_sync_entrypoint_inside_event_loop'):
            client.explore(input_packet(),toolbox)
        assert toolbox.closed and requests==[]
    asyncio.run(scenario())

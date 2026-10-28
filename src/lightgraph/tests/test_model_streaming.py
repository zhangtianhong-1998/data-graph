"""Real protocol-shaped offline responses; no model service or source data is used."""
import json

import httpx
import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.llm import ModelConfig, StructuredModelClient
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet


CANARY='STREAM_EPHEMERAL_秘密_2397'


class Chunks(httpx.SyncByteStream):
    def __init__(self, value, size=7, after=None):
        self.value=value
        self.size=size
        self.after=after

    def __iter__(self):
        for index in range(0,len(self.value),self.size):
            yield self.value[index:index+self.size]
        if self.after:
            self.after()


def event(value, newline='\n'):
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    return ('data: '+text+newline+newline).encode()


def delta(value=None, reason=None):
    return {'choices':[{'index':0,'delta':value or {},'finish_reason':reason}]}


def wire(message, *, reason=None, newline='\n', usage=True):
    """Fragment all actionable tool fields and Unicode content across SSE chunks."""
    values=[delta({'role':'assistant'})]
    if message.get('tool_calls'):
        for index,call in enumerate(message['tool_calls']):
            function=call['function']
            values.extend([
                delta({'tool_calls':[{'index':index,'id':call['id'][:3],'type':'function',
                    'function':{'name':function['name'][:4],'arguments':function['arguments'][:2]}}]}),
                delta({'tool_calls':[{'index':index,'id':call['id'][3:],
                    'function':{'name':function['name'][4:],'arguments':function['arguments'][2:]}}]})])
        reason=reason or 'tool_calls'
    else:
        content=message['content']
        values.extend(delta({'content':content[index:index+3]}) for index in range(0,len(content),3))
        reason=reason or 'stop'
    values.append(delta(reason=reason))
    if usage:
        values.append({'choices':[],'usage':{'prompt_tokens':30,'completion_tokens':20}})
    return b''.join(event(value,newline) for value in values)+event('[DONE]',newline)


def model(handler, *, stream=True, budget=None, **config):
    defaults={'base_url':'https://model.invalid/v1','model':'offline','api_key':'synthetic-secret',
        'retries':0,'stream':stream,'allow_source_samples':True}
    defaults.update(config)
    return StructuredModelClient(ModelConfig(**defaults),budget or BudgetAllocator(Cost(
        llm_calls=20,llm_tokens=1000000,memory_bytes=1000000,seconds=60)),
        transport=httpx.MockTransport(handler))


def streamed(value):
    return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks(value,size=1))


@pytest.mark.parametrize('stream',[False,True])
def test_modes_produce_same_multi_turn_agent_result_and_usage(stream,capsys,caplog,tmp_path):
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        assert body['stream'] is stream
        assert body.get('stream_options')==({'include_usage':True} if stream else None)
        if len(turns)==1:
            message={'content':None,'tool_calls':[{'id':'call-1','type':'function',
                'function':{'name':'get_metadata','arguments':'{}'}}]}
        else:
            assert json.loads(body['messages'][-1]['content'])['probe_id']=='probe_0001'
            message={'content':json.dumps(final(evidence='probe_0001'),ensure_ascii=False)}
        if stream:
            return streamed(wire(message,newline='\r\n'))
        return httpx.Response(200,json={'choices':[{'message':message,
            'finish_reason':'tool_calls' if len(turns)==1 else 'stop'}],
            'usage':{'prompt_tokens':30,'completion_tokens':20}})
    client=model(handler,stream=stream)
    toolbox=Toolbox()
    packet=input_packet()
    packet['strata_samples'][0]['rows'][0]['c_a']=CANARY
    result=client.explore(packet,toolbox)
    assert result==type(result).model_validate(final(evidence='probe_0001'))
    assert toolbox.executed==['get_metadata'] and toolbox.closed
    assert client.usage=={'calls':2,'input_tokens':60,'output_tokens':40,'failures':0}
    assert client.budget.used.llm_tokens==100 and client.budget.snapshot()['reserved']=={}
    assert set(vars(client))=={'config','budget','transport','usage'}
    assert not list(tmp_path.iterdir())
    assert CANARY not in capsys.readouterr().out+caplog.text


def test_fragmented_unicode_multiple_tool_indices_and_no_usage_option():
    message={'content':None,'tool_calls':[
        {'id':'call-one','type':'function','function':{'name':'get_metadata','arguments':'{}'}},
        {'id':'call-two','type':'function','function':{'name':'search_knowledge',
            'arguments':json.dumps({'query':CANARY},ensure_ascii=False)}}]}
    def handler(request):
        assert 'stream_options' not in json.loads(request.content)
        return streamed(wire(message,newline='\r',usage=False))
    client=model(handler,stream_options_include_usage=False,response_format='json_object')
    result=client._request([{'role':'user','content':'test'}],lambda value:value)
    assert result['tool_calls']==message['tool_calls']
    assert client.usage['output_tokens']==client.config.max_output_tokens


@pytest.mark.parametrize('suffix,code',[
    (b'','model_stream_incomplete'),
    (event('[DONE]'),'model_stream_incomplete'),
    (event(delta(reason='tool_calls')),'model_stream_incomplete'),
    (event(delta(reason='length'))+event('[DONE]'),'model_response_truncated'),
    (event(delta(reason='content_filter'))+event('[DONE]'),'model_response_refused'),
    (event(delta(reason='tool_calls'))+event('[DONE]')+event(delta()),'model_stream_invalid'),
    (event(delta(reason='tool_calls'))+event(delta({'content':'late'}))+event('[DONE]'),'model_stream_invalid'),
    (event('not-json '+CANARY),'llm_schema_invalid'),
])
def test_invalid_or_incomplete_stream_never_delivers_tool_request(suffix,code,capsys,caplog):
    start=event(delta({'tool_calls':[{'index':0,'id':'call','type':'function',
        'function':{'name':'get_metadata','arguments':'{}'}}]}))
    toolbox=Toolbox();client=model(lambda request:streamed(start+suffix))
    with pytest.raises(LightGraphError,match=code) as error:
        client.explore(input_packet(),toolbox)
    assert toolbox.executed==[] and toolbox.closed
    assert CANARY not in str(error.value)+repr(error.value)+capsys.readouterr().out+caplog.text
    assert client.budget.snapshot()['reserved']=={} and client.budget.used.llm_tokens>0


def test_socket_interruption_after_tool_json_cannot_execute_tool():
    value=wire({'content':None,'tool_calls':[{'id':'call','type':'function',
        'function':{'name':'get_metadata','arguments':'{}'}}]})
    def fail():
        raise httpx.ReadError(CANARY)
    client=model(lambda request:httpx.Response(200,stream=Chunks(value,after=fail)))
    toolbox=Toolbox()
    with pytest.raises(LightGraphError,match='model_transport_error') as error:
        client.explore(input_packet(),toolbox)
    assert toolbox.executed==[] and toolbox.closed and CANARY not in str(error.value)


def test_malformed_complete_tool_arguments_are_not_executed():
    turns=[]
    def handler(request):
        body=json.loads(request.content);turns.append(1)
        if len(turns)==1:
            message={'content':None,'tool_calls':[{'id':'call','type':'function',
                'function':{'name':'get_metadata','arguments':'{"bad":'}}]}
        else:
            assert json.loads(body['messages'][-1]['content'])['code']=='invalid_tool_arguments'
            message={'content':json.dumps(final())}
        return streamed(wire(message))
    toolbox=Toolbox();client=model(handler)
    assert client.explore(input_packet(),toolbox).proposals
    assert toolbox.executed==[] and toolbox.closed


def test_total_stream_limit_counts_ignored_comments_after_done():
    value=wire({'content':'{"ok":true}'})
    client=model(lambda request:streamed(value+b': '+CANARY.encode()*30+b'\n'),
                 max_response_bytes=len(value)+10,response_format='json_object')
    parsed=[]
    with pytest.raises(LightGraphError,match='model_response_too_large'):
        client._request([],lambda message:parsed.append(message))
    assert parsed==[] and client.budget.snapshot()['reserved']=={}


def test_stream_deadline_checks_each_chunk(monkeypatch):
    now=[100.0]
    monkeypatch.setattr('lightgraph.discovery.budget.time.monotonic',lambda:now[0])
    allocation=BudgetAllocator(Cost(llm_calls=10,llm_tokens=1000000,seconds=1))
    class SlowChunks(httpx.SyncByteStream):
        def __iter__(self):
            yield event(delta({'content':'{"ok":true}'}))
            now[0]+=2
            yield event(delta(reason='stop'))+event('[DONE]')
    client=model(lambda request:httpx.Response(200,stream=SlowChunks()),budget=allocation,
                 response_format='json_object')
    parsed=[]
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        client._request([],lambda message:parsed.append(message))
    assert parsed==[] and allocation.snapshot()['reserved']=={}


def test_schema_retry_preserves_stream_mode_and_charges_each_attempt():
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content)['stream'])
        return streamed(wire({'content':'not JSON '+CANARY}))
    client=model(handler,retries=1,response_format='json_object')
    with pytest.raises(LightGraphError,match='llm_schema_invalid') as error:
        client._request([],lambda message:json.loads(message['content']))
    assert requests==[True,True] and client.budget.used.llm_tokens==100
    assert CANARY not in str(error.value)


def test_explicit_key_is_excluded_from_serialization_and_empty_key_uses_env(monkeypatch):
    monkeypatch.setenv('KEY_TEST','environment-secret')
    seen=[]
    def handler(request):
        seen.append(request.headers['authorization'])
        return streamed(wire({'content':'{}'}))
    for configured in ('configured-secret',''):
        client=model(handler,api_key=configured,api_key_env='KEY_TEST',response_format='json_object')
        assert 'api_key' not in client.config.model_dump()
        assert 'configured-secret' not in repr(client.config)
        client._request([],lambda message:json.loads(message['content']))
    assert seen==['Bearer configured-secret','Bearer environment-secret']


def test_private_http_is_explicit_opt_in_and_never_allows_url_credentials():
    config={'base_url':'http://192.0.2.8:8000/v1'}
    with pytest.raises(LightGraphError,match='insecure_model_endpoint'):
        model(lambda request:None,**config)
    model(lambda request:None,**config,allow_insecure_http=True)
    for url in ('https://user:password@model.invalid/v1','https://model.invalid/v1?api_key=x',
                'https://model.invalid/v1#key','https:///v1'):
        with pytest.raises(LightGraphError,match='invalid_model_endpoint'):
            model(lambda request:None,base_url=url,allow_insecure_http=True)

"""Token windows work without a second implicit byte-sized context window."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.context import initial_agent_prompt_tokens
from lightgraph.discovery.prompt_window import fit_source_history, prompt_size
from lightgraph.discovery.workflow import _bounded_catalog_window
from lightgraph.runtime.config import ModelConfig, RunConfig
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_discovery_agent import input_packet, Toolbox
from lightgraph.tests.test_model_streaming import model, event, delta, Chunks
from lightgraph.tests.test_prompt_history_window import exchange


@pytest.mark.parametrize('stream',[False,True])
def test_large_valid_token_window_is_not_shrunk_by_old_byte_defaults(stream):
    messages=[{'role':'user','content':'context '*20000}]
    size,tokens=prompt_size(messages,None,'json_object')
    assert size>131072
    content='answer '*40000
    assert len(content.encode())>262144
    captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        if stream:
            body=event(delta({'role':'assistant','content':content}))+event(delta(reason='stop'))
            body+=event({'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':200}})+event('[DONE]')
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks(body,size=8192))
        return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':200}})
    client=model(handler,stream=stream,max_prompt_tokens=tokens,max_output_tokens=100000,response_format='json_object')
    assert client.config.max_prompt_bytes is None and client.config.max_response_bytes is None
    assert client._request(messages,lambda message:message['content'])==content
    assert captured[0]['max_tokens']==100000
    assert client.usage['input_tokens']==100 and client.usage['output_tokens']==200
    assert client.budget.used.llm_tokens==300  # Provider usage, never raw JSON bytes.


def test_token_only_history_pressure_keeps_complete_latest_exchange():
    messages=[{'role':'system','content':'policy'},*exchange(['older'],1000),*exchange(['latest'],1000)]
    token_limit=prompt_size(messages,None,'json_object')[1]-100
    fitted,removed=fit_source_history(messages,None,'json_object',None,set(),token_limit=token_limit)
    assert removed==1
    assert [m['tool_call_id'] for m in fitted if m['role']=='tool']==['latest']
    assert prompt_size(fitted,None,'json_object')[1]<=token_limit


def test_catalog_preflight_uses_tokens_when_byte_guard_is_disabled():
    primary=table('main',['id','name']);other=table('other',['id','name'])
    other.columns[1].comment='Optional metadata. '*1000
    config=SimpleNamespace(max_prompt_bytes=None,max_prompt_tokens=None,max_output_tokens=1000,
        response_format='json_schema',max_discovery_steps=8,max_tool_calls=16)
    client=SimpleNamespace(config=config)
    selected,full=_bounded_catalog_window(primary,[primary,other],input_packet(),client,Toolbox().tool_specs())
    assert selected==[primary,other]
    config.max_prompt_tokens=initial_agent_prompt_tokens(full,Toolbox().tool_specs())
    selected,_=_bounded_catalog_window(primary,[primary,other],input_packet(),client,Toolbox().tool_specs())
    assert selected==[primary]  # Leave token headroom for tool replies.


def test_optional_byte_guards_roundtrip_and_trace_retention_default():
    config=RunConfig(sources=[],model={'max_prompt_tokens':1048576,'max_output_tokens':65536,'timeout_seconds':300})
    loaded=RunConfig.model_validate_json(config.model_dump_json())
    assert loaded.record_trace is True
    assert loaded.model.max_prompt_bytes is None and loaded.model.max_response_bytes is None
    assert loaded.model.max_prompt_tokens==1048576 and loaded.model.max_output_tokens==65536
    assert loaded.model.timeout_seconds==300
    # Explicit historical transport guards still mean bytes and remain editable.
    legacy=ModelConfig(max_prompt_bytes=131072,max_response_bytes=262144)
    assert ModelConfig.model_validate_json(legacy.model_dump_json()).max_prompt_bytes==131072

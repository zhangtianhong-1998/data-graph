"""Context windows, HTTP byte caps and cumulative spend use distinct units."""
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from lightgraph.contracts import canonical
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.prompt_window import fit_source_history, prompt_size
from lightgraph.discovery.token_estimation import estimate_tokens, prompt_token_limit
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import make_model
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_model_streaming import model
from lightgraph.tests.test_prompt_history_window import exchange


def test_estimation_distinguishes_ascii_unicode_and_structured_punctuation():
    assert estimate_tokens('a'*4000)==1000
    assert estimate_tokens('中'*4000)==6000
    assert estimate_tokens('{}'*2000)==4000
    assert estimate_tokens('a中') > estimate_tokens('ab')
    config=SimpleNamespace(max_prompt_tokens=8000,context_window_tokens=10000,max_output_tokens=3000)
    assert prompt_token_limit(config)==7000


@pytest.mark.parametrize('constraint',['input','combined','bytes'])
def test_actual_envelope_includes_tool_schema_and_reserves_output(constraint):
    messages=[{'role':'user','content':'test'}]
    tools=[{'type':'function','function':{'name':'find','description':'abc '*100,
        'parameters':{'type':'object','properties':{}}}}]
    size,tokens=prompt_size(messages,tools,'json_schema')
    assert tokens>prompt_size(messages,None,'json_object')[1]
    seen=[]
    def handler(request):
        seen.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':17,'completion_tokens':3}})
    settings={'max_prompt_bytes':size,'max_output_tokens':32}
    if constraint=='input':settings['max_prompt_tokens']=tokens
    elif constraint=='combined':settings['context_window_tokens']=tokens+32
    client=model(handler,stream=False,**settings)
    assert client._request(messages,lambda message:message['content'],tools)=='ok'
    assert client.usage['input_tokens']==17 and client.budget.used.llm_tokens==20
    assert not client.usage.get('estimated_input_tokens_calls')
    field={'input':'max_prompt_tokens','combined':'context_window_tokens','bytes':'max_prompt_bytes'}[constraint]
    setattr(client.config,field,getattr(client.config,field)-1)
    code='model_packet_too_large' if constraint=='bytes' else 'model_context_window_exceeded'
    with pytest.raises(LightGraphError,match=code):
        client._request(messages,lambda message:message,tools)
    assert len(seen)==1 and client.budget.snapshot()['reserved']=={}


def test_request_budget_reservation_uses_estimated_tokens_not_bytes():
    messages=[{'role':'user','content':'english '*1000}]
    size,tokens=prompt_size(messages,None,'json_object')
    assert tokens < size/2
    def handler(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':tokens,'completion_tokens':2}})
    client=model(handler,stream=False,max_output_tokens=8,response_format='json_object',
        budget=BudgetAllocator(Cost(llm_calls=1,llm_tokens=tokens+8,seconds=60)))
    assert client._request(messages,lambda message:message['content'])=='ok'
    assert client.budget.used.llm_tokens==tokens+2


def test_token_pressure_keeps_tool_pairs_and_structural_memory():
    knowledge={'schema_knowledge':{'object_models':[{'positions':[1,2,3]}]},
        'issued_refs':['probe_old','probe_latest']}
    messages=[{'role':'system','content':'policy'},{'role':'user','content':canonical(knowledge)},
        *exchange(['old_a','old_b'],1000),*exchange(['latest'],1000)]
    byte_limit=100000
    token_limit=prompt_size(messages,None,'json_object')[1]-400
    fitted,removed=fit_source_history(messages,None,'json_object',byte_limit,set(),token_limit=token_limit)
    assert removed==1
    assert prompt_size(fitted,None,'json_object')[1]<=token_limit
    assert fitted[1]==messages[1]
    assert [m['tool_call_id'] for m in fitted if m['role']=='tool']==['latest']


def test_config_roundtrip_preserves_explicit_windows_and_legacy_byte_unit():
    config=RunConfig(sources=[],model={'mode':'approved_external','model':'offline',
        'base_url':'https://example.invalid/v1','max_calls':240,'max_tokens':None,
        'max_prompt_bytes':1048576,'max_prompt_tokens':1048576,
        'context_window_tokens':1114112,'max_output_tokens':65536})
    client=make_model(RunConfig.model_validate_json(config.model_dump_json()))
    assert client.config.max_prompt_bytes==1048576
    assert prompt_token_limit(client.config)==1048576
    assert client.config.max_output_tokens==65536
    assert client.budget.limits.llm_tokens is None
    legacy=RunConfig(sources=[],model={'max_prompt_bytes':1048576,'max_output_tokens':65536})
    assert legacy.model.max_prompt_tokens is None and legacy.model.context_window_tokens is None
    with pytest.raises(ValidationError,match='context_window_must_exceed_output_reservation'):
        RunConfig(sources=[],model={'context_window_tokens':4096,'max_output_tokens':4096})

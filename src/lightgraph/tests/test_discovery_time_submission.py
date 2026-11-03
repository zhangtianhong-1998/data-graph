"""Slow exploration must leave an existing-budget path to submit findings."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.tests.test_discovery_agent import Toolbox, input_packet, response, final
from lightgraph.tests.test_model_streaming import model


@pytest.fixture
def clock(monkeypatch):
    now=[1000.0]
    for module in ('lightgraph.discovery.budget','lightgraph.discovery.llm',
                   'lightgraph.discovery.agentscope_runtime'):
        monkeypatch.setattr(module+'.time',SimpleNamespace(monotonic=lambda:now[0]))
    return now


def proposal():
    return {k:v for k,v in final()['proposals'][0].items() if k!='kind'}


def test_observed_call_time_opens_submission_before_last_two_steps(clock):
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        if len(seen)==1:
            clock[0]+=40
            return response(tool='get_metadata')
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['submission_reason']=='time_submission_window'
        assert control['turn']<15
        assert control['remaining_seconds']<=control['submission_reserve_seconds']==80
        assert not {'get_metadata','execute_readonly_sql','profile_columns'} & {
            t['function']['name'] for t in body['tools']}
        clock[0]+=10
        return (response(tool='submit_column_group',args=proposal()) if len(seen)==2
                else response(tool='finish_discovery',args={'pending':False}))
    budget=BudgetAllocator(Cost(llm_calls=3,llm_tokens=None,seconds=100))
    client=model(handler,stream=False,budget=budget,max_discovery_steps=16)
    source=Toolbox();batch=client.explore(input_packet(),source)
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert source.executed==['get_metadata']
    assert client.usage['calls']==3 and client.usage['time_submission_windows']==1
    assert budget.remaining_seconds()==40 and not budget.snapshot()['reserved']


def test_600_second_timeout_uses_remaining_time_for_submission_not_same_request(clock):
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        if len(requests)==1:
            clock[0]+=138.5
            return response(tool='get_metadata')
        if len(requests)==2:
            clock[0]+=600
            raise httpx.ReadTimeout('synthetic timeout',request=request)
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['submission_reason']=='request_timeout_submission'
        assert not {'get_metadata','execute_readonly_sql','profile_columns'} & {
            t['function']['name'] for t in body['tools']}
        clock[0]+=10
        return (response(tool='submit_column_group',args=proposal()) if len(requests)==3
                else response(tool='finish_discovery',args={'pending':False}))
    budget=BudgetAllocator(Cost(llm_calls=4,llm_tokens=None,seconds=877.5))
    client=model(handler,stream=False,budget=budget,timeout_seconds=600,retries=1,max_discovery_steps=16)
    source=Toolbox();batch=client.explore(input_packet(),source)
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert len(requests)==client.usage['calls']==4 and client.usage['failures']==1
    assert client.usage['timeout_submission_attempts']==1
    assert client.usage['request_retry_skipped']==1
    assert client.usage.get('request_retry_attempts',0)==0
    assert source.executed==['get_metadata']
    assert budget.remaining_seconds()==119 and not budget.snapshot()['reserved']


def test_fresh_fast_task_keeps_source_allowance(clock):
    bodies=[]
    def handler(request):
        body=json.loads(request.content);bodies.append(body)
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['submission_reason'] is None
        clock[0]+=1
        if len(bodies)<3:return response(tool='get_metadata',args={'page':len(bodies)})
        return response(tool='finish_discovery',args={'pending':False})
    client=model(handler,stream=False,budget=BudgetAllocator(Cost(llm_calls=3,llm_tokens=None,seconds=100)))
    source=Toolbox();client.explore(input_packet(),source)
    assert source.executed==['get_metadata','get_metadata']
    assert not client.usage.get('time_submission_windows')

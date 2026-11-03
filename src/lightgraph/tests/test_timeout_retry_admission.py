"""A short remaining phase must not replay a much longer read-timeout request."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.llm import ModelConfig, StructuredModelClient
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.trace import RunTrace, bind_trace


@pytest.fixture
def clock(monkeypatch):
    now=[100.0]
    timer=SimpleNamespace(monotonic=lambda:now[0])
    monkeypatch.setattr('lightgraph.discovery.llm.time',timer)
    monkeypatch.setattr('lightgraph.discovery.budget.time',timer)
    return now


def envelope(content='{"answer":"ok"}'):
    return {'choices':[{'message':{'content':content},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5}}


def make_client(clock,handler,*,remaining=738.05,retries=1):
    budget=BudgetAllocator(Cost(llm_calls=5,llm_tokens=None,seconds=1800))
    # Match the object phase: 1800 * .75 * .65 = 877.5 seconds. This request
    # starts after earlier calls; its 600.05-second failure leaves 138 seconds.
    phase_span=max(877.5,remaining)
    budget.set_phase_deadline(clock[0]+phase_span)
    clock[0]+=phase_span-remaining
    config=ModelConfig(base_url='https://offline.invalid/v1',model='offline',
        api_key='synthetic-private-key',timeout_seconds=600,max_output_tokens=64,
        retries=retries,stream=False,response_format='json_object')
    return StructuredModelClient(config,budget,transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('wrapped_by_sdk',[False,True])
@pytest.mark.parametrize('semantic_review',[False,True])
def test_long_read_timeout_keeps_remaining_phase_for_caller_instead_of_short_replay(
        clock,monkeypatch,tmp_path,wrapped_by_sdk,semantic_review):
    requests=[]
    def failed(request):
        requests.append(request)
        clock[0]+=600.05
        raise httpx.ReadTimeout('PRIVATE_TRANSPORT_DETAIL')
    client=make_client(clock,failed)
    if not wrapped_by_sdk:
        monkeypatch.setattr('lightgraph.discovery.llm.request_completion',
                            lambda *args:failed(args[-1]))
    path=tmp_path/'retry.trace.jsonl'
    with bind_trace(RunTrace(path)),pytest.raises(LightGraphError) as caught:
        client._request([{'role':'user','content':'bounded task'}],
                        lambda message:message,semantic_review=semantic_review,allow_schema_repair=False)
    error=caught.value
    assert error.code=='model_transport_error' and not isinstance(error,BudgetExceeded)
    assert error.failure_kind=='read_timeout' and error.retry_suppressed
    assert error.retry_skip_reason=='insufficient_time_for_same_request'
    assert len(requests)==client.usage['calls']==client.usage['failures']==1
    assert client.usage['transport_read_timeout']==1
    assert client.usage['request_retry_skipped']==client.usage['request_retry_skipped_insufficient_time']==1
    assert not client.usage.get('request_retry_attempts') and not client.usage.get('request_retry_exhausted')
    assert client.budget.remaining_seconds()==pytest.approx(138)
    assert client.budget.used.llm_calls==1 and client.budget.used.llm_tokens>0
    assert client.usage['unknown_usage_reserved_tokens']==client.budget.used.llm_tokens
    assert client.budget.snapshot()['reserved']=={}
    raw=path.read_text(encoding='utf-8')
    events=[json.loads(line) for line in raw.splitlines()]
    assert sum(e['kind']=='model_request' for e in events)==1
    skipped=[e['data'] for e in events if e['kind']=='model_retry_skipped']
    assert len(skipped)==1 and skipped[0]['attempt']==2
    assert skipped[0]['failed_attempt_elapsed_seconds']==pytest.approx(600.05)
    assert skipped[0]['effective_retry_timeout_seconds']==pytest.approx(138)
    assert 'PRIVATE_TRANSPORT_DETAIL' not in raw and 'synthetic-private-key' not in raw


@pytest.mark.parametrize('failed_elapsed',[600.05,700.0])
def test_same_idle_timeout_can_retry_when_phase_covers_overhead_or_a_longer_stream(clock,failed_elapsed):
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)==1:
            # A stream may deliver chunks before its idle read later times out;
            # its total duration is not bounded by the 600-second idle setting.
            clock[0]+=failed_elapsed
            raise httpx.ReadTimeout('slow response')
        clock[0]+=2
        return httpx.Response(200,json=envelope())
    client=make_client(clock,handler,remaining=1500)
    assert client._request([{'role':'user','content':'task'}],json_result)=={'answer':'ok'}
    assert len(requests)==2 and client.usage['request_retry_succeeded']==1
    assert not client.usage.get('request_retry_skipped')
    assert client.budget.snapshot()['reserved']=={}


def json_result(message):
    return json.loads(message['content'])


@pytest.mark.parametrize('failure',['connection','malformed_json'])
def test_short_remaining_phase_does_not_block_other_request_failure_retries(clock,failure):
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)==1:
            clock[0]+=600.05
            if failure=='connection':raise httpx.ConnectError('private network error')
            return httpx.Response(200,json=envelope('not json'))
        clock[0]+=2
        return httpx.Response(200,json=envelope())
    client=make_client(clock,handler)
    assert client._request([{'role':'user','content':'task'}],json_result)=={'answer':'ok'}
    assert len(requests)==2 and client.usage['request_retry_succeeded']==1
    assert not client.usage.get('request_retry_skipped')
    assert client.budget.snapshot()['reserved']=={}


def test_slow_complete_response_keeps_schema_correction_within_remaining_phase(clock):
    from lightgraph.discovery.models import ProposalBatch
    from lightgraph.tests.test_discovery_agent import input_packet
    from lightgraph.tests.test_schema_protocol_correction import malformed_hierarchy, corrected_hierarchy
    original=malformed_hierarchy()
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests)==1:
            clock[0]+=600.05
            return httpx.Response(200,json=envelope(json.dumps(original)))
        assert 'schema_protocol_correction' in requests[-1]['messages'][-1]['content']
        clock[0]+=2
        return httpx.Response(200,json=envelope(json.dumps(corrected_hierarchy(original))))
    client=make_client(clock,handler)
    result=client.complete('T1_table_discovery',input_packet(),ProposalBatch)
    assert result==ProposalBatch.model_validate(corrected_hierarchy(original))
    assert len(requests)==2 and client.usage['schema_repairs_succeeded']==1
    assert not client.usage.get('request_retry_skipped')
    assert client.budget.snapshot()['reserved']=={}


def test_disabled_retry_is_not_reported_as_suppressed_and_expired_budget_still_expires(clock):
    def handler(request):
        clock[0]+=600.05
        raise httpx.ReadTimeout('private timeout')
    client=make_client(clock,handler,retries=0)
    with pytest.raises(LightGraphError) as caught:
        client._request([{'role':'user','content':'task'}],json_result)
    assert caught.value.code=='model_transport_error'
    assert not getattr(caught.value,'retry_suppressed',False)
    assert not client.usage.get('request_retry_skipped')
    assert client.usage['request_retry_exhausted']==1
    assert client.budget.snapshot()['reserved']=={}
    expired=make_client(clock,handler,remaining=500)
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        expired._request([{'role':'user','content':'task'}],json_result)
    assert not expired.usage.get('request_retry_skipped')
    assert expired.budget.snapshot()['reserved']=={}

"""A late provider response must never become a publishable extraction result."""
import json
import httpx
import pytest
from lightgraph.contracts import StrictModel
from lightgraph.discovery import BudgetAllocator,Cost
from lightgraph.discovery.llm import ModelConfig,StructuredModelClient
from lightgraph.documents import ClaimBatch,extract_with_cache,load_document
from lightgraph.errors import BudgetExceeded


@pytest.fixture
def clock(monkeypatch):
    now=[100.0]
    monkeypatch.setattr('lightgraph.discovery.budget.time.monotonic',lambda:now[0])
    return now


def budget(seconds=0.02):
    return BudgetAllocator(Cost(db_cu=10,llm_calls=10,llm_tokens=1000000,
                                memory_bytes=1000000,seconds=seconds))


def model(monkeypatch,allocation,handler):
    monkeypatch.setenv('DEADLINE_TEST_KEY','synthetic-key')
    return StructuredModelClient(ModelConfig(base_url='https://model.invalid/v1',model='synthetic',
        api_key_env='DEADLINE_TEST_KEY',timeout_seconds=45,retries=1),allocation,
        transport=httpx.MockTransport(handler))


def envelope(value):
    return {'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}


def test_charge_after_deadline_records_actual_cost_and_releases_reservation(clock):
    allocation=budget();allocation.reserve('probe',Cost(db_cu=2))
    clock[0]+=0.08
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        allocation.charge(Cost(db_cu=1),'probe')
    assert allocation.used.db_cu==1
    assert allocation.snapshot()['reserved']=={}


def test_external_deadline_can_only_tighten_and_expiry_stops_admission(clock):
    allocation=budget(10)
    assert allocation.tighten_deadline(102.0)==102.0
    assert allocation.tighten_deadline(109.0)==102.0
    assert allocation.deadline_monotonic==102.0
    assert allocation.remaining_seconds()==2
    clock[0]=102.0
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        allocation.admit(Cost(llm_calls=1))
    for value in (float('inf'),float('nan')):
        with pytest.raises(ValueError,match='invalid_budget_deadline'):
            allocation.tighten_deadline(value)


def test_slow_mock_response_cannot_return_claims_and_timeout_is_remaining_budget(monkeypatch,clock):
    requests=[];allocation=budget()
    document=load_document('analysis.md',text='---\npurpose: analysis_knowledge\n---\n# 分析\n价格可能影响收入。')
    def handler(request):
        requests.append(request)
        # Reproduce 0.02 s budget / 0.08 s transport without wall-clock flakiness.
        clock[0]+=0.08
        return httpx.Response(200,json=envelope({'claims':[{'section_id':document.sections[0].section_id,
            'subject':'价格','predicate':'may_affect','object':'收入','modality':'possible',
            'evidence_text':'价格可能影响收入'}]}))
    client=model(monkeypatch,allocation,handler)
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        extract_with_cache(document,client)
    assert len(requests)==1 and client.usage['calls']==1
    assert all(0 < limit <= 0.02 for limit in requests[0].extensions['timeout'].values())
    assert allocation.used.llm_tokens>0 and allocation.snapshot()['reserved']=={}


def test_slow_chunk_fails_total_deadline_even_when_each_io_completes(monkeypatch,clock):
    allocation=budget();requests=[]
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{'
            clock[0]+=0.08
            yield b'}'
    def handler(request):
        requests.append(request)
        return httpx.Response(200,stream=Chunks())
    client=model(monkeypatch,allocation,handler)
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        client.complete('T4_document',{},ClaimBatch)
    assert len(requests)==1 and allocation.used.llm_tokens>0
    assert allocation.snapshot()['reserved']=={}


def test_expired_retryable_error_is_not_retried(monkeypatch,clock):
    allocation=budget();requests=[]
    def handler(request):
        requests.append(request);clock[0]+=0.08
        return httpx.Response(429,text='unretained-provider-body')
    client=model(monkeypatch,allocation,handler)
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        client.complete('T4_document',{},ClaimBatch)
    assert len(requests)==1 and allocation.used.llm_calls==1
    assert allocation.snapshot()['reserved']=={}


def test_deadline_is_checked_after_schema_validation_before_result_return(monkeypatch,clock):
    allocation=budget()
    class SlowSchema(StrictModel):
        ok: bool
        @classmethod
        def model_validate(cls,value,*args,**kwargs):
            result=super().model_validate(value,*args,**kwargs)
            clock[0]+=0.08
            return result
    client=model(monkeypatch,allocation,lambda request:httpx.Response(200,json=envelope({'ok':True})))
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        client.complete('T4_document',{},SlowSchema)
    assert allocation.used.llm_tokens==20 and allocation.used.llm_calls==1
    assert allocation.snapshot()['reserved']=={}


def test_already_expired_shared_deadline_performs_no_http_request(monkeypatch,clock):
    allocation=budget();allocation.tighten_deadline(clock[0])
    requests=[]
    client=model(monkeypatch,allocation,lambda request:requests.append(request))
    with pytest.raises(BudgetExceeded,match='wall_time_budget_exhausted'):
        client.complete('T4_document',{},ClaimBatch)
    assert requests==[] and allocation.used.llm_calls==0
    assert allocation.snapshot()['reserved']=={}

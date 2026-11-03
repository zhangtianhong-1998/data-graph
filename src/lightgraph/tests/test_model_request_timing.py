"""Per-attempt timing and fixed transport causes make timeout traces actionable."""
import json

import httpx
import pytest

from lightgraph.contracts import StrictModel
from lightgraph.runtime.trace import RunTrace, bind_trace
from lightgraph.tests.test_agent_pipeline import client, response


class Answer(StrictModel):
    answer: str


@pytest.mark.parametrize('failure,kind',[(httpx.ReadTimeout,'read_timeout'),
                                        (httpx.ConnectTimeout,'connect_timeout')])
def test_request_trace_separates_failed_and_successful_attempt_duration(monkeypatch,tmp_path,failure,kind):
    clock=[100.0]
    monkeypatch.setattr('lightgraph.discovery.llm.time.monotonic',lambda:clock[0])
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:
            clock[0]+=5.0
            raise failure('PRIVATE_TRANSPORT_ERROR_DETAIL')
        clock[0]+=2.0
        return response({'answer':'ok'})
    model=client(monkeypatch,handler)
    path=tmp_path/'timing.trace.jsonl'
    with bind_trace(RunTrace(path)):
        assert model.complete('T4_document',{},Answer).answer=='ok'
    raw=path.read_text(encoding='utf-8')
    events=[json.loads(line) for line in raw.splitlines()]
    outcomes=[e['data'] for e in events if e['kind'] in ('model_error','model_request_completed')]
    assert len(outcomes)==2
    assert outcomes[0]['request_id']==outcomes[1]['request_id']
    assert [(item['attempt'],item['status'],item['elapsed_seconds']) for item in outcomes]==[
        (1,'failed',5.0),(2,'completed',2.0)]
    assert outcomes[0]['failure_kind']==kind
    assert 'failure_kind' not in outcomes[1]
    assert 'PRIVATE_TRANSPORT_ERROR_DETAIL' not in raw

"""Complete local diagnostics survive resource cleanup without persisting secrets."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import httpx
import pytest

from lightgraph.contracts import StrictModel
from lightgraph.runtime.trace import RunTrace, bind_trace, current_trace, emit_trace, trace_artifacts, trace_path
from lightgraph.tests.test_discovery_agent import client, response, Toolbox, input_packet, final, CANARY


def events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


class Answer(StrictModel):
    answer: str


def test_trace_retains_complete_inputs_and_index_until_manual_cleanup(tmp_path):
    path=tmp_path/'run_a.trace.jsonl'
    sink=RunTrace(path,secrets=['TOP_SECRET'])
    content='完整输入'+(' x'*200000)
    packet={'messages':[{'content':content}],'api_key':'TOP_SECRET',
        'nested':{'password':'SECOND_SECRET'},'text':'Bearer TOP_SECRET',
        'uri':'mysql://name:pass@localhost/db',
        'json_text':json.dumps({'client_secret':'THIRD_SECRET'}),
        'tool_schema':{'type':'object','properties':{'password':{'type':'string','description':'User secret'}}},
        'url':'https://local.test?api_key=FOURTH_SECRET&mode=trace'}
    with bind_trace(sink):
        assert current_trace() is sink
        emit_trace('model_request',packet)
    packet.clear()
    assert current_trace() is None
    raw=path.read_text()
    assert all(s not in raw for s in ['TOP_SECRET','SECOND_SECRET','THIRD_SECRET','FOURTH_SECRET','name:pass'])
    assert events(path)[0]['data']['messages'][0]['content']==content
    assert events(path)[0]['data']['tool_schema']['properties']['password']=={'type':'string','description':'User secret'}
    index=json.loads(sink.index_path.read_text())
    assert index=={'seq':1,'offset':0,'length':path.stat().st_size}
    assert trace_path(tmp_path/'run_a.result.json')==path
    assert trace_artifacts(path)==(path,sink.index_path,sink.status_path)
    # New binding continues the same archive, without replacing prior content.
    with bind_trace(RunTrace(path)):
        emit_trace('run_end',{'status':'completed'})
    assert [e['seq'] for e in events(path)]==[1,2]


def test_append_writer_is_thread_safe_and_context_flows_to_worker_thread(tmp_path):
    path=tmp_path/'run.trace.jsonl';sink=RunTrace(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i:sink('tool_result',{'i':i}),range(40)))
    async def run():
        with bind_trace(sink):await asyncio.to_thread(emit_trace,'thread_event',{'ok':True})
    asyncio.run(run())
    assert [e['seq'] for e in events(path)]==list(range(1,42))
    raw=path.read_bytes()
    for line in sink.index_path.read_text().splitlines():
        item=json.loads(line)
        assert json.loads(raw[item['offset']:item['offset']+item['length']])['seq']==item['seq']


def test_failed_trace_write_is_visible_without_failing_extraction_or_deleting_history(tmp_path,monkeypatch):
    path=tmp_path/'run.trace.jsonl';sink=RunTrace(path)
    with bind_trace(sink):emit_trace('first',{'ok':True})
    before=path.read_bytes()
    def fail(*args):raise OSError('PRIVATE_IO_DETAIL')
    monkeypatch.setattr(sink,'_append',fail)
    with bind_trace(sink):emit_trace('second',{'input':'preserve independently'})
    assert sink.error_code=='trace_write_failed' and sink.error_count==1
    assert json.loads(sink.status_path.read_text())=={'error_code':'trace_write_failed','error_count':1}
    assert path.read_bytes()==before and 'PRIVATE_IO_DETAIL' not in sink.status_path.read_text()


def test_trace_refuses_symlink_destinations(tmp_path):
    target=tmp_path/'target';target.mkdir()
    link=tmp_path/'linked'
    try:link.symlink_to(target,target_is_directory=True)
    except OSError:pytest.skip('symlinks not permitted')
    sink=RunTrace(link/'run.trace.jsonl')
    sink('test',{'input':'must not write'})
    assert sink.error_code=='trace_write_failed' and not list(target.iterdir())


def test_malformed_json_request_is_retained_with_retry_and_actual_payload(monkeypatch,tmp_path):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls)==1:
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"answer":'},'finish_reason':'stop'}]})
        return response({'answer':'ok'})
    model=client(monkeypatch,handler)
    path=tmp_path/'run.trace.jsonl'
    with bind_trace(RunTrace(path)):
        assert model.complete('test',{'probe':'business value'},Answer).answer=='ok'
    records=events(path)
    requests=[e['data'] for e in records if e['kind']=='model_request']
    assert len(requests)==2 and [r['attempt'] for r in requests]==[1,2]
    assert requests[0]['request_id']==requests[1]['request_id']
    assert [r['payload'] for r in requests]==calls
    assert any(e['kind']=='model_response' and e['data']['response']['choices'][0]['message']['content']=='{"answer":' for e in records)
    assert any(e['kind']=='model_error' for e in records)
    assert [e for e in records if e['kind'].startswith('model_')][-1]['kind']=='model_request_completed'


def test_invalid_wire_json_and_error_http_body_are_not_lost(monkeypatch,tmp_path):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:return httpx.Response(503,text='temporary failure password=PRIVATE_PASSWORD')
        return httpx.Response(200,text='{"choices":')
    model=client(monkeypatch,handler)
    path=tmp_path/'run.trace.jsonl'
    with bind_trace(RunTrace(path)),pytest.raises(Exception):model.complete('test',{},Answer)
    wire=[e['data'] for e in events(path) if e['kind']=='model_wire_response']
    assert len(wire)==2 and wire[0]['status_code']==503
    assert wire[1]['body']=='{"choices":'
    assert 'PRIVATE_PASSWORD' not in path.read_text()


@pytest.mark.parametrize('remaining,expected,limited_by',[
    (200,200,'phase_deadline'), (750,500,'configuration'),
])
def test_trace_records_applied_http_timeout_not_only_configuration(
        monkeypatch,tmp_path,remaining,expected,limited_by):
    applied=[]
    def handler(request):
        applied.append(request.extensions['timeout'])
        return response({'answer':'ok'})
    model=client(monkeypatch,handler)
    model.config.timeout_seconds=500
    monkeypatch.setattr(model.budget,'remaining_seconds',lambda:remaining)
    path=tmp_path/'timeout.trace.jsonl'
    with bind_trace(RunTrace(path)):
        assert model.complete('test',{},Answer).answer=='ok'
    records=events(path)
    request=next(e for e in records if e['kind']=='model_request')
    transport=next(e for e in records if e['kind']=='model_transport_config')
    assert transport['data']=={
        'configured_timeout_seconds':500,'effective_timeout_seconds':expected,
        'remaining_phase_seconds':remaining,'timeout_limited_by':limited_by,
        'timeout_scope':'http_io','stream':False,
        'request_id':request['data']['request_id'],'attempt':1,
    }
    assert set(applied[0].values())=={expected}


def test_retry_trace_records_its_reduced_phase_allowance(monkeypatch,tmp_path):
    applied=[]
    def handler(request):
        applied.append(request.extensions['timeout']['read'])
        if len(applied)==1:raise httpx.ReadTimeout('temporary delay',request=request)
        return response({'answer':'ok'})
    model=client(monkeypatch,handler)
    model.config.timeout_seconds=500
    monkeypatch.setattr(model.budget,'remaining_seconds',lambda:50 if applied else 200)
    path=tmp_path/'retry_timeout.trace.jsonl'
    with bind_trace(RunTrace(path)):
        assert model.complete('test',{},Answer).answer=='ok'
    records=events(path)
    transports=[e for e in records if e['kind']=='model_transport_config']
    assert applied==[200,50]
    assert [e['data']['effective_timeout_seconds'] for e in transports]==applied
    assert [e['data']['attempt'] for e in transports]==[1,2]
    assert len({e['data']['request_id'] for e in transports})==1


@pytest.mark.parametrize('status',[429,503])
def test_error_response_trace_never_reads_a_lazy_body_before_retry(monkeypatch,tmp_path,status):
    class UnreadErrorStream(httpx.SyncByteStream):
        closed=False
        def __iter__(self):
            pytest.fail('diagnostic logging consumed a rejected error body')
            yield b''
        def close(self):self.closed=True
    body=UnreadErrorStream()
    attempts=[]
    def handler(request):
        attempts.append(1)
        return httpx.Response(status,stream=body) if len(attempts)==1 else response({'answer':'ok'})
    model=client(monkeypatch,handler)
    path=tmp_path/'run.trace.jsonl'
    with bind_trace(RunTrace(path)):
        assert model.complete('test',{},Answer).answer=='ok'
    assert body.closed and len(attempts)==2
    wire=[e['data'] for e in events(path) if e['kind']=='model_wire_response']
    assert wire[0]['status_code']==status and wire[0]['complete'] is False
    assert wire[0]['body_unavailable'] is True and 'body' not in wire[0]
    assert wire[1]['complete'] is True and 'ok' in wire[1]['body']


def test_agent_tool_input_output_and_final_drafts_survive_ephemeral_cleanup(monkeypatch,tmp_path):
    count=0
    def handler(request):
        nonlocal count
        count+=1
        if count==1:return response(tool='execute_readonly_sql',args={'sql':'SELECT opaque_a FROM t'})
        return response(final('probe_0002'))
    model=client(monkeypatch,handler)
    path=tmp_path/'run.trace.jsonl'
    with bind_trace(RunTrace(path)):
        result=model.explore(input_packet(),Toolbox(),max_steps=3)
    assert result.proposals
    records=events(path);kinds=[e['kind'] for e in records]
    assert all(k in kinds for k in ('agent_start','tool_call','tool_result','agent_end'))
    tool=next(e['data'] for e in records if e['kind']=='tool_call')
    assert tool['call']['input']['sql']=='SELECT opaque_a FROM t'
    assert CANARY in path.read_text()  # Explicitly authorized source observations.
    assert records[-1]['kind']=='agent_end'


def test_secrets_split_across_sse_deltas_are_withheld(tmp_path):
    path=tmp_path/'run.trace.jsonl'
    stream=''.join('data: '+json.dumps({'choices':[{'delta':{'content':part}}]})+'\n\n' for part in ['top-','secret'])
    with bind_trace(RunTrace(path,secrets=['top-secret'])):
        emit_trace('model_wire_response',{'body':stream})
    body=events(path)[0]['data']['body']
    assert body=='[REDACTED: stream contains credentials]'

"""Trace archives survive completion/restart; deletion is explicit and scoped."""
import json

import pytest
from fastapi.testclient import TestClient

from lightgraph.runtime.retention import atomic_text
from lightgraph.runtime.trace import RunTrace, bind_trace, emit_trace, trace_path
from lightgraph.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'workspace',frontend_dir=tmp_path/'missing')) as client:
        client.post('/api/projects',json={'name':'trace tests','project_id':'p'})
        client.post('/api/projects',json={'name':'other project','project_id':'q'})
        yield client


def seed(client,status='completed'):
    rid='run_'+'a'*24
    record=client.app.state.workspace.project_path('p')/'runs'/(rid+'.json')
    atomic_text(record,json.dumps({'run_id':rid,'project_id':'p','status':status,
        'stage':'finished','created_at':'2026-11-02T00:00:00+00:00','cleanup_status':'completed'}))
    sink=RunTrace(trace_path(record),secrets=['secret-test-key'])
    with bind_trace(sink):
        for i in range(23):emit_trace('model_request',{'input':'第'+str(i)+'步 secret-test-key','count':i})
    return rid,record,sink


def test_paging_export_and_explicit_clear_preserve_reports(client):
    rid,record,sink=seed(client)
    path=f'/api/projects/p/runs/{rid}/trace'
    first=client.get(path).json()
    assert first['total_events']==23 and len(first['events'])==10
    assert first['next_offset']==10 and first['status']=='available'
    assert not first['active'] and not first['incomplete']
    assert [e['seq'] for e in client.get(path+'?offset=20').json()['events']]==[21,22,23]
    exported=client.get(path+'/export')
    assert len(exported.text.splitlines())==23
    assert 'secret-test-key' not in exported.text and '[REDACTED]' in exported.text
    assert exported.headers['content-disposition'].startswith('attachment;')
    assert client.get(path.replace('/p/','/q/')).status_code==404
    assert client.delete(path).status_code==428
    etag=client.get('/api/projects/p/config').headers['etag']
    deleted=client.delete(path,headers={'If-Match':etag})
    assert deleted.status_code==200 and deleted.json()['status']=='cleared'
    assert not sink.path.exists() and record.exists()
    assert client.get(f'/api/projects/p/runs/{rid}').json()['status']=='completed'
    assert client.get(path+'/export').status_code==404


def test_active_export_is_committed_snapshot_and_cannot_be_cleared(client):
    rid,record,sink=seed(client,'running')
    path=f'/api/projects/p/runs/{rid}/trace'
    # A process may be killed between trace append and index commit. Readers
    # expose only complete, indexed events, never a partial JSON response.
    with sink.path.open('ab') as f:f.write(b'{"unfinished":')
    with sink.index_path.open('ab') as f:f.write(b'{"seq":24')
    assert client.get(path).json()['total_events']==23
    assert len(client.get(path+'/export').text.splitlines())==23
    etag=client.get('/api/projects/p/config').headers['etag']
    assert client.delete(path,headers={'If-Match':etag}).status_code==409
    assert sink.path.exists()
    record_data=json.loads(record.read_text())
    record_data['status']='interrupted'
    atomic_text(record,json.dumps(record_data))
    assert client.get(path).json()['incomplete'] is True


def test_trace_failure_and_legacy_absence_are_visible(client):
    rid,record,sink=seed(client)
    path=f'/api/projects/p/runs/{rid}/trace'
    atomic_text(sink.status_path,json.dumps({'error_code':'trace_write_failed','error_count':1}))
    assert client.get(path).json()['incomplete'] is True
    for artifact in (sink.path,sink.index_path,sink.status_path):artifact.unlink()
    assert client.get(path).json()['status']=='not_recorded'
    assert client.get(path+'?offset=-1').status_code==400
    assert client.get(path+'?limit=51').status_code==400


def test_trace_symlink_is_not_read_or_deleted(client,tmp_path):
    rid,_,sink=seed(client)
    sink.path.unlink()
    external=tmp_path/'other.jsonl';external.write_text('private',encoding='utf-8')
    try:sink.path.symlink_to(external)
    except (OSError,NotImplementedError):pytest.skip('symlink unavailable')
    path=f'/api/projects/p/runs/{rid}/trace'
    assert client.get(path).status_code==400
    assert client.get(path+'/export').status_code==400
    etag=client.get('/api/projects/p/config').headers['etag']
    assert client.delete(path,headers={'If-Match':etag}).status_code==400
    assert external.read_text(encoding='utf-8')=='private'


def test_export_lease_blocks_clear_until_download_or_cancel_releases(client,monkeypatch):
    import asyncio
    from fastapi.responses import StreamingResponse
    from lightgraph.web.run_traces import export_run_trace, TraceResponse
    rid,_,sink=seed(client)
    path=f'/api/projects/p/runs/{rid}/trace'
    etag=client.get('/api/projects/p/config').headers['etag']
    download=export_run_trace(client.app.state.manager,'p',rid)
    assert client.delete(path,headers={'If-Match':etag}).status_code==409
    assert sink.path.exists()
    async def disconnect(*args,**kwargs):raise asyncio.CancelledError
    monkeypatch.setattr(StreamingResponse,'__call__',disconnect)
    response=TraceResponse(download)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(response({},None,None))
    assert download.stream.closed
    assert client.delete(path,headers={'If-Match':etag}).status_code==200


def test_model_limits_save_for_inline_and_agent_file(client):
    path='/api/projects/p/model-limits'
    desired={'max_discovery_steps':24,'max_prompt_tokens':1048576,'max_output_tokens':65536,'timeout_seconds':300,
        'max_prompt_bytes':None,'max_response_bytes':None}
    assert client.put(path,json=desired).status_code==428
    before=client.get(path)
    changed=client.put(path,json=desired,headers={'If-Match':before.headers['etag']})
    assert changed.status_code==200
    assert all(changed.json()[k]==v for k,v in desired.items())
    assert client.put(path,json=desired,headers={'If-Match':before.headers['etag']}).status_code==409
    workspace=client.app.state.workspace
    original=b'framework: agentscope\nname: custom-agent\nllm:\n  mode: disabled\n  max_calls: 240\n  api_key_env: TEST_MODEL_KEY\n'
    asset=workspace.add_file('p','agent.yaml',original)
    config=workspace.get_config('p');config.pop('model');config['agent_config']=asset['relative_path']
    workspace.save_config('p',config)
    response=client.put(path,json=desired,headers={'If-Match':client.get(path).headers['etag']})
    assert response.status_code==200,response.text
    resolved=workspace.resolved_config('p')
    assert resolved.resolved_model.max_calls==240 and resolved.agent_name=='custom-agent'
    assert resolved.resolved_model.max_prompt_tokens==1048576
    assert resolved.resolved_model.max_discovery_steps==24
    assert workspace.file_path('p',asset['file_id']).read_bytes()==original
    assert workspace.get_config('p')['agent_config']!=asset['relative_path']
    assert 'model' not in workspace.get_config('p')
    etag=client.get(path).headers['etag']
    assert client.put(path,json={'api_key':'secret'},headers={'If-Match':etag}).status_code==400
    assert client.put(path,json={'max_output_tokens':0},headers={'If-Match':etag}).status_code==400
    for invalid in (0,33,1.5,None):
        assert client.put(path,json={'max_discovery_steps':invalid},headers={'If-Match':etag}).status_code==400
    assert client.get(path).json()['max_discovery_steps']==24

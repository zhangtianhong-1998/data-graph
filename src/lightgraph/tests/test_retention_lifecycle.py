"""US-04 application-path retention audit with generated canaries only.

These checks cover application artifacts, captured stdout/stderr/logs and mock
provider packets. They do not attest OS swap/core configuration, administrators'
observability systems, or a real model provider's backend retention.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Thread

import httpx
import pytest

from lightgraph.contracts import (TableSpec,ColumnSpec,SourceRef,RuleSpec,IdentitySpec,
    AttributeSpec,ExcludedColumn,ExtractionGoal,QueryLimits,OperatorSpec)
from lightgraph.discovery import StructuredModelClient,ModelConfig
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check,write_rules
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore

CANARY='RETENTION_LIFECYCLE_ONLY_6a8c742e'


def source_rows():
    table=TableSpec(source_id='audit',name='items',columns=[ColumnSpec(name=c) for c in ('item_code','item_name','private_note')])
    return [table],{'public.items':[{'item_code':'A','item_name':'Allowed label','private_note':CANARY}]}


def fixed_rule():
    return RuleSpec(rule_id='items',kind='column_group_entity',source=SourceRef(source_id='audit',table='items'),
        identity=IdentitySpec(key_columns=['item_code'],entity_kind='ReportItem'),
        attributes=AttributeSpec(single_valued={'label':'item_name'},excluded=[ExcludedColumn(column='private_note',reason='excluded')]))


def run_config(tmp_path):
    return RunConfig(sources=[],output_dir=str(tmp_path/'output'),
        goal=ExtractionGoal(excluded_columns=['private_note']))


def assert_retained_nothing(tmp_path,capsys,caplog,packets=()):
    audit=retain_check([tmp_path],[CANARY])
    captured=capsys.readouterr()
    assert audit['violations']==0,audit['paths']
    assert audit['deployment_verified'] is False
    assert CANARY not in captured.out+captured.err
    assert CANARY not in caplog.text
    assert all(CANARY not in p for p in packets)


def test_source_success_excludes_canary_from_every_application_output(tmp_path,capsys,caplog):
    tables,rows=source_rows()
    with MemorySource(tables,rows) as source:
        result=run_pipeline(run_config(tmp_path),[source],rules=[fixed_rule()])
    assert result.rules_published==['items']
    assert (tmp_path/'output'/'graph.sqlite').is_file()
    assert (tmp_path/'output'/'report.json').is_file()
    assert_retained_nothing(tmp_path,capsys,caplog)


def test_source_exception_body_is_never_reported_or_written(tmp_path,capsys,caplog):
    class FailingSource(MemorySource):
        def project(self,*args,**kwargs):
            raise RuntimeError(CANARY)
            yield # keep the source's iterator protocol
    tables,rows=source_rows()
    with FailingSource(tables,rows) as source:
        result=run_pipeline(run_config(tmp_path),[source],rules=[fixed_rule()])
    assert result.status in ('FAILED','PARTIAL','COMPLETED_WITH_GAPS')
    assert result.issues==[{'rule_id':'items','code':'execution_failed'}]
    assert not result.rules_published
    assert_retained_nothing(tmp_path,capsys,caplog)


def test_source_cancel_mid_projection_keeps_previous_head_and_no_raw_data(tmp_path,capsys,caplog):
    tables,rows=source_rows()
    with MemorySource(tables,rows) as source:
        baseline=run_pipeline(run_config(tmp_path),[source],rules=[fixed_rule()])
    assert baseline.rules_published==['items']
    with GraphStore(tmp_path/'output'/'graph.sqlite') as store:
        original={f['id'] for f in store.view({'public'})['facts']}
    class CancelAfterBatch(MemorySource):
        def project(self,*args,**kwargs):
            for batch in super().project(*args,**kwargs):
                yield batch
                self.cancel()
    rows['public.items'].append({'item_code':'B','item_name':'Unpublished second label','private_note':CANARY})
    with CancelAfterBatch(tables,rows,limits=QueryLimits(max_batch_rows=1)) as source:
        result=run_pipeline(run_config(tmp_path),[source],rules=[fixed_rule()])
    assert any(i['code']=='query_cancelled' for i in result.issues)
    assert not result.rules_published
    with GraphStore(tmp_path/'output'/'graph.sqlite') as store:
        assert {f['id'] for f in store.view({'public'})['facts']}==original
    assert_retained_nothing(tmp_path,capsys,caplog)


def _provider_envelope(request,packets):
    packets.append(request.content.decode())
    body=json.loads(request.content)
    packet=json.loads(body['messages'][1]['content'])['input']
    assert packet['strata_samples']==[]
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    proposal={'task':packet['task'],'proposals':[{'kind':'column_group','claim':{'group_id':'g','entity_kind_hint':'ReportItem',
        'members':[{'column_id':ids['item_code'],'role':'identity_code','in_identity':True},
                   {'column_id':ids['item_name'],'role':'label'}]},'evidence_cited':[ids['item_code']],
        # A provider may reflect source text; the proposal rationale stays ephemeral.
        'rationale':CANARY}],'notes':CANARY}
    return {'choices':[{'message':{'content':json.dumps(proposal)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':40,'completion_tokens':40}}


def _model(monkeypatch,handler,retries=1):
    monkeypatch.setenv('LIGHTGRAPH_RETENTION_TEST_KEY','synthetic-test-key')
    return StructuredModelClient(ModelConfig(base_url='https://model.invalid/v1',model='synthetic',
        api_key_env='LIGHTGRAPH_RETENTION_TEST_KEY',allow_source_samples=False,retries=retries),
        transport=httpx.MockTransport(handler))


def test_provider_retry_accounts_requests_without_recording_packets_or_error_body(tmp_path,capsys,caplog,monkeypatch):
    packets=[];attempts=[]
    def handler(request):
        envelope=_provider_envelope(request,packets);attempts.append(1)
        if len(attempts)==1:return httpx.Response(429,text=CANARY)
        return httpx.Response(200,json=envelope)
    model=_model(monkeypatch,handler)
    tables,rows=source_rows()
    with MemorySource(tables,rows) as source:
        report=run_pipeline(run_config(tmp_path),[source],model=model)
    assert report.rules_published and model.usage['calls']==2
    assert model.budget.snapshot()['reserved']=={}
    assert_retained_nothing(tmp_path,capsys,caplog,packets)


@pytest.mark.parametrize('stream_failure',[False,True])
def test_streamed_http_response_never_enters_artifacts_or_captured_output(tmp_path,capsys,caplog,monkeypatch,stream_failure):
    packets=[]
    class ResponseChunks(httpx.SyncByteStream):
        def __init__(self,data):self.data=data
        def __iter__(self):
            # The canary deliberately spans several streamed chunks.
            for offset in range(0,len(self.data),13):
                yield self.data[offset:offset+13]
                if stream_failure and offset>39:raise httpx.ReadError(CANARY)
    def handler(request):
        envelope=_provider_envelope(request,packets)
        return httpx.Response(200,stream=ResponseChunks(json.dumps(envelope).encode()))
    model=_model(monkeypatch,handler,retries=0)
    tables,rows=source_rows()
    with MemorySource(tables,rows) as source:
        report=run_pipeline(run_config(tmp_path),[source],model=model)
    assert model.usage['calls']==1
    if stream_failure:assert any(i['code']=='model_transport_error' for i in report.issues)
    else:assert report.rules_published
    assert_retained_nothing(tmp_path,capsys,caplog,packets)


def test_actual_process_kill_recovers_staging_without_source_retention(tmp_path,capsys,caplog):
    tables,rows=source_rows()
    with MemorySource(tables,rows) as source:
        report=run_pipeline(run_config(tmp_path),[source],rules=[fixed_rule()])
    assert report.rules_published==['items']
    db=tmp_path/'output'/'graph.sqlite'
    with GraphStore(db) as store:
        before={f['id'] for f in store.view({'public'})['facts']}
    # No source-data fixture is written: the child receives a generated marker in
    # its environment, creates only an in-memory source and stages allowed facts.
    script=r'''
import os, threading
from lightgraph.contracts import TableSpec,ColumnSpec,SourceRef,RuleSpec,IdentitySpec,AttributeSpec,ExcludedColumn
from lightgraph.sources import MemorySource
from lightgraph.rules import RuleEngine
from lightgraph.store import GraphStore
if os.name == 'posix':
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
table=TableSpec(source_id='audit',name='items',columns=[ColumnSpec(name=c) for c in ('item_code','item_name','private_note')])
source=MemorySource([table],{'public.items':[{'item_code':'A','item_name':'Pending label','private_note':os.environ['TEST_CANARY']}]})
rule=RuleSpec(rule_id='items',kind='column_group_entity',source=SourceRef(source_id='audit',table='items'),identity=IdentitySpec(key_columns=['item_code'],entity_kind='ReportItem'),attributes=AttributeSpec(single_valued={'label':'item_name'},excluded=[ExcludedColumn(column='private_note',reason='excluded')]))
store=GraphStore(os.environ['TEST_GRAPH_PATH'])
result=RuleEngine(store).execute(source,rule,defer_publish=True)
if not result.prepared: raise SystemExit(17)
print('STAGING_READY',flush=True)
threading.Event().wait()
'''
    env={**os.environ,'TEST_CANARY':CANARY,'TEST_GRAPH_PATH':str(db),'PYTHONDONTWRITEBYTECODE':'1'}
    process=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',env=env,cwd=tmp_path)
    assert process.stdout is not None
    ready='';stdout='';stderr=''
    # Windows selectors cannot watch anonymous process pipes. Read the bounded
    # ready marker on a thread; always kill and join before collecting output.
    ready_lines=Queue(maxsize=1)
    reader=Thread(target=lambda:ready_lines.put(process.stdout.readline(128)),daemon=True)
    reader.start()
    try:
        try:
            ready=ready_lines.get(timeout=10)
        except Empty:
            pytest.fail('child did not reach staging')
        assert ready.strip()=='STAGING_READY'
        process.kill()
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(timeout=10)
        assert not reader.is_alive(),'child stdout reader did not stop'
        stdout,stderr=process.communicate(timeout=10)
    if os.name=='nt':
        assert process.returncode==1  # Popen.kill uses TerminateProcess(..., 1).
    else:
        assert process.returncode==-signal.SIGKILL
    assert CANARY not in ready+stdout+stderr
    with GraphStore(db) as store:
        recovered=store.recover_staging()
        assert len(recovered)==1
        assert {f['id'] for f in store.view({'public'})['facts']}==before
    assert_retained_nothing(tmp_path,capsys,caplog)


def test_validated_rule_export_cannot_smuggle_rows_to_yaml(tmp_path,capsys,caplog):
    rule=fixed_rule();rule.status='validated';rule.validation_ref='validated'
    rule.operators=[OperatorSpec(op='distinct_projection',args={'rows':[[CANARY]]})]
    target=tmp_path/'rules.yaml'
    with pytest.raises(LightGraphError):write_rules(target,[rule])
    assert not target.exists()
    assert not list(tmp_path.glob('.published-*'))
    assert_retained_nothing(tmp_path,capsys,caplog)

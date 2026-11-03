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
from lightgraph.discovery.budget import BudgetAllocator,Cost
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
    # Legacy strict-retention mode remains available by explicit configuration.
    # The default now records local diagnostic traces at the user's request.
    return RunConfig(sources=[],output_dir=str(tmp_path/'output'),record_trace=False,
        goal=ExtractionGoal(excluded_columns=['private_note']))


def assert_retained_nothing(tmp_path,capsys,caplog,packets=()):
    audit=retain_check([tmp_path],[CANARY])
    captured=capsys.readouterr()
    assert audit['violations']==0,audit['paths']
    assert audit['deployment_verified'] is False
    assert CANARY not in captured.out+captured.err
    assert CANARY not in caplog.text
    assert all(CANARY not in p for p in packets)








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
        # This test audits retention across a retry, independently of the
        # cumulative token gate (covered by the model-budget tests).
        api_key_env='LIGHTGRAPH_RETENTION_TEST_KEY',allow_source_samples=False,retries=retries),
        budget=BudgetAllocator(Cost(db_cu=160,llm_calls=8,llm_tokens=None,memory_bytes=8*1024*1024,seconds=60)),
        transport=httpx.MockTransport(handler))








def test_validated_rule_export_cannot_smuggle_rows_to_yaml(tmp_path,capsys,caplog):
    rule=fixed_rule();rule.status='validated';rule.validation_ref='validated'
    rule.operators=[OperatorSpec(op='distinct_projection',args={'rows':[[CANARY]]})]
    target=tmp_path/'rules.yaml'
    with pytest.raises(LightGraphError):write_rules(target,[rule])
    assert not target.exists()
    assert not list(tmp_path.glob('.published-*'))
    assert_retained_nothing(tmp_path,capsys,caplog)

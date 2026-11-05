"""Successful snapshots survive failed attempts; publication never rereads facts."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_construction_mixed_sources import mixed_config, mixed_draft
from lightgraph.tests.test_graph_yaml_workspace import graph, payload
from lightgraph.tests.test_construction_runner import configuration, scripted_bridge, save_object, completed


def test_budget_failure_retains_snapshot_and_publishes_without_reading_database(tmp_path, monkeypatch):
    config, database, connection, opened = mixed_config(tmp_path, monkeypatch, 'gauss', 'excel')
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft = mixed_draft(tools.describe_sources())
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        checked = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert checked['passed']
        first_path = tools.workspace.root / checked['candidate_graph']
        original = first_path.read_bytes()
        budget = opened[0].query_budget
        budget.max_queries = budget.queries
        failed = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert not failed['passed']
        assert failed['resource_state']['code'] == 'query_budget_exhausted'
        assert failed['resource_state']['retryable'] is False
        assert failed['candidate_graph'] != checked['candidate_graph']
        assert first_path.read_bytes() == original
        record = tools.workspace.get_draft(draft['draft_id'])
        assert record.last_successful_validation.validation_id == checked['validation_id']
        assert record.validation.validation_id == failed['validation_id']
        before = len(connection.executed)
        published = tools.call('publish_graph_draft', draft_id=draft['draft_id'])
        assert published['status'] == 'ok' and published['nodes'] == 4
        assert published['publication']['validation_id'] == checked['validation_id']
        assert published['publication']['read_window']['consistency'] == 'independent_source_snapshots'
        assert len(connection.executed) == before
        # Repeated attempts never ask the adapter to run another SQL statement.
        for _ in range(3):
            stopped = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
            assert stopped['code'] == 'query_budget_exhausted' and not stopped['retryable']
        assert len(connection.executed) == before
        assert json.loads((tools.workspace.root / 'query-stop.json').read_text())['code'] == 'query_budget_exhausted'
    finally:
        tools.close()
        database.close()


def test_live_database_change_does_not_rewrite_validated_snapshot(tmp_path, monkeypatch):
    config, database, connection, _ = mixed_config(tmp_path, monkeypatch, 'gauss', 'csv')
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft = mixed_draft(tools.describe_sources())
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        checked = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert checked['passed']
        database.execute('PRAGMA query_only=OFF')
        database.execute("UPDATE public.items SET name='new value'")
        database.commit()
        database.execute('PRAGMA query_only=ON')
        before = len(connection.executed)
        assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        assert len(connection.executed) == before
        result = tools.workspace.read_published_graph()
        assert 'new value' not in str(result)
        assert result['validation_snapshots'][0]['read_window']['publication_semantics'] == 'validated_snapshot'
    finally:
        tools.close()
        database.close()


def test_database_snapshot_survives_restart_without_rereading_rows(tmp_path, monkeypatch):
    config, database, _, _ = mixed_config(tmp_path, monkeypatch, 'gauss', 'csv')
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft = mixed_draft(tools.describe_sources())
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        checked = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert checked['passed']
        original_context = tools.workspace.context_fingerprint
    finally:
        tools.close()
        database.close()

    # Open new real adapters over a different live dataset, preserving catalog,
    # authorization and the file bytes. Publishing still means the old read window.
    config, database, connection, _ = mixed_config(tmp_path, monkeypatch, 'gauss', 'csv')
    database.execute('PRAGMA query_only=OFF')
    database.execute("UPDATE public.items SET name='updated after validation'")
    database.commit()
    database.execute('PRAGMA query_only=ON')
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        assert tools.workspace.context_fingerprint == original_context
        record = tools.workspace.get_draft(draft['draft_id'])
        assert record.status == 'validated'
        assert record.last_successful_validation.validation_id == checked['validation_id']
        before = len(connection.executed)
        result = tools.call('publish_graph_draft', draft_id=draft['draft_id'])
        assert result['status'] == 'ok'
        assert len(connection.executed) == before
        graph = tools.workspace.read_published_graph()
        assert 'updated after validation' not in str(graph)
        windows = graph['validation_snapshots'][0]['read_window']['source_read_windows']
        assert {w['source_id'] for w in windows} == {'database', 'file'}
        assert all(w['opened_at'] <= w['closed_at'] for w in windows)
    finally:
        tools.close()
        database.close()


def test_database_connection_environment_change_invalidates_saved_snapshot(tmp_path, monkeypatch):
    config, database, _, _ = mixed_config(tmp_path, monkeypatch, 'gauss', 'csv')
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft = mixed_draft(tools.describe_sources())
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        assert tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
        original_context = tools.workspace.context_fingerprint
    finally:
        tools.close()
        database.close()

    # The production source opener obtains DSNs from this named variable. A
    # changed endpoint must not reuse facts from the previous connection even
    # when its catalog and configured credential revision happen to match.
    config, database, _, _ = mixed_config(tmp_path, monkeypatch, 'gauss', 'csv')
    changed_dsn = 'private-test-endpoint-not-for-artifacts'
    monkeypatch.setenv('MIXED_SOURCE_TEST_DSN', changed_dsn)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        assert tools.workspace.context_fingerprint != original_context
        assert tools.workspace.get_draft(draft['draft_id']).status == 'stale'
        result = tools.call('publish_graph_draft', draft_id=draft['draft_id'])
        assert result['code'] == 'stale_graph_draft'
        for path in tools.workspace.root.rglob('*.yaml'):
            assert changed_dsn not in path.read_text(encoding='utf-8')
    finally:
        tools.close()
        database.close()


def test_semantic_counterexample_requires_explicit_historical_selection(tmp_path):
    workspace = GraphWorkspace(tmp_path, 'context')
    draft = payload('items')
    workspace.save_draft(draft)
    success = workspace.record_validation('items', revision=1, passed=True, graph=graph()).validation
    workspace.record_validation('items', revision=1, passed=False,
        diagnostics=[{'code': 'same_identity_conflicting_label'}], graph=graph(label='conflicting'))
    with pytest.raises(LightGraphError, match='graph_validation_required'):
        workspace.publish('items', revision=1, graph=graph())
    published = workspace.publish('items', revision=1, graph=graph(), validation_id=success.validation_id)
    assert published['validation_id'] == success.validation_id
    assert workspace.get_draft('items').validation.passed is False
    modified = deepcopy(draft)
    modified['purpose'] = 'Changed purpose'
    workspace.save_draft(modified)
    with pytest.raises(LightGraphError, match='graph_validation_required'):
        workspace.select_validation('items', success.validation_id)


def test_immutable_graph_digest_guard(tmp_path):
    workspace = GraphWorkspace(tmp_path, 'context')
    workspace.save_draft(payload('items'))
    checked = workspace.record_validation('items', revision=1, passed=True, graph=graph()).validation
    path = workspace.root / checked.graph_path
    path.write_text(dump_yaml(graph(label='tampered')), encoding='utf-8')
    with pytest.raises(LightGraphError, match='validated_graph_mismatch'):
        workspace.read_validation_graph('items', checked)


def test_legacy_validation_remains_readable_after_schema_upgrade(tmp_path):
    workspace = GraphWorkspace(tmp_path, 'context')
    workspace.save_draft(payload('items'))
    workspace.record_validation('items', revision=1, passed=True, graph=graph())
    manifest = workspace._manifest()
    relative = manifest['drafts']['items']['current']
    previous = workspace._read_record(relative)
    previous.pop('last_successful_validation')
    for key in ['validation_id', 'graph_path', 'read_window']:
        previous['validation'].pop(key)
    (workspace.root / relative).write_text(dump_yaml(previous), encoding='utf-8')
    legacy_path = workspace.root / f'previews/{digest("items")[:16]}-1.yaml'
    legacy_path.write_text(dump_yaml(graph()), encoding='utf-8')
    checked = workspace.select_validation('items')
    assert checked.validation_id is None
    assert workspace.read_validation_graph('items', checked) == graph()
    assert workspace.publish('items', revision=1, graph=graph())['revision'] == 1


@pytest.mark.parametrize('stop_code', ['query_budget_exhausted', 'source_retry_limit_exhausted'])
def test_query_stop_allows_only_two_finalization_model_dispatches(tmp_path, stop_code):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute native Harness plugin')
    module = Path(__file__).parents[1] / 'construction/runtime/run-budget.mjs'
    stop_path = tmp_path / 'query-stop.json'
    script = tmp_path / 'test.mjs'
    script.write_text('''
import { apply } from MODULE
import { writeFileSync } from 'node:fs'
const hooks = new Map()
const ctx = {on:(name,fn)=>hooks.set(name,fn), tools:{guard:()=>{}}, tokenMeter:{estimateMessage:()=>0}}
apply(ctx,{maxCalls:1000,maxRunSeconds:60,output:OUTPUT,queryStopFile:STOP})
let network=0
async function* next(){network++;yield {type:'text',text:'ok'}}
for await (const c of hooks.get('llm/stream')({},next)) {}
writeFileSync(STOP,JSON.stringify({code:STOP_CODE}))
for(const purpose of ['agent','summary']) for await(const c of hooks.get('llm/stream')({purpose},next)){}
let code
try {for await(const c of hooks.get('llm/stream')({purpose:'retry'},next)){}}catch(e){code=e.code}
console.log(JSON.stringify({network,code}))
'''.replace('MODULE', json.dumps(module.as_uri())).replace('OUTPUT', json.dumps(str(tmp_path / 'budget.yaml')))
       .replace('STOP_CODE', json.dumps(stop_code)).replace('STOP', json.dumps(str(stop_path))), encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(result.stdout) == {'network': 3, 'code': stop_code}


def test_report_keeps_budget_cause_and_successful_publication(tmp_path, scripted_bridge):
    from lightgraph.construction.runner import run_construction
    config = configuration(tmp_path)
    def runtime(*args, **kwargs):
        tools = scripted_bridge['tools']
        draft = save_object(tools)
        assert tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
        budget = tools.adapters[0].query_budget
        budget.max_queries = budget.queries
        assert not tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
        assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        return completed(3)
    report = run_construction(config, runtime=runtime)
    assert report.status == 'PARTIAL'
    assert report.counts['business_nodes'] == 2 and report.counts['rules_published'] == 1
    assert any(i['code'] == 'query_budget_exhausted' for i in report.issues)
    assert report.counts['db_query_admissions'] > 0

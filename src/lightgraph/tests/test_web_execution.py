"""Local process execution, publication truth and retention boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
import yaml

from lightgraph.contracts import (ColumnSpec, GraphFact, PartitionCompletion, RuleSpec,
                                  RunReport, SupportSpec, TableSpec)
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.web.execution import (RunManager, _bounded_view, _input_manifest,
                                      _resolved_payload, read_graph, read_metadata)
from lightgraph.web.worker import execute


class ProjectWorkspace:
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        self.configs = {}
        self.locks = {}

    def add(self, pid):
        path = self.root / pid
        path.mkdir()
        document = path / 'analysis.md'
        document.write_text('---\npurpose: analysis_knowledge\n---\n# 分析目标\n分析收入指标。\n', encoding='utf-8')
        self.configs[pid] = RunConfig(sources=[], documents=[str(document)], output_dir=str(path / 'output'), isolation_domain=pid)
        return path

    def project_path(self, pid):
        if pid not in self.configs:
            raise LightGraphError('project_not_found')
        return self.root / pid

    def resolved_config(self, pid):
        self.project_path(pid)
        return self.configs[pid]

    def config_revision(self, pid):
        return 1

    def lock(self, pid):
        self.project_path(pid)
        return self.locks.setdefault(pid, threading.RLock())


@pytest.fixture
def workspace(tmp_path):
    result = ProjectWorkspace(tmp_path / 'workspace')
    result.add('alpha')
    result.add('beta')
    return result


def prepare_csv(workspace):
    project = workspace.project_path('alpha')
    data = project / 'metrics.csv'
    data.write_text('metric_code,metric_name,amount\nK1,收入,RETENTION_SECRET_312\nK1,收入,999\nK2,费用,100\n', encoding='utf-8')
    rule = RuleSpec(rule_id='metrics', kind='column_group_entity',
                    source={'source_id': 'input', 'table': 'metrics'},
                    identity={'key_columns': ['metric_code'], 'entity_kind': 'Metric'},
                    attributes={'single_valued': {'label': 'metric_name'},
                                'excluded': [{'column': 'amount', 'reason': 'observation_value'}]},
                    evidence={'semantic_basis': ['fixture:E:approved_definition']})
    rules = project / 'rules.yaml'
    rules.write_text(yaml.safe_dump([rule.model_dump(mode='json', by_alias=True)]), encoding='utf-8')
    workspace.configs['alpha'] = RunConfig(
        sources=[dict(id='input', kind='csv_file', path=str(data), encoding='utf-8', delimiter=',',
                      header=True, null_values=[''], type_inference='text', table_name='metrics')],
        rules=[str(rules)], output_dir=str(project / 'output'), isolation_domain='alpha')
    return data


def wait_run(manager, pid, rid, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        record = manager.get(pid, rid)
        if record['status'] not in {'queued', 'running'}:
            return record
        time.sleep(.03)
    pytest.fail('worker did not finish')


def test_real_worker_csv_publishes_and_incremental_reuses_output(workspace):
    prepare_csv(workspace)
    manager = RunManager(workspace)
    try:
        first = manager.start('alpha')
        result = wait_run(manager, 'alpha', first['run_id'])
        assert result['status'] == 'completed', result
        assert result['report']['rules_published'] == ['metrics']
        assert result['cleanup_status'] == 'completed'
        metadata = read_metadata(workspace, 'alpha')
        assert metadata['requires_refresh'] is False and metadata['tables'][0]['name'] == 'metrics'
        before = read_graph(workspace, 'alpha')
        assert {n['label'] for n in before['nodes']} == {'收入', '费用'}
        second = manager.start('alpha')
        done = wait_run(manager, 'alpha', second['run_id'])
        assert done['status'] == 'completed'
        assert {n['id'] for n in read_graph(workspace, 'alpha')['nodes']} == {n['id'] for n in before['nodes']}
        assert not read_graph(workspace, 'beta')['nodes']
        assert len(manager.list('alpha')) == 2
        for path in (workspace.root / 'alpha' / 'output').rglob('*'):
            if path.is_file():
                assert b'RETENTION_SECRET_312' not in path.read_bytes()
        for path in (workspace.root / 'alpha' / 'runs').iterdir():
            assert path.suffix == '.json'
            assert b'RETENTION_SECRET_312' not in path.read_bytes()
    finally:
        manager.shutdown()


def test_missing_worker_result_is_failure_not_success_exit(workspace, monkeypatch):
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace)
    result = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    assert result['status'] == 'failed'
    assert result['error_code'] == 'worker_result_missing'
    assert result['cleanup_status'] == 'unknown'
    manager.shutdown()


def test_cancel_and_exclusivity_keep_cleanup_unknown_without_worker_report(workspace, monkeypatch):
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys,time; sys.stdin.buffer.read(); time.sleep(30)'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace, cancel_timeout=.1)
    record = manager.start('alpha')
    with pytest.raises(LightGraphError, match='project_run_active'):
        manager.start('alpha')
    result = manager.cancel('alpha', record['run_id'])
    assert result['status'] == 'cancelled'
    assert result['cleanup_status'] == 'unknown'
    assert not manager.active('alpha')
    manager.shutdown()


def test_force_cancel_does_not_claim_cleanup(workspace, monkeypatch):
    real_popen = subprocess.Popen
    ready = workspace.project_path('alpha') / 'ready'
    def child(*args, **kwargs):
        code = 'import sys,time,pathlib; pathlib.Path("ready").touch(); sys.stdin.buffer.read(); time.sleep(30)'
        return real_popen([sys.executable, '-c', code], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace, cancel_timeout=.05)
    record = manager.start('alpha')
    until = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < until:
        time.sleep(.01)
    assert ready.exists()
    result = manager.cancel('alpha', record['run_id'])
    assert result['status'] == 'cancelled'
    assert result['error_code'] == 'run_force_stopped'
    assert result['cleanup_status'] == 'unknown'
    manager.shutdown()


def test_real_worker_cooperative_cancel_cleans_sources_and_keeps_report(workspace, monkeypatch):
    # Run the real worker entrypoint/monitor in a child on every OS. Only the
    # pipeline is replaced, so no database or model is needed for cancellation.
    code = r'''
import sys,time
from pathlib import Path
import lightgraph.pipeline as pipeline
import lightgraph.runtime.config as config
from lightgraph.contracts import RunReport, TableSpec, ColumnSpec, QueryLimits
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource
from lightgraph.web.worker import main
table=TableSpec(source_id='memory',name='items',columns=[ColumnSpec(name='code')])
source=MemorySource([table],{'public.items':[(str(i),) for i in range(100)]},limits=QueryLimits(max_batch_rows=1))
close=source.close
def closed():
    close()
    Path('source-closed').touch()
source.close=closed
config.open_sources=lambda _: [source]
def run(*a,**kw):
    try:
        for batch in source.project(table,['code']):
            Path('worker-ready').touch()
            time.sleep(.1)
    except LightGraphError as exc:
        if exc.code!='query_cancelled': raise
        return RunReport(run_id='cancelled',status='PARTIAL',rules_published=['already_published'])
    raise AssertionError('query was not cancelled')
pipeline.run_pipeline=run
sys.exit(main())
'''
    real_popen = subprocess.Popen
    def child(args, **kwargs):
        return real_popen([sys.executable, '-c', code, args[-1]], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace, cancel_timeout=5)
    project = workspace.project_path('alpha')
    try:
        record = manager.start('alpha')
        until = time.monotonic() + 30
        while not (project / 'worker-ready').exists() and time.monotonic() < until:
            assert manager.active('alpha'), manager.get('alpha', record['run_id'])
            time.sleep(.03)
        assert (project / 'worker-ready').exists()
        result = manager.cancel('alpha', record['run_id'])
        assert result['status'] == 'cancelled'
        assert result['error_code'] == 'run_cancelled'
        assert result['cleanup_status'] == 'completed'
        assert result['report']['rules_published'] == ['already_published']
        assert (project / 'source-closed').exists()
        assert not list((project / 'runs').glob('*.cancel'))
        assert not manager.active('alpha')
    finally:
        manager.shutdown()


def test_restart_marks_old_records_interrupted_without_signalling_pid(workspace, monkeypatch):
    runs = workspace.project_path('alpha') / 'runs'
    runs.mkdir()
    rid = 'run_' + 'a' * 24
    path = runs / (rid + '.json')
    path.write_text(json.dumps(dict(run_id=rid, project_id='alpha', status='running', created_at='2026', pid=1)))
    marker = runs / (rid + '.result.cancel')
    marker.write_text('cancel\n', encoding='utf-8')
    monkeypatch.setattr('os.kill', lambda *_: pytest.fail('historical PID must not be killed'))
    manager = RunManager(workspace)
    record = manager.get('alpha', rid)
    assert record['status'] == 'interrupted'
    assert record['cleanup_status'] == 'unknown'
    assert record['error_code'] == 'service_restarted'
    assert not marker.exists()
    manager.shutdown()


def test_worker_error_redacts_exception_and_credentials(workspace, monkeypatch):
    config = workspace.configs['alpha']
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _config: (_ for _ in ()).throw(RuntimeError('SOURCE_ROW_SECRET password=secret')))
    path = workspace.project_path('alpha') / 'runs' / 'result.json'
    execute(_resolved_payload(config, workspace.project_path('alpha')), path)
    raw = path.read_text(encoding='utf-8')
    assert 'SOURCE_ROW_SECRET' not in raw and 'password' not in raw
    assert json.loads(raw)['error_code'] == 'worker_execution_failed'


def test_worker_dependency_change_is_partial_with_safe_report(workspace, monkeypatch):
    data = prepare_csv(workspace)
    config = workspace.configs['alpha']
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _: [])
    def run(*_args, **_kwargs):
        data.write_text('metric_code,metric_name\nnew,new\n')
        return RunReport(run_id='test', rules_published=['existing'])
    monkeypatch.setattr('lightgraph.pipeline.run_pipeline', run)
    result = workspace.project_path('alpha') / 'runs' / 'result.json'
    execute(_resolved_payload(config, workspace.project_path('alpha')), result)
    record = json.loads(result.read_text(encoding='utf-8'))
    assert record['status'] == 'partial'
    assert record['report']['rules_published'] == ['existing']
    assert record['report']['status'] == 'PARTIAL'
    assert record['error_code'] == 'input_dependency_changed'


def test_metadata_refresh_only_describes_and_closes_adapter(workspace, monkeypatch):
    table = TableSpec(source_id='input', name='metrics', columns=[ColumnSpec(name='code')])
    source = MemorySource([table], {'public.metrics': [('PRIVATE_VALUE',)]})
    source.execute = lambda *_args, **_kwargs: pytest.fail('metadata refresh must not run instance queries')
    calls = []
    def opened(config, *, metadata_only=False):
        calls.append(metadata_only)
        return [source]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', opened)
    result = read_metadata(workspace, 'alpha', refresh=True)
    assert calls == [True]
    assert source._closed
    assert result['tables'][0]['id'] == 'input.public.metrics'
    assert result['instance_queries'] == 0
    assert any(edge['relation'] == 'table_has_column' for edge in result['edges'])
    assert 'PRIVATE_VALUE' not in json.dumps(result)
    path = workspace.project_path('alpha') / 'output' / 'metadata.yaml'
    assert path.exists() and 'PRIVATE_VALUE' not in path.read_text(encoding='utf-8')
    # Cached declaration remains usable without connecting or loading rows.
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: pytest.fail('cached read reconnects'))
    assert read_metadata(workspace, 'alpha')['tables'] == result['tables']


def publish(store, domain, scope, label, layer='business'):
    pub = store.begin_publication('fixture:' + label, 1, 'all', {})
    support = SupportSpec(origin='catalog', source_binding=label, scope_ref=scope,
                          partition_id='all', rule_id='fixture:' + label, rule_revision=1)
    for kind, payload in [('NodeExistence', {'entity_id': label, 'kind': 'Metric' if layer == 'business' else 'Column', 'layer': layer}),
                          ('PropertyValue', {'entity_id': label, 'name': 'label', 'value': label})]:
        store.stage_fact(pub, GraphFact(isolation_domain=domain, fact_kind=kind, payload=payload), support)
    store.publish_partition(PartitionCompletion(publication_id=pub, complete=True, consistent=True, dependencies_valid=True))


def test_graph_uses_authorized_publication_heads_not_export(workspace):
    path = workspace.project_path('alpha') / 'output' / 'graph.sqlite'
    with GraphStore(path) as store:
        publish(store, 'alpha', 'public', 'visible')
        publish(store, 'alpha', 'private', 'hidden')
        publish(store, 'beta', 'public', 'other-project')
        publish(store, 'alpha', 'public', 'column', 'metadata')
    (path.parent / 'graph.json').write_text(json.dumps({'nodes': [{'id': 'stale', 'label': 'stale'}]}))
    assert [n['label'] for n in read_graph(workspace, 'alpha')['nodes']] == ['visible']
    metadata = read_graph(workspace, 'alpha', 'metadata')
    assert not metadata['nodes'] and metadata['requires_refresh']
    with pytest.raises(LightGraphError, match='graph_layer_invalid'):
        read_graph(workspace, 'alpha', 'raw')


def test_graph_truncation_preserves_endpoints(monkeypatch):
    monkeypatch.setattr('lightgraph.web.execution.MAX_GRAPH_NODES', 2)
    monkeypatch.setattr('lightgraph.web.execution.MAX_GRAPH_EDGES', 1)
    result = _bounded_view({'nodes': [{'id': str(i), 'layer': 'business'} for i in range(3)],
                            'edges': [{'source': '0', 'target': '1'}, {'source': '1', 'target': '2'}]}, 'business')
    assert result['truncated'] and result['total_nodes'] == 3 and result['total_edges'] == 2
    ids = {n['id'] for n in result['nodes']}
    assert all(e['source'] in ids and e['target'] in ids for e in result['edges'])


def test_run_ids_and_file_dependencies_cannot_escape_project(workspace):
    manager = RunManager(workspace)
    with pytest.raises(LightGraphError, match='run_not_found'):
        manager.get('alpha', '../beta/run_x')
    target = workspace.project_path('beta') / 'input.md'
    target.write_text('private document')
    workspace.configs['alpha'].documents = [str(target)]
    with pytest.raises(LightGraphError, match='input_dependency_changed'):
        manager.start('alpha')
    manager.shutdown()


def test_empty_project_and_empty_purpose_do_not_start(workspace):
    manager = RunManager(workspace)
    workspace.configs['alpha'].documents = []
    with pytest.raises(LightGraphError, match='project_inputs_required'):
        manager.start('alpha')
    workspace.configs['beta'].goal.purpose = '  '
    with pytest.raises(LightGraphError, match='extraction_purpose_required'):
        manager.start('beta')
    assert not manager.active('alpha') and not manager.active('beta')
    manager.shutdown()


def test_metadata_first_get_never_connects(workspace, monkeypatch):
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: pytest.fail('GET must not connect'))
    result = read_metadata(workspace, 'alpha')
    assert not result['tables'] and not result['nodes']
    assert result['requires_refresh'] is True
    assert result['coverage'] == 'not_read'


def test_refresh_cannot_be_replaced_by_stale_published_metadata(workspace, monkeypatch):
    from lightgraph.metadata import publish_metadata
    old = TableSpec(source_id='input', name='old_table', columns=[ColumnSpec(name='old_column')])
    new = TableSpec(source_id='input', name='new_table', columns=[ColumnSpec(name='new_column')])
    path = workspace.project_path('alpha') / 'output' / 'graph.sqlite'
    with MemorySource([old], {}) as source, GraphStore(path) as store:
        publish_metadata(store, source, domain='alpha')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [MemorySource([new], {})])
    refreshed = read_metadata(workspace, 'alpha', refresh=True)
    cached = read_metadata(workspace, 'alpha')
    for result in (refreshed, cached):
        assert result['stale_engine_metadata'] is True
        assert 'new_column' in {n['label'] for n in result['nodes']}
        assert 'old_column' not in {n['label'] for n in result['nodes']}
        assert result['tables'][0]['name'] == 'new_table'


def test_graceful_cancel_preserves_published_report_and_closes_sources(workspace, monkeypatch):
    import signal
    source = MemorySource([], {})
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _config: [source])
    def run(*_a, **_kw):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return RunReport(run_id='cancelled', status='PARTIAL', rules_published=['already_published'])
    monkeypatch.setattr('lightgraph.pipeline.run_pipeline', run)
    path = workspace.project_path('alpha') / 'runs' / 'cancel.json'
    execute(_resolved_payload(workspace.configs['alpha'], workspace.project_path('alpha')), path)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record['status'] == 'cancelled' and record['cleanup_status'] == 'completed'
    assert record['report']['rules_published'] == ['already_published']
    assert source._closed


def test_late_cancel_marker_cannot_interrupt_source_cleanup(workspace, monkeypatch):
    source = MemorySource([], {})
    path = workspace.project_path('alpha') / 'runs' / 'late.json'
    path.parent.mkdir()
    close = source.close
    def closing():
        path.with_suffix('.cancel').write_text('cancel\n', encoding='utf-8')
        time.sleep(.1)
        close()
    source.close = closing
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _config: [source])
    monkeypatch.setattr('lightgraph.pipeline.run_pipeline', lambda *_a, **_kw: RunReport(run_id='done'))
    execute(_resolved_payload(workspace.configs['alpha'], workspace.project_path('alpha')), path)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record['status'] == 'completed'
    assert record['cleanup_status'] == 'completed' and source._closed


def test_preexisting_cancel_marker_still_runs_worker_finally(workspace, monkeypatch):
    path = workspace.project_path('alpha') / 'runs' / 'early.json'
    path.parent.mkdir()
    path.with_suffix('.cancel').write_text('cancel\n', encoding='utf-8')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _: pytest.fail('cancelled startup must not open sources'))
    execute(_resolved_payload(workspace.configs['alpha'], workspace.project_path('alpha')), path)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record['status'] == 'cancelled'
    assert record['cleanup_status'] == 'completed'
    assert record['error_code'] == 'run_cancelled'


def test_cancel_during_opening_closes_new_sources_without_starting_pipeline(workspace, monkeypatch):
    path = workspace.project_path('alpha') / 'runs' / 'opening.json'
    path.parent.mkdir()
    source = MemorySource([], {})
    cancel_calls = []
    cancel = source.cancel
    def cancelled():
        cancel_calls.append(True)
        cancel()
    source.cancel = cancelled
    def opening(_config):
        # Inject at an explicit phase, without relying on timer scheduling.
        path.with_suffix('.cancel').write_text('cancel\n', encoding='utf-8')
        return [source]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', opening)
    monkeypatch.setattr('lightgraph.pipeline.run_pipeline', lambda *_a, **_kw: pytest.fail('cancelled opening must not run pipeline'))
    execute(_resolved_payload(workspace.configs['alpha'], workspace.project_path('alpha')), path)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record['status'] == 'cancelled' and record['cleanup_status'] == 'completed'
    assert cancel_calls == [True] and source._closed


@pytest.mark.parametrize('statement', ['closing.set()', 'source.close()', '_save(result_path, result)'])
def test_signal_at_exact_cleanup_instruction_never_skips_finally(workspace, monkeypatch, statement):
    import inspect
    import signal
    source = MemorySource([], {})
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda _: [source])
    monkeypatch.setattr('lightgraph.pipeline.run_pipeline', lambda *_a, **_kw: RunReport(run_id='done'))
    lines, start = inspect.getsourcelines(execute)
    target_line = next(start + i for i, line in enumerate(lines) if line.strip() == statement)
    injected = []
    def trace(frame, event, _arg):
        if event == 'line' and frame.f_code is execute.__code__ and frame.f_lineno == target_line and not injected:
            injected.append(True)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, frame)
        return trace
    previous = sys.gettrace()
    path = workspace.project_path('alpha') / 'runs' / 'signal.json'
    try:
        sys.settrace(trace)
        execute(_resolved_payload(workspace.configs['alpha'], workspace.project_path('alpha')), path)
    finally:
        sys.settrace(previous)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert injected and source._closed
    assert record['cleanup_status'] == 'completed'
    if statement != '_save(result_path, result)':
        assert record['status'] == 'cancelled'


def test_workspace_service_lock_excludes_second_manager_and_releases(workspace):
    first = RunManager(workspace)
    try:
        with pytest.raises(LightGraphError, match='workspace_service_active'):
            RunManager(workspace)
    finally:
        first.shutdown()
    second = RunManager(workspace)
    second.shutdown()


def test_child_retains_service_lock_after_parent_crash(workspace):
    # Exercise actual OS handle inheritance. The child stays alive until this
    # test releases it, avoiding timing assumptions on Windows CI machines.
    script = r"""
import os,sys,threading,subprocess
from pathlib import Path
from lightgraph.web.execution import RunManager
from lightgraph.runtime.config import RunConfig
root=Path(sys.argv[1])
class W:
    def __init__(self): self.root=root; self.l=threading.RLock()
    def project_path(self,p): return root/p
    def lock(self,p): return self.l
    def config_revision(self,p): return 1
    def resolved_config(self,p): return RunConfig(sources=[],documents=[str(root/p/'analysis.md')],output_dir=str(root/p/'output'))
real=subprocess.Popen
import lightgraph.web.execution as ex
def child(*a,**kw):
    code='import sys,time,pathlib; sys.stdin.buffer.read(); stop=pathlib.Path("release-orphan"); deadline=time.monotonic()+45\nwhile not stop.exists() and time.monotonic()<deadline: time.sleep(.03)'
    return real([sys.executable,'-c',code],**kw)
ex.subprocess.Popen=child
m=RunManager(W())
m.start('alpha')
os._exit(0)
"""
    parent = subprocess.Popen([sys.executable, '-c', script, str(workspace.root)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert parent.wait(timeout=30) == 0
        with pytest.raises(LightGraphError, match='workspace_service_active'):
            RunManager(workspace)
    finally:
        (workspace.project_path('alpha') / 'release-orphan').touch()
        if parent.poll() is None:
            parent.kill(); parent.wait()
    until = time.monotonic() + 15
    while True:
        try:
            recovered = RunManager(workspace)
            break
        except LightGraphError as exc:
            assert exc.code == 'workspace_service_active'
            if time.monotonic() > until:
                pytest.fail('child failed to release inherited service lock')
            time.sleep(.03)
    try:
        records = recovered.list('alpha')
        assert len(records) == 1 and records[0]['status'] == 'interrupted'
        assert records[0]['cleanup_status'] == 'unknown'
    finally:
        recovered.shutdown()


def test_removed_source_and_narrowed_columns_do_not_reappear_from_old_graph(workspace, monkeypatch):
    from lightgraph.metadata import publish_metadata
    from lightgraph.runtime.config import SourceConfig
    a = TableSpec(source_id='a', name='one', columns=[ColumnSpec(name='code'), ColumnSpec(name='secret_column')])
    b = TableSpec(source_id='b', name='two', columns=[ColumnSpec(name='other_column')])
    config = workspace.configs['alpha']
    config.sources = [SourceConfig(id='a', kind='gauss_database', allowed_tables=['public.one'], dsn_env='A_DSN'),
                      SourceConfig(id='b', kind='gauss_database', allowed_tables=['public.two'], dsn_env='B_DSN')]
    graph_path = workspace.project_path('alpha') / 'output' / 'graph.sqlite'
    with GraphStore(graph_path) as store:
        for table in (a, b):
            with MemorySource([table], {}) as source:
                publish_metadata(store, source, domain='alpha')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [MemorySource([a], {}), MemorySource([b], {})])
    initial = read_metadata(workspace, 'alpha', refresh=True)
    assert initial['stale_engine_metadata'] is False
    config.sources = config.sources[:1]
    cached = read_metadata(workspace, 'alpha')
    assert cached['requires_refresh'] and not cached['tables'] and not cached['nodes']
    assert not read_graph(workspace, 'alpha', 'metadata')['nodes']
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [MemorySource([a], {})])
    refreshed = read_metadata(workspace, 'alpha', refresh=True)
    assert refreshed['stale_engine_metadata']
    assert 'other_column' not in json.dumps(refreshed)
    config.sources[0].allowed_columns = {'public.one': ['code']}
    cached = read_metadata(workspace, 'alpha')
    assert cached['requires_refresh'] and not cached['nodes']
    narrowed = a.model_copy(update={'columns': [ColumnSpec(name='code')]})
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [MemorySource([narrowed], {})])
    refreshed = read_metadata(workspace, 'alpha', refresh=True)
    assert 'secret_column' not in json.dumps(refreshed)
    assert refreshed['stale_engine_metadata']
    config.sources = []
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [])
    empty = read_metadata(workspace, 'alpha', refresh=True)
    assert not empty['tables'] and not empty['nodes'] and not empty['edges']
    assert empty['stale_engine_metadata']

"""Worker failures retain useful evidence without keeping driver/process text."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from lightgraph.construction.paths import io_path
from lightgraph.contracts import RunReport
from lightgraph.errors import LightGraphError
from lightgraph.runtime.trace import RunTrace, bind_trace, trace_path
from lightgraph.sources.database_errors import database_error
from lightgraph.web.app import create_app
from lightgraph.web.worker_diagnostics import (DatabaseDiagnosticHandler, MAX_LOG_BYTES,
    StderrCollector, WorkerDiagnostics, read_worker_log, worker_log_path)
from lightgraph.tests.test_web_execution import ProjectWorkspace, wait_run
from lightgraph.web.execution import RunManager, _resolved_payload
from lightgraph.web.worker import execute

SECRET = 'PRIVATE_ROW_AND_PASSWORD'


def test_structured_exception_and_driver_metadata_never_format_raw_message(tmp_path):
    record = tmp_path / 'run.json'
    sink = WorkerDiagnostics(record)
    driver_type = type('OperationalError', (Exception,), {'__module__': 'psycopg.errors'})
    for state in ('08006', None):
        raw = driver_type(f'postgresql://admin:{SECRET}@private-host/db SELECT * FROM private_table')
        raw.sqlstate = state
        error = database_error('query_execution_failed', raw, engine='gauss', stage='query')
        try:
            raise error from raw
        except LightGraphError as caught:
            sink.emit('worker_error', stage='construction', error=caught,
                      error_code=caught.code, message=SECRET, password=SECRET)
    events = read_worker_log(record)['events']
    assert [item['database']['category'] for item in events] == ['connection', 'unknown']
    assert [item['database']['sqlstate'] for item in events] == ['08006', 'unknown']
    assert all(item['exception_type'] == 'LightGraphError' and item['frames'] for item in events)
    text = worker_log_path(record).read_text()
    assert all(value not in text for value in (SECRET, 'private-host', 'private_table', 'SELECT', 'postgresql://'))


def test_diagnostic_extras_survive_handled_tool_errors_and_mirror_to_trace(tmp_path):
    record = tmp_path / 'run.json'
    sink = WorkerDiagnostics(record)
    trace = RunTrace(trace_path(record))
    handler = DatabaseDiagnosticHandler(sink, lambda: 'construction')
    logger = logging.getLogger('lightgraph.sources.database_errors')
    raw = Exception(SECRET)
    safe = database_error('query_execution_failed', raw, engine='gauss', stage='query')
    record_obj = logger.makeRecord(logger.name, logging.ERROR, __file__, 1,
        'raw arguments %s', (SECRET,), None, extra={'database_diagnostic': safe.database_diagnostic,
            'error_code': 'query_execution_failed', 'password': SECRET})
    with bind_trace(trace):
        handler.handle(record_obj)
    events = read_worker_log(record)['events']
    assert events[0]['code'] == 'worker_database_diagnostic'
    assert events[0]['database']['category'] == 'unknown'
    assert SECRET not in worker_log_path(record).read_text()
    exported = trace.path.read_text()
    assert 'worker_diagnostic' in exported and 'query_execution_failed' in exported and SECRET not in exported


def test_raw_stderr_is_drained_with_bounded_lines_and_no_raw_log(tmp_path):
    code = "import sys; sys.stderr.write('" + SECRET + "' * 100000 + '\\nOperationalError: " + SECRET + "\\n'); sys.stderr.flush()"
    child = subprocess.Popen([sys.executable, '-c', code], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    collector = StderrCollector(child.stderr)
    assert child.wait(timeout=10) == 0
    summary = collector.finish()
    assert summary['stderr_bytes'] > 2 * 1024 * 1024
    assert summary['stderr_lines'] == 2 and summary['oversized_lines'] == 1
    assert summary['observed_exception_types'] == ['OperationalError']
    assert summary['stderr_complete'] and not collector.thread.is_alive()
    sink = WorkerDiagnostics(tmp_path / 'run.json')
    sink.emit('worker_stderr_summary', **summary)
    assert SECRET not in sink.path.read_text() and 'database' not in sink.path.read_text()


def test_collector_stops_when_an_inherited_writer_keeps_the_pipe_open():
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'rb')
    collector = StderrCollector(stream)
    try:
        os.write(write_fd, b'MemoryError: do not retain this\n')
        started = time.monotonic()
        summary = collector.finish()
        assert time.monotonic() - started < 2
        assert not collector.thread.is_alive() and stream.closed
        assert summary['observed_exception_types'] == ['MemoryError']
        assert not summary['stderr_complete']
    finally:
        os.close(write_fd)


def test_log_is_bounded_and_never_automatically_removed(tmp_path):
    sink = WorkerDiagnostics(tmp_path / 'run.json')
    for _ in range(3000):
        sink.emit('worker_database_diagnostic', stage='construction', error_code='query_execution_failed')
    sink.emit('worker_process_exited', exit_code=7)
    assert sink.path.stat().st_size <= MAX_LOG_BYTES
    result = read_worker_log(tmp_path / 'run.json')
    assert result['status'] == 'available' and result['incomplete']
    assert any(item['code'] == 'worker_log_truncated' for item in result['events'])
    assert result['events'][-1]['code'] == 'worker_process_exited' and result['events'][-1]['exit_code'] == 7


def test_log_write_failure_is_visible_without_failing_extraction(tmp_path, monkeypatch):
    record = tmp_path / 'run.json'
    sink = WorkerDiagnostics(record)
    original = os.open
    def fail_log(path, *args, **kwargs):
        if str(path).endswith('.worker.jsonl'):
            raise PermissionError(SECRET)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', fail_log)
    sink.emit('worker_started')
    assert sink.failed
    result = read_worker_log(record)
    assert result['status'] == 'unavailable' and result['incomplete']
    assert result['error_code'] == 'worker_diagnostic_write_failed'
    assert SECRET not in sink.path.with_suffix('.status.json').read_text()


@pytest.mark.parametrize('code', ['read_only_transaction_failed', 'snapshot_close_failed',
                                  'source_catalog_changed', 'database_reconnect_failed'])
def test_primary_database_error_codes_are_preserved(tmp_path, code):
    record = tmp_path / 'run.json'
    sink = WorkerDiagnostics(record)
    sink.emit('worker_database_diagnostic', error_code=code)
    assert read_worker_log(record)['events'][0]['error_code'] == code


def test_probe_diagnostics_preserve_reason_and_bounds_without_raw_readback(tmp_path):
    record = tmp_path / 'run.json'
    sink = WorkerDiagnostics(record)
    trace = RunTrace(trace_path(record))
    handler = DatabaseDiagnosticHandler(sink, lambda: 'construction')
    logger = logging.getLogger('lightgraph.sources.database_errors')
    detail = {'engine': 'gauss', 'stage': 'connection_preflight', 'category': 'qualification',
              'probe_code': 'parameter_binding_unverified', 'probe_failure': 'readback_mismatch',
              'boundary_recovery_attempts': 3, 'recovery_attempt_limit': 3, 'terminal': True,
              'received': SECRET, 'sql': SECRET}
    event = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, SECRET, (), None,
        extra={'error_code': 'database_reconnect_failed', 'database_diagnostic': detail})
    with bind_trace(trace):
        handler.handle(event)
    diagnostic = read_worker_log(record)['events'][0]['database']
    assert diagnostic['probe_code'] == 'parameter_binding_unverified'
    assert diagnostic['probe_failure'] == 'readback_mismatch'
    assert diagnostic['category'] == 'qualification'
    assert diagnostic['boundary_recovery_attempts'] == diagnostic['recovery_attempt_limit'] == 3
    assert SECRET not in worker_log_path(record).read_text()
    assert SECRET not in trace.path.read_text()
    detail.update(probe_code=SECRET, probe_failure=SECRET)
    with bind_trace(trace):
        handler.handle(event)
    diagnostic = read_worker_log(record)['events'][-1]['database']
    assert diagnostic.get('probe_code') != SECRET and diagnostic.get('probe_failure') != SECRET
    assert SECRET not in worker_log_path(record).read_text()


def test_windows_long_path_and_symlink_destinations(tmp_path):
    record = tmp_path.joinpath(*(['long_project_' + 'x' * 40] * 5)) / 'run.json'
    sink = WorkerDiagnostics(record)
    sink.emit('worker_started', stage='initializing')
    assert read_worker_log(record)['status'] == 'available'
    assert io_path(worker_log_path(record)).exists()
    external = tmp_path / 'external'
    external.write_text(SECRET)
    other = tmp_path / 'other.json'
    try:
        worker_log_path(other).symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    unsafe = WorkerDiagnostics(other)
    unsafe.emit('worker_started')
    assert unsafe.failed and read_worker_log(other)['status'] == 'unavailable'
    assert external.read_text() == SECRET


def test_manager_retains_exit_and_suppressed_stderr_without_result(tmp_path, monkeypatch):
    workspace = ProjectWorkspace(tmp_path / 'workspace')
    workspace.add('p')
    original = subprocess.Popen
    def child(*args, **kwargs):
        return original([sys.executable, '-c',
            "import sys; sys.stdin.buffer.read(); sys.stderr.write('RuntimeError: " + SECRET + "\\n'); sys.exit(7)"], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace)
    try:
        started = manager.start('p')
        result = wait_run(manager, 'p', started['run_id'])
        assert result['error_code'] == 'worker_result_missing'
        assert result['worker_diagnostics']['status'] == 'available'
        path = manager._paths('p', started['run_id'])[1]
        events = read_worker_log(path)['events']
        assert next(item for item in events if item['code'] == 'worker_process_exited')['exit_code'] == 7
        assert next(item for item in events if item['code'] == 'worker_stderr_summary')['observed_exception_types'] == ['RuntimeError']
        assert SECRET not in worker_log_path(path).read_text()
    finally:
        manager.shutdown()
    assert worker_log_path(path).exists()


def test_worker_records_source_open_failure_and_restores_logging(tmp_path, monkeypatch):
    workspace = ProjectWorkspace(tmp_path / 'workspace')
    project = workspace.add('p')
    logger = logging.getLogger('lightgraph.sources')
    old_handlers, old_level = list(logger.handlers), logger.level
    def fail(*_args, **_kwargs):
        raise RuntimeError(f'password={SECRET} row={SECRET}')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', fail)
    result = project / 'runs' / 'result.json'
    assert execute(_resolved_payload(workspace.configs['p'], project), result) == 2
    event = next(item for item in read_worker_log(result)['events'] if item['code'] == 'worker_error')
    assert event['stage'] == 'opening_sources' and event['exception_type'] == 'RuntimeError'
    assert 'database' not in event
    assert SECRET not in worker_log_path(result).read_text()
    assert logger.handlers == old_handlers and logger.level == old_level


def test_worker_records_handled_driver_errors_and_successful_recovery(tmp_path, monkeypatch):
    workspace = ProjectWorkspace(tmp_path / 'workspace')
    project = workspace.add('p')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *_a, **_kw: [])
    def run(*_args, **_kwargs):
        raw = RuntimeError(SECRET)
        raw.sqlstate = '08006'
        try:
            raise database_error('read_only_transaction_failed', raw, engine='gauss',
                                 stage='snapshot_start', connection_closed=True)
        except LightGraphError:
            pass
        logging.getLogger('lightgraph.sources.database_errors').info('raw %s', SECRET, extra={
            'database_diagnostic': {'engine': 'gauss', 'stage': 'reconnect', 'recovery': 'reconnected'},
            'connection_generation': 2, 'reconnect_attempts': 1})
        return RunReport(run_id='test', status='COMPLETED')
    monkeypatch.setattr('lightgraph.construction.runner.run_construction', run)
    result = project / 'runs' / 'result.json'
    assert execute(_resolved_payload(workspace.configs['p'], project), result) == 0
    events = [item for item in read_worker_log(result)['events'] if item['code'] == 'worker_database_diagnostic']
    assert len(events) == 2
    assert events[0]['database']['connection_closed'] is True
    assert events[1]['database']['recovery'] == 'reconnected'
    assert events[1]['connection_generation'] == 2 and events[1]['reconnect_attempts'] == 1
    assert SECRET not in worker_log_path(result).read_text()


def test_api_read_and_export_are_run_scoped_and_return_safe_snapshot(tmp_path):
    app = create_app(tmp_path / 'workspace')
    with TestClient(app) as client:
        client.post('/api/projects', json={'name': 'p', 'project_id': 'p'})
        rid = 'run_' + 'a' * 24
        project = app.state.workspace.project_path('p')
        record = project / 'runs' / (rid + '.json')
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps({'run_id': rid, 'project_id': 'p', 'status': 'failed', 'created_at': '2026'}))
        sink = WorkerDiagnostics(record)
        sink.emit('worker_process_exited', exit_code=9)
        path = f'/api/projects/p/runs/{rid}/worker-diagnostics'
        response = client.get(path)
        assert response.status_code == 200 and response.json()['events'][0]['exit_code'] == 9
        assert response.json()['active'] is False
        exported = client.get(path + '/export')
        assert exported.status_code == 200 and exported.headers['content-disposition'].endswith('.worker.jsonl"')
        assert json.loads(exported.text)['exit_code'] == 9
        assert client.get(path.replace('/projects/p/', '/projects/other/')).status_code != 200
        assert client.delete(path.replace('/worker-diagnostics', '/trace')).status_code == 428
        assert sink.path.exists()
        etag = client.get('/api/projects/p/config').headers['etag']
        stored = json.loads(record.read_text())
        record.write_text(json.dumps({**stored, 'status': 'running'}))
        assert client.delete(path.replace('/worker-diagnostics', '/trace'), headers={'If-Match': etag}).status_code == 409
        assert sink.path.exists()
        record.write_text(json.dumps(stored))
        cleared = client.delete(path.replace('/worker-diagnostics', '/trace'), headers={'If-Match': etag})
        assert cleared.status_code == 200 and not sink.path.exists()
        assert client.get(path).json()['status'] == 'cleared'
        assert client.get(path + '/export').status_code == 404


def test_worker_log_symlink_blocks_combined_cleanup_before_any_trace_is_removed(tmp_path):
    app = create_app(tmp_path / 'workspace')
    with TestClient(app) as client:
        client.post('/api/projects', json={'name': 'p', 'project_id': 'p'})
        rid = 'run_' + 'b' * 24
        project = app.state.workspace.project_path('p')
        record = project / 'runs' / (rid + '.json')
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps({'run_id': rid, 'project_id': 'p', 'status': 'failed', 'created_at': '2026'}))
        trace = RunTrace(trace_path(record))
        trace('run_started', {})
        outside = tmp_path / 'outside'
        outside.write_text(SECRET)
        try:
            worker_log_path(record).symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip('symlinks unavailable')
        etag = client.get('/api/projects/p/config').headers['etag']
        response = client.delete(f'/api/projects/p/runs/{rid}/trace', headers={'If-Match': etag})
        assert response.status_code == 400
        assert trace.path.exists() and outside.read_text() == SECRET

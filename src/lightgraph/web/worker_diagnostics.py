"""Bounded, value-free worker diagnostics, separate from model/tool transcripts.

Raw stderr is drained but never written. Exceptions retain their type, program
locations and allowlisted driver metadata; messages, SQL and frame locals do not.
"""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import select
import threading
import time

from lightgraph.construction.paths import io_path
from lightgraph.errors import LightGraphError
from lightgraph.sources.database_errors import PROBE_CODES

MAX_LOG_BYTES = 128 * 1024
MAX_LINE_BYTES = 4096
_TAIL_RESERVE = 8 * 1024
_EXIT_RESERVE = 4 * 1024
_MAX_TERMINAL_DATABASE_EVENTS = 4
_PACKAGE = Path(__file__).resolve().parents[1]
_EVENTS = frozenset({'worker_starting', 'worker_started', 'worker_finished', 'worker_error',
    'worker_cleanup_error', 'worker_cancel_error', 'worker_process_exited', 'worker_stderr_summary',
    'worker_start_failed', 'worker_unhandled_error', 'worker_input_invalid', 'worker_log_truncated',
    'worker_database_diagnostic'})
_FINAL_EVENTS = frozenset({'worker_finished', 'worker_error', 'worker_cleanup_error',
    'worker_process_exited', 'worker_stderr_summary', 'worker_start_failed', 'worker_unhandled_error'})
_STAGES = frozenset({'launch', 'initializing', 'opening_sources', 'construction', 'cleanup', 'result', 'finished'})
_EXCEPTIONS = frozenset({'Exception', 'LightGraphError', 'BudgetExceeded', 'RuntimeError',
    'ValueError', 'TypeError', 'KeyError', 'AttributeError', 'AssertionError', 'OSError',
    'PermissionError', 'FileNotFoundError', 'TimeoutError', 'ConnectionError', 'MemoryError',
    'ImportError', 'ModuleNotFoundError', 'SyntaxError', 'KeyboardInterrupt', 'SystemExit',
    'BrokenPipeError', 'ConnectionResetError', 'OperationalError', 'InterfaceError',
    'DatabaseError', 'ProgrammingError', 'IntegrityError', 'InternalError', 'NotSupportedError'})
_ERRORS = frozenset({'worker_execution_failed', 'worker_start_failed', 'worker_result_missing',
    'input_dependency_changed', 'run_cancelled', 'run_force_stopped', 'database_connection_failed',
    'database_snapshot_failed', 'query_execution_failed', 'snapshot_or_projection_unavailable',
    'query_timeout', 'query_cancelled', 'runtime_capability_unverified', 'legacy_rule_input_removed',
    'read_only_transaction_failed', 'snapshot_close_failed', 'source_catalog_changed',
    'database_reconnect_failed', 'source_closed', 'source_close_failed', 'query_cancel_failed',
    'source_circuit_open', 'source_retry_limit_exhausted',
    'database_preflight_failed', 'database_runtime_preflight_failed', 'read_only_transaction_unverified',
    'parameter_binding_unverified', 'distinct_projection_unverified',
    'capability_manifest_binding_mismatch', 'qualified_capability_manifest_required',
    'authorized_table_unavailable_or_view', 'authorized_column_unavailable', 'invalid_capability_manifest'}) | PROBE_CODES
_EXCEPTION_LINE = re.compile(rb'^(?:[a-zA-Z_][a-zA-Z0-9_.]*\.)?([A-Za-z]+Error|KeyboardInterrupt|SystemExit):')


def worker_log_path(record_path):
    path = Path(record_path)
    stem = path.name.removesuffix('.json').removesuffix('.result')
    return path.with_name(stem + '.worker.jsonl')


def worker_log_artifacts(record_path):
    path = worker_log_path(record_path)
    return path, path.with_suffix('.status.json')


def check_worker_log_paths(record_path):
    """Check every artifact before an explicit cleanup deletes any files."""
    try:
        for path in worker_log_artifacts(record_path):
            _check_path(path)
    except OSError:
        raise LightGraphError('workspace_path_invalid') from None


def _check_path(path):
    for item in (path, *path.parents):
        if io_path(item).is_symlink():
            raise OSError('worker_log_path_invalid')


def _safe_event(value):
    if not isinstance(value, dict) or value.get('code') not in _EVENTS:
        return None
    result = {'code': value['code']}
    timestamp = value.get('time')
    if isinstance(timestamp, str) and len(timestamp) <= 40:
        try:
            if datetime.fromisoformat(timestamp).tzinfo is not None:
                result['time'] = timestamp
        except ValueError:
            pass
    if value.get('stage') in _STAGES:
        result['stage'] = value['stage']
    if value.get('exception_type') in _EXCEPTIONS:
        result['exception_type'] = value['exception_type']
    if 'error_code' in value:
        result['error_code'] = value['error_code'] if value['error_code'] in _ERRORS else 'other_application_error'
    for name in ('stderr_bytes', 'stderr_lines', 'oversized_lines', 'connection_generation', 'reconnect_attempts'):
        if type(value.get(name)) is int and 0 <= value[name] <= 2**63 - 1:
            result[name] = value[name]
    if type(value.get('exit_code')) is int and -(2**31) <= value['exit_code'] <= 2**32 - 1:
        result['exit_code'] = value['exit_code']
    for name in ('forced', 'cancel_requested', 'fatal_python_error', 'stderr_complete'):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    if isinstance(value.get('observed_exception_types'), list):
        result['observed_exception_types'] = sorted({name for name in value['observed_exception_types']
            if isinstance(name, str) and name in _EXCEPTIONS})
    frames = []
    for frame in value.get('frames', [])[:12] if isinstance(value.get('frames'), list) else []:
        if not isinstance(frame, dict):
            continue
        name, line = frame.get('file'), frame.get('line')
        if (isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_/]+\.py', name)
                and type(line) is int and 0 < line < 10**7):
            frames.append({'file': name, 'line': line})
    if frames:
        result['frames'] = frames
    if isinstance(value.get('database'), dict):
        from lightgraph.sources.database_errors import safe_database_diagnostic
        carrier = LightGraphError('worker_execution_failed')
        carrier.database_diagnostic = value['database']
        result['database'] = safe_database_diagnostic(carrier)
    return result


def exception_details(error):
    """Never format an exception: driver text can contain credentials and rows."""
    name = type(error).__name__
    result = {'exception_type': name if name in _EXCEPTIONS else 'Exception'}
    frames, trace = [], error.__traceback__
    while trace is not None:
        try:
            relative = Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(_PACKAGE)
            frames.append({'file': relative.as_posix(), 'line': trace.tb_lineno})
        except (OSError, ValueError):
            pass
        trace = trace.tb_next
    result['frames'] = frames[-12:]
    from lightgraph.sources.database_errors import safe_database_diagnostic, database_diagnostic
    diagnostic, cause, visited = None, error, set()
    from lightgraph.sources.gauss_compat import sqlstate
    while cause is not None and id(cause) not in visited and len(visited) < 8:
        visited.add(id(cause))
        diagnostic = safe_database_diagnostic(cause)
        module = type(cause).__module__
        engine = 'mysql' if module.startswith('pymysql.') else 'gauss' if module.startswith(('psycopg.', 'psycopg2.')) else 'unknown'
        # A real driver code can add evidence. Missing codes cannot prove a
        # network fault; OperationalError alone is deliberately not classified.
        if diagnostic is None and (sqlstate(cause) or engine != 'unknown'):
            diagnostic = database_diagnostic(cause, engine=engine, stage='unknown')
        if diagnostic is not None:
            break
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    if diagnostic is not None:
        result['database'] = diagnostic
    return result


class WorkerDiagnostics:
    def __init__(self, record_path):
        self.path = worker_log_path(record_path)
        self.lock = threading.Lock()
        self.failed = False
        self._terminal_database_seen = set()

    def emit(self, code, *, error=None, **details):
        try:
            event = _safe_event({'code': code, 'time': datetime.now(timezone.utc).isoformat(),
                                **(exception_details(error) if error is not None else {}), **details})
            if event is None:
                return
            terminal_database = (code == 'worker_database_diagnostic'
                                 and event.get('database', {}).get('terminal') is True)
            with self.lock:
                if terminal_database:
                    # A stopped source may be returned by many subsequent tools.
                    # Keep distinct terminal causes without spending the exit reserve.
                    signature = json.dumps({key: event[key] for key in
                        ('error_code', 'database', 'connection_generation') if key in event}, sort_keys=True)
                    if (signature in self._terminal_database_seen
                            or len(self._terminal_database_seen) >= _MAX_TERMINAL_DATABASE_EVENTS):
                        return
                    self._terminal_database_seen.add(signature)
                _check_path(self.path)
                path = io_path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                _check_path(self.path)
                encoded = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(path, flags, 0o600)
                try:
                    size = os.fstat(fd).st_size
                    limit = (MAX_LOG_BYTES - _EXIT_RESERVE if terminal_database else
                             MAX_LOG_BYTES if code in _FINAL_EVENTS else MAX_LOG_BYTES - _TAIL_RESERVE)
                    if size + len(encoded) > limit:
                        if terminal_database:
                            # The trace has its own budget. A full worker log must
                            # not hide the actual final database cause from it.
                            encoded = b''
                        else:
                            if size > MAX_LOG_BYTES - _TAIL_RESERVE:
                                return
                            event = {'code': 'worker_log_truncated', 'time': event['time']}
                            encoded = (json.dumps(event, separators=(',', ':')) + '\n').encode()
                    if size + len(encoded) <= MAX_LOG_BYTES:
                        remaining = memoryview(encoded)
                        while remaining:
                            written = os.write(fd, remaining)
                            if not written:
                                raise OSError('worker_log_write_failed')
                            remaining = remaining[written:]
                finally:
                    os.close(fd)
            from lightgraph.runtime.trace import emit_trace
            emit_trace('worker_diagnostic', event)
        except Exception:
            self.failed = True  # Optional diagnostics cannot fail extraction.
            try:
                from lightgraph.runtime.retention import atomic_text
                status = self.path.with_suffix('.status.json')
                _check_path(status)
                atomic_text(io_path(status), json.dumps({'error_code': 'worker_diagnostic_write_failed'}))
            except Exception:
                pass


class DatabaseDiagnosticHandler(logging.Handler):
    """Capture structured driver outcomes even when tools handle the error.

    Never call getMessage/format: logger arguments and exc_info can contain raw
    driver text. Only the centralized driver's allowlisted extra fields count.
    """
    def __init__(self, sink, stage):
        super().__init__(level=logging.INFO)
        self.sink, self.stage = sink, stage

    def emit(self, record):
        database = getattr(record, 'database_diagnostic', None)
        if not isinstance(database, dict):
            return
        details = {'database': database, 'stage': self.stage()}
        for name in ('error_code', 'connection_generation', 'reconnect_attempts'):
            if hasattr(record, name):
                details[name] = getattr(record, name)
        self.sink.emit('worker_database_diagnostic', **details)


def read_worker_log(record_path):
    path = worker_log_path(record_path)
    try:
        _check_path(path)
        status = path.with_suffix('.status.json')
        _check_path(status)
        write_failed = False
        if io_path(status).exists():
            with io_path(status).open('rb') as stream:
                raw_status = stream.read(257)
            write_failed = len(raw_status) > 256 or json.loads(raw_status).get('error_code') == 'worker_diagnostic_write_failed'
        if not io_path(path).exists():
            from lightgraph.runtime.trace import trace_path
            cleared = trace_path(record_path).with_suffix('.cleared.json')
            _check_path(cleared)
            return {'status': 'unavailable' if write_failed else 'cleared' if io_path(cleared).exists() else 'not_recorded', 'events': [],
                    'incomplete': write_failed,
                    **({'error_code': 'worker_diagnostic_write_failed'} if write_failed else {})}
        with io_path(path).open('rb') as stream:
            raw = stream.read(MAX_LOG_BYTES + 1)
        if len(raw) > MAX_LOG_BYTES:
            raise ValueError
        events = []
        incomplete = bool(raw and not raw.endswith(b'\n'))
        for line in raw.splitlines():
            try:
                event = _safe_event(json.loads(line))
                if event is None:
                    raise ValueError
                events.append(event)
            except (ValueError, TypeError):
                incomplete = True
        return {'status': 'available', 'events': events, 'incomplete': incomplete or write_failed or
                any(event['code'] == 'worker_log_truncated' for event in events),
                **({'error_code': 'worker_diagnostic_write_failed'} if write_failed else {})}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {'status': 'unavailable', 'events': [], 'incomplete': True}


def read_worker_diagnostics(manager, pid, rid):
    record = manager.get(pid, rid)
    _, path = manager._paths(pid, rid)
    return {'run_id': rid, 'active': record['status'] in {'queued', 'running'}, **read_worker_log(path)}


def export_worker_diagnostics(manager, pid, rid):
    # Copy and validate a bounded snapshot before responding; no open-reader
    # lease or platform-specific file replacement behavior is required.
    result = read_worker_diagnostics(manager, pid, rid)
    if result['status'] in {'not_recorded', 'cleared'}:
        raise LightGraphError('run_trace_not_found')
    if result['status'] != 'available':
        raise LightGraphError('run_trace_invalid')
    return ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in result['events'])


class StderrCollector:
    """Drain pipes with a bounded line buffer and stoppable cross-platform reads."""
    def __init__(self, stream):
        self.stream = stream
        self.stop = threading.Event()
        self.stopped_at = None
        self.summary = {'stderr_bytes': 0, 'stderr_lines': 0, 'oversized_lines': 0,
                        'observed_exception_types': [], 'fatal_python_error': False, 'stderr_complete': False}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @staticmethod
    def _available(fd):
        if os.name != 'nt':
            return 8192 if select.select([fd], [], [], .05)[0] else 0
        import ctypes
        import msvcrt
        from ctypes import wintypes
        available = wintypes.DWORD()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        peek.restype = wintypes.BOOL
        if not peek(wintypes.HANDLE(msvcrt.get_osfhandle(fd)), None, 0, None, ctypes.byref(available), None):
            return -1 if ctypes.get_last_error() in {109, 232} else -2
        return min(8192, available.value)

    def _line(self, prefix):
        self.summary['stderr_lines'] += 1
        if prefix.startswith(b'Fatal Python error:'):
            self.summary['fatal_python_error'] = True
        match = _EXCEPTION_LINE.match(prefix)
        if match:
            name = match[1].decode('ascii')
            if name in _EXCEPTIONS and name not in self.summary['observed_exception_types']:
                self.summary['observed_exception_types'].append(name)

    def _run(self):
        pending = bytearray()
        oversized = False
        try:
            fd = self.stream.fileno()
            while True:
                if self.stop.is_set() and self.stopped_at is not None and time.monotonic() - self.stopped_at > .5:
                    break
                count = self._available(fd)
                if count < 0:
                    self.summary['stderr_complete'] = count == -1
                    break
                if not count:
                    if self.stop.is_set():
                        break
                    self.stop.wait(.05)
                    continue
                block = os.read(fd, count)
                if not block:
                    self.summary['stderr_complete'] = True
                    break
                self.summary['stderr_bytes'] += len(block)
                for part in block.splitlines(keepends=True):
                    room = MAX_LINE_BYTES - len(pending)
                    pending.extend(part[:room])
                    if len(part) > room and not oversized:
                        self.summary['oversized_lines'] += 1
                        oversized = True
                    if part.endswith((b'\n', b'\r')):
                        self._line(pending)
                        pending.clear()
                        oversized = False
            if pending:
                self._line(pending)
        except Exception:
            pass
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def finish(self):
        self.stopped_at = time.monotonic()
        self.stop.set()
        self.thread.join(timeout=1)
        return dict(self.summary, observed_exception_types=sorted(self.summary['observed_exception_types']))

"""Bounded runtime telemetry: fixed event codes and counts, never source rows.

The optional sink belongs to one worker. A failed telemetry write cannot change
extraction results; readers independently validate the bounded snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
import time

from .retention import atomic_text

STAGES = frozenset({'initializing', 'metadata', 'discovery', 'validation', 'relations',
                    'extraction', 'publishing', 'cleanup', 'finished'})
EVENT_CODES = frozenset({
    'run_started', 'run_completed', 'run_completed_with_gaps', 'run_partial',
    'run_failed', 'run_cancelled', 'run_interrupted', 'stage_started', 'stage_completed',
    'source_open_started', 'source_open_completed', 'table_started', 'table_completed',
    'rule_started', 'rule_completed', 'rule_rejected', 'model_call_started',
    'model_call_completed', 'model_call_failed', 'publication_started',
    'publication_completed', 'cleanup_started', 'cleanup_completed', 'issue_detected',
})
COUNTERS = frozenset({'model_calls', 'input_tokens', 'output_tokens', 'model_failures',
    'tables_completed', 'tables_total', 'rules_completed', 'rules_total',
    'published_rules', 'db_queries', 'business_nodes', 'business_edges'})
UNITS = frozenset({'tables', 'rules', 'documents'})
LEVELS = frozenset({'info', 'warning', 'error'})
MAX_EVENTS = 200
MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_COUNT = 10**15
_IDENTIFIER = re.compile(r'^[\w. :\-]{1,256}$', re.UNICODE)
_RUN_ID = re.compile(r'^run_[0-9a-f]{24}$')
_sink = ContextVar('lightgraph_progress_sink', default=None)


def _count(value):
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else None


def _counters(values):
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in COUNTERS and _count(value) is not None}


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _event(stage, code, *, completed=None, total=None, unit=None, table=None,
           rule_id=None, level='info', counters=None):
    if stage is not None and stage not in STAGES or code not in EVENT_CODES:
        return None
    result = {'stage': stage, 'code': code, 'level': level if level in LEVELS else 'info'}
    for name, value in (('completed', completed), ('total', total)):
        if _count(value) is not None:
            result[name] = value
    if unit in UNITS:
        result['unit'] = unit
    for name, value in (('table', table), ('rule_id', rule_id)):
        if _identifier(value) is not None:
            result[name] = value
    safe_counts = _counters(counters)
    if safe_counts:
        result['counters'] = safe_counts
    return result


@contextmanager
def bind_progress(sink):
    """Bind telemetry to this execution context; nested bindings restore cleanly."""
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)


def emit_progress(stage, code, *, completed=None, total=None, unit=None, table=None,
                  rule_id=None, level='info', counters=None):
    """Emit a fixed event. None stage preserves the current worker phase."""
    from .trace import emit_trace
    emit_trace('progress', {'stage':stage,'code':code,'completed':completed,'total':total,
        'unit':unit,'table':table,'rule_id':rule_id,'level':level,'counters':counters})
    sink = _sink.get()
    if sink is None:
        return
    try:
        event = _event(stage, code, completed=completed, total=total, unit=unit,
                       table=table, rule_id=rule_id, level=level, counters=counters)
        if event is not None:
            sink(event)
    except Exception:
        # Telemetry is best-effort and cannot turn a verified publication into
        # an extraction failure (e.g. transient file replacement on Windows).
        pass


def progress_path(record_path):
    """Sibling paths avoid platform-dependent string splitting on separators."""
    path = Path(record_path)
    stem = path.name.removesuffix('.json').removesuffix('.result')
    return path.with_name(stem + '.progress.json')


class RunProgress:
    def __init__(self, path):
        self.path = Path(path)
        self.run_id = self.path.name.removesuffix('.progress.json')
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.snapshot = {'run_id': self.run_id, 'stage': 'initializing', 'completed': None,
            'total': None, 'unit': None, 'updated_at': None, 'elapsed_seconds': 0,
            'counters': {}, 'events': [], 'dropped_events': 0}
        self.sequence = 0

    def __call__(self, event):
        event = _event(**{key: value for key, value in event.items()
                         if key in {'stage', 'code', 'completed', 'total', 'unit', 'table',
                                    'rule_id', 'level', 'counters'}})
        if event is None:
            return
        with self.lock:
            state = self.snapshot
            stage = event['stage'] or state['stage']
            if stage != state['stage'] or event['code'] == 'stage_started':
                state.update(stage=stage, completed=None, total=None, unit=None)
            event['stage'] = stage
            for key in ('completed', 'total', 'unit'):
                if key in event:
                    state[key] = event[key]
            # Never invent a completion percentage from incompatible counts.
            if state['total'] is not None and state['completed'] is not None and state['completed'] > state['total']:
                state.update(completed=None, total=None, unit=None)
            state['counters'].update(event.get('counters', {}))
            self.sequence += 1
            now = datetime.now(timezone.utc).isoformat()
            event.update(seq=self.sequence, time=now)
            state['events'].append(event)
            if len(state['events']) > MAX_EVENTS:
                state['events'].pop(0)
                state['dropped_events'] += 1
            state.update(updated_at=now, elapsed_seconds=round(max(0, time.monotonic()-self.started), 3))
            serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
            while len(serialized.encode('utf-8')) > MAX_SNAPSHOT_BYTES and state['events']:
                state['events'].pop(0)
                state['dropped_events'] += 1
                serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
            if self.path.is_symlink() or self.path.parent.is_symlink():
                return
            atomic_text(self.path, serialized)


def read_progress(path, run_id):
    """Malformed/missing telemetry is optional, never a failed run-record read."""
    path = Path(path)
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('run_id') != run_id or value.get('stage') not in STAGES:
            return None
        events = value.get('events')
        elapsed = value.get('elapsed_seconds')
        if (not isinstance(events, list) or len(events) > MAX_EVENTS or
                type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= _MAX_COUNT or
                _time(value.get('updated_at')) is None or _count(value.get('dropped_events')) is None):
            return None
        safe_events = []
        previous = 0
        for item in events:
            if not isinstance(item, dict) or item.get('stage') not in STAGES:
                return None
            event = _event(item['stage'], item.get('code'), **{key: item[key] for key in
                ('completed', 'total', 'unit', 'table', 'rule_id', 'level', 'counters') if key in item})
            sequence = _count(item.get('seq'))
            timestamp = _time(item.get('time'))
            if event is None or sequence is None or sequence <= previous or timestamp is None:
                return None
            previous = sequence
            event.update(seq=sequence, time=timestamp)
            safe_events.append(event)
        completed, total = _count(value.get('completed')), _count(value.get('total'))
        if completed is not None and total is not None and completed > total:
            completed = total = None
        return {'stage': value['stage'], 'completed': completed, 'total': total,
            'unit': value.get('unit') if value.get('unit') in UNITS else None,
            'updated_at': value['updated_at'], 'elapsed_seconds': elapsed,
            'counters': _counters(value.get('counters')), 'events': safe_events,
            'dropped_events': value['dropped_events']}
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def finish_progress(path, run_id, status):
    """After child exit, reconcile forced cancellation/recovery with its last event."""
    if status not in {'completed', 'completed_with_gaps', 'partial', 'failed', 'cancelled', 'interrupted'}:
        return
    try:
        state = read_progress(path, run_id)
        if state is None:
            return
        code = 'run_' + status
        if state['events'] and state['events'][-1]['code'] == code:
            return
        sink = RunProgress(path)
        sink.snapshot = dict(state, run_id=run_id)
        sink.sequence = state['events'][-1]['seq'] if state['events'] else state['dropped_events']
        sink.started -= state['elapsed_seconds']
        sink({'stage': 'finished', 'code': code,
              'level': 'error' if status == 'failed' else 'warning' if status in {'partial', 'cancelled', 'interrupted'} else 'info'})
    except Exception:
        pass

"""Read and explicitly clear local run traces without loading whole exports."""
from collections import OrderedDict
import json
import threading

from fastapi.responses import StreamingResponse

from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text

_readers = {}
_reader_lock = threading.RLock()


def _paths(manager, pid, rid):
    record = manager.get(pid, rid)
    _, record_path = manager._paths(pid, rid)
    trace = record_path.with_name(rid + '.trace.jsonl')
    paths = (trace, trace.with_suffix('.index.jsonl'), trace.with_suffix('.status.json'),
             trace.with_suffix('.cleared.json'))
    if any(path.is_symlink() for path in paths):
        raise LightGraphError('workspace_path_invalid')
    return record, paths


# The archive remains complete; filtering is a read-time presentation choice.
AGENT_EVENTS = frozenset({
    'agent_start', 'agent_end', 'agent_started', 'agent_completed', 'agent_finished',
    'model_request', 'model_response', 'model_error',
    'tool_request', 'tool_call', 'tool_result', 'tool_error',
    'proposal_submitted', 'proposal_result', 'discovery_phase_feedback',
})
_kind_caches = OrderedDict()
_kind_lock = threading.RLock()


def _legacy_kinds(trace):
    """Cache only event kinds for old indexes; appended archives reuse prior work."""
    stat = trace.stat()
    stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    key = str(trace)
    with _kind_lock:
        previous = _kind_caches.pop(key, None)
        if previous and previous[0][:2] == stamp[:2] and (
            stamp[2] > previous[0][2] or stamp[2:] == previous[0][2:]
        ):
            kinds = previous[1]
        else:
            kinds = OrderedDict()
        _kind_caches[key] = (stamp, kinds)
        while len(_kind_caches) > 32:
            _kind_caches.popitem(last=False)
        return kinds


def _read_event(stream, item):
    stream.seek(item['offset'])
    try:
        event = json.loads(stream.read(item['length']))
        if not isinstance(event, dict) or event.get('seq') != item['seq'] or not isinstance(event.get('kind'), str):
            raise ValueError
    except (ValueError, TypeError):
        raise LightGraphError('run_trace_invalid') from None
    return event


def _index(trace, index, *, offset=0, limit=0, view='all'):
    """Read committed metadata and page within a view, retaining original seq IDs.

    New writers index kinds directly. Legacy indexes decode each event once and
    retain only its kind, never message or sample contents, in a bounded cache.
    """
    if not trace.exists() or not index.exists():
        return [],0,0,0
    size = trace.stat().st_size
    entries = []
    total = selected = end = 0
    kinds = _legacy_kinds(trace) if view != 'all' else None
    with index.open('rb') as stream, trace.open('rb') as archive:
        for line in stream:
            if not line.endswith(b'\n'):
                break
            try:
                item = json.loads(line)
                if (set(item) not in ({'seq','offset','length'}, {'seq','offset','length','kind'}) or
                    any(type(item[key]) is not int for key in ('seq','offset','length')) or
                    ('kind' in item and not isinstance(item['kind'], str)) or
                    item['seq'] != total + 1 or item['length'] <= 0 or item['offset'] != end):
                    raise ValueError
            except (ValueError, TypeError):
                raise LightGraphError('run_trace_invalid') from None
            if item['offset'] + item['length'] > size:
                break
            include = True
            if view != 'all':
                kind = item.get('kind')
                if kind is None:
                    cache_key = (item['seq'], item['offset'], item['length'])
                    with _kind_lock:
                        kind = kinds.get(cache_key)
                    if kind is None:
                        kind = _read_event(archive, item)['kind']
                        with _kind_lock:
                            # Keep a stable prefix at the cap. Evicting the
                            # oldest while scanning from seq=1 would thrash and
                            # force a full reread on every poll of a large file.
                            if len(kinds) < 40000:
                                kinds[cache_key] = kind
                include = (kind in AGENT_EVENTS) == (view == 'agent')
            if include:
                if offset <= selected < offset+limit:
                    entries.append(item)
                selected += 1
            total += 1
            end = item['offset'] + item['length']
    return entries,selected,end,total


def read_run_trace(manager, pid, rid, *, offset=0, limit=10, view='all'):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50 or view not in {'all','agent','runtime'}:
        raise LightGraphError('run_trace_page_invalid')
    record, (trace, index, status_path, cleared) = _paths(manager, pid, rid)
    entries,total,committed_end,recorded_total = _index(trace, index,offset=offset,limit=limit,view=view)
    status = 'available' if trace.exists() else 'cleared' if cleared.exists() else 'not_recorded'
    error_code = None
    if status_path.exists():
        try:
            with status_path.open(encoding='utf-8') as stream:
                state = json.load(stream)
            if state.get('error_code'): error_code = 'trace_write_failed'
        except (OSError, ValueError, TypeError):
            error_code = 'run_trace_invalid'
    active = record['status'] in {'queued','running'}
    if trace.exists() and not active and trace.stat().st_size != committed_end:
        error_code = error_code or 'run_trace_incomplete'
    events = []
    if entries:
        with trace.open('rb') as stream:
            for item in entries:
                events.append(_read_event(stream, item))
    return {'events': events, 'next_offset': offset+limit if offset+limit<total else None,
        'total_events':total, 'recorded_events':recorded_total, 'view':view, 'status':status, 'active':active,
        'incomplete': bool(error_code), 'error_code':error_code}


def export_run_trace(manager, pid, rid):
    _, (trace, index, _, _) = _paths(manager, pid, rid)
    if not trace.exists(): raise LightGraphError('run_trace_not_found')
    _,_,end,_ = _index(trace, index)
    # Open under the project lock. Deletion cannot swap in a different file
    # between authorization and iteration; exports are a committed snapshot.
    return TraceDownload(trace,end)


class TraceDownload:
    """A reader lease makes concurrent deletion safe on Windows as well."""
    def __init__(self,path,end):
        self.path,self.remaining=path,end
        with _reader_lock:
            self.stream=path.open('rb')
            _readers[path]=_readers.get(path,0)+1

    def __iter__(self): return self

    def __next__(self):
        if self.remaining<=0:
            self.close()
            raise StopIteration
        try:
            block=self.stream.read(min(256*1024,self.remaining))
            if not block:raise StopIteration
            self.remaining-=len(block)
            return block
        except BaseException:
            self.close()
            raise

    def close(self):
        with _reader_lock:
            if not self.stream.closed:
                self.stream.close()
                _readers[self.path]-=1
                if not _readers[self.path]: _readers.pop(self.path)


class TraceResponse(StreamingResponse):
    def __init__(self,download,**kwargs):
        self.download=download
        super().__init__(download,**kwargs)

    async def __call__(self,scope,receive,send):
        try:await super().__call__(scope,receive,send)
        finally:self.download.close()


def clear_run_trace(manager, pid, rid):
    record, paths = _paths(manager, pid, rid)
    if record['status'] in {'queued','running'}:
        raise LightGraphError('run_trace_active')
    trace, index, status, cleared = paths
    # This operation is only exposed by the explicit, revision-checked DELETE.
    with _reader_lock:
        if _readers.get(trace):raise LightGraphError('run_trace_export_active')
        with _kind_lock:
            _kind_caches.pop(str(trace), None)
        for path in (trace, index, status):
            path.unlink(missing_ok=True)
    atomic_text(cleared, json.dumps({'run_id':rid,'cleared':True}))
    return read_run_trace(manager,pid,rid)

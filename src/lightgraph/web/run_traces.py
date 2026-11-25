"""Read and explicitly clear local run traces without loading whole exports."""
from collections import OrderedDict
import json
import threading

from fastapi.responses import StreamingResponse

from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from lightgraph.construction.trace import reclassify_trace_event
from .workspace import _no_symlinks

_readers = {}
_reader_lock = threading.RLock()


def _trace_files(manager, pid, rid):
    _, record_path = manager._paths(pid, rid)
    trace = record_path.with_name(rid + '.trace.jsonl')
    paths = (trace, trace.with_suffix('.index.jsonl'), trace.with_suffix('.status.json'),
             trace.with_suffix('.cleared.json'))
    try:
        for path in paths:
            _no_symlinks(path)
    except LightGraphError:
        raise LightGraphError('workspace_path_invalid') from None
    return paths


def _paths(manager, pid, rid):
    return manager.get(pid, rid, view='summary'), _trace_files(manager, pid, rid)


# The archive remains complete; filtering is a read-time presentation choice.
AGENT_EVENTS = frozenset({
    'agent_start', 'agent_end', 'agent_started', 'agent_completed', 'agent_finished',
    'model_request', 'model_response', 'model_error',
    'tool_request', 'tool_call', 'tool_result', 'tool_error',
    'proposal_submitted', 'proposal_result', 'discovery_phase_feedback',
})
_kind_caches = OrderedDict()
_kind_lock = threading.RLock()
# The cache contains numeric file positions and counts, never event payloads.
_page_indexes = OrderedDict()
_page_lock = threading.Lock()


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
    return reclassify_trace_event(event)


def _index(trace, index, *, offset=0, limit=0, view='all'):
    """Read committed metadata and page within a view, retaining original seq IDs.

    New writers index kinds directly. Legacy indexes decode each event once and
    retain only its kind, never message or sample contents, in a bounded cache.
    """
    if not trace.exists() or not index.exists():
        return [],0,0,0
    def stamp(path):
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    signature = stamp(trace), stamp(index)
    size = signature[0][2]
    key = str(trace), view
    with _page_lock:
        cached = _page_indexes.get(key)
        if cached and cached[0] == signature:
            _page_indexes.move_to_end(key)
        else:
            cached = None
    # At most a few thousand anchors per archive even for gigabyte indexes.
    stride = max(128, signature[1][2] // 4096)
    anchors = []
    start = selected = end = total = 0
    if cached:
        _, anchors, count, committed, recorded = cached
        if not limit or offset >= count:
            return [], count, committed, recorded
        start, total, end = anchors[offset // stride]
        selected = offset // stride * stride
    kinds = _legacy_kinds(trace) if view != 'all' else None
    entries = []
    with index.open('rb') as stream, trace.open('rb') as archive:
        stream.seek(start)
        position = start
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
                if kind is None or kind == 'tool_result':
                    cache_key = (item['seq'], item['offset'], item['length'])
                    with _kind_lock:
                        kind = kinds.get(cache_key)
                    if kind is None:
                        kind = _read_event(archive, item)['kind']
                        with _kind_lock:
                            if len(kinds) < 40000:
                                kinds[cache_key] = kind
                include = (kind in AGENT_EVENTS) == (view == 'agent')
            if include:
                if not cached and selected % stride == 0:
                    anchors.append((position, total, end))
                if offset <= selected < offset+limit:
                    entries.append(item)
                selected += 1
                if cached and selected >= min(offset+limit, count):
                    break
            total += 1
            end = item['offset'] + item['length']
            position += len(line)
    if cached:
        return entries, count, committed, recorded
    if signature == (stamp(trace), stamp(index)):
        with _page_lock:
            _page_indexes[key] = signature, anchors, selected, end, total
            _page_indexes.move_to_end(key)
            while len(_page_indexes) > 32:
                _page_indexes.popitem(last=False)
    return entries,selected,end,total


def _event_summary(event):
    # Match tracePresentation's labels without returning full prompts or tool results.
    data = event.get('data')
    summary = {}
    if isinstance(data, dict):
        for field in ('task', 'discovery_step', 'max_discovery_steps', 'attempt', 'dispatch_index',
                      'harness_turn', 'harness_step', 'call_id', 'name'):
            value = data.get(field)
            if isinstance(value, (str, int)):
                summary[field] = value[:256] if isinstance(value, str) else value
        call = data.get('call')
        if not summary.get('name') and isinstance(call, dict) and isinstance(call.get('name'), str):
            summary['name'] = call['name'][:256]
    return {key: event.get(key) for key in ('seq', 'kind', 'time')} | {'data': summary}


def read_run_trace(manager, pid, rid, *, offset=0, limit=10, view='all', summary=False, node_id=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50 or view not in {'all','agent','runtime'}:
        raise LightGraphError('run_trace_page_invalid')
    binding = {}
    if node_id is None:
        record, files = _paths(manager, pid, rid)
        active = record['status'] in {'queued', 'running'}
    else:
        binding, active = manager.node_trace_binding(pid, rid, node_id)
        files = _trace_files(manager, pid, binding['run_id'])
    trace, index, status_path, cleared = files
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
    if trace.exists() and not active and trace.stat().st_size != committed_end:
        error_code = error_code or 'run_trace_incomplete'
    events = []
    if entries:
        with trace.open('rb') as stream:
            for item in entries:
                event = _read_event(stream, item)
                events.append(_event_summary(event) if summary else event)
    return {**binding, 'events': events, 'next_offset': offset+limit if offset+limit<total else None,
        'total_events':total, 'recorded_events':recorded_total, 'view':view, 'status':status, 'active':active,
        'incomplete': bool(error_code), 'error_code':error_code}


def export_run_trace(manager, pid, rid):
    _, (trace, index, _, _) = _paths(manager, pid, rid)
    if not trace.exists(): raise LightGraphError('run_trace_not_found')
    _,_,end,_ = _index(trace, index)
    # Open under the project lock. Deletion cannot swap in a different file
    # between authorization and iteration; exports are a committed snapshot.
    return TraceDownload(trace,end)


def ensure_no_project_downloads(directory):
    with _reader_lock:
        if any(path.is_relative_to(directory) for path in _readers):
            raise LightGraphError('run_trace_export_active')


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
    from .worker_diagnostics import check_worker_log_paths, worker_log_artifacts
    from lightgraph.construction.paths import io_path
    _, record_path = manager._paths(pid, rid)
    check_worker_log_paths(record_path)
    # This operation is only exposed by the explicit, revision-checked DELETE.
    with _reader_lock:
        if _readers.get(trace):raise LightGraphError('run_trace_export_active')
        with _kind_lock:
            _kind_caches.pop(str(trace), None)
        with _page_lock:
            for view in ('all', 'agent', 'runtime'):
                _page_indexes.pop((str(trace), view), None)
        for path in (trace, index, status):
            path.unlink(missing_ok=True)
        for path in worker_log_artifacts(record_path):
            io_path(path).unlink(missing_ok=True)
    atomic_text(cleared, json.dumps({'run_id':rid,'cleared':True}))
    return read_run_trace(manager,pid,rid)

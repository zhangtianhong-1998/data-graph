"""Read and explicitly clear local run traces without loading whole exports."""
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


def _index(trace, index, *, offset=0, limit=0):
    """Only fsynced, newline-terminated index entries expose committed events."""
    if not trace.exists() or not index.exists():
        return [],0,0
    size = trace.stat().st_size
    entries = []
    total = end = 0
    with index.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                break
            try:
                item = json.loads(line)
                if (set(item) != {'seq','offset','length'} or
                    any(type(item[key]) is not int for key in item) or
                    item['seq'] != total + 1 or item['length'] <= 0 or item['offset'] != end):
                    raise ValueError
            except (ValueError, TypeError):
                raise LightGraphError('run_trace_invalid') from None
            if item['offset'] + item['length'] > size:
                break
            if offset <= total < offset+limit: entries.append(item)
            total += 1
            end = item['offset'] + item['length']
    return entries,total,end


def read_run_trace(manager, pid, rid, *, offset=0, limit=10):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise LightGraphError('run_trace_page_invalid')
    record, (trace, index, status_path, cleared) = _paths(manager, pid, rid)
    entries,total,committed_end = _index(trace, index,offset=offset,limit=limit)
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
                stream.seek(item['offset'])
                try:
                    event = json.loads(stream.read(item['length']))
                    if not isinstance(event, dict) or event.get('seq') != item['seq']:
                        raise ValueError
                except (ValueError, TypeError):
                    raise LightGraphError('run_trace_invalid') from None
                events.append(event)
    return {'events': events, 'next_offset': offset+limit if offset+limit<total else None,
        'total_events':total, 'status':status, 'active':active,
        'incomplete': bool(error_code), 'error_code':error_code}


def export_run_trace(manager, pid, rid):
    _, (trace, index, _, _) = _paths(manager, pid, rid)
    if not trace.exists(): raise LightGraphError('run_trace_not_found')
    _,_,end = _index(trace, index)
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
        for path in (trace, index, status):
            path.unlink(missing_ok=True)
    atomic_text(cleared, json.dumps({'run_id':rid,'cleared':True}))
    return read_run_trace(manager,pid,rid)

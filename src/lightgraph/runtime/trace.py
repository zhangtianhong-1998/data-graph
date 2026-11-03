"""Local run history, retained until the user explicitly clears it.

Unlike progress counters this archive contains model inputs and tool observations.
It is isolated from graph facts and never sent to a tracing service. Credentials
are redacted before either data or index bytes reach disk. Resource cleanup does
not delete this archive, and a failed writer cannot invalidate published facts.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading

from .retention import atomic_text

_sink = ContextVar('lightgraph_run_trace', default=None)
_context = ContextVar('lightgraph_trace_context', default={})
_REDACTED = '[REDACTED]'
_SECRET_KEYS = {'apikey','authorization','proxyauthorization','password','passwd','pwd',
    'dsn','connectionstring','accesstoken','refreshtoken','authtoken','clientsecret',
    'privatekey','secret','credentials','sourcecredentials','modelapikey','cookie','setcookie'}
_NAMED_SECRET = re.compile(
    r'''(?i)(["']?(?:api[_-]?key|password|passwd|pwd|authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|dsn)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)''')
_BEARER = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+')
_URI_CREDENTIALS = re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]*@')


def _secret_key(key):
    return re.sub(r'[^a-z0-9]','',str(key).lower()) in _SECRET_KEYS


def _schema_definition(value):
    return isinstance(value,dict) and (value.get('type') in ('string','number','integer','object','array','null','boolean')
        or any(key in value for key in ('$ref','anyOf','allOf','oneOf')))


def _check_path(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise OSError('trace_path_invalid')


def trace_path(record_path):
    path=Path(record_path)
    stem=path.name.removesuffix('.json').removesuffix('.result')
    return path.with_name(stem+'.trace.jsonl')


def trace_artifacts(path):
    path=Path(path)
    return path,path.with_suffix('.index.jsonl'),path.with_suffix('.status.json')


def current_trace():
    return _sink.get()


@contextmanager
def bind_trace(sink):
    token=_sink.set(sink)
    try:yield sink
    finally:_sink.reset(token)


@contextmanager
def trace_context(**values):
    token=_context.set({**_context.get(),**values})
    try:yield
    finally:_context.reset(token)


def emit_trace(kind,data=None):
    sink=current_trace()
    if sink is not None:
        sink(kind,{**_context.get(),**(data or {})})


class RunTrace:
    """One append-only JSONL writer with a lightweight committed-event index."""
    def __init__(self,path,*,secrets=()):
        self.path,self.index_path,self.status_path=trace_artifacts(Path(path).absolute())
        self.lock=threading.RLock()
        self.sequence=0
        self.error_code=None
        self.error_count=0
        self._secrets=set()
        self.add_secrets(secrets)
        try:
            _check_path(self.path)
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            _check_path(self.path)
            if self.index_path.exists():
                _check_path(self.index_path)
                with self.index_path.open(encoding='utf-8') as stream:
                    for line in stream:
                        last=json.loads(line)
                        self.sequence=last['seq']
                if self.sequence and self.path.stat().st_size != last['offset']+last['length']:
                    raise OSError('trace_incomplete')
            elif self.path.exists() and self.path.stat().st_size:
                raise OSError('trace_index_missing')
        except (OSError,ValueError,KeyError,TypeError):
            self._failed()

    def add_secrets(self,values):
        """Runtime credentials can be registered without putting them in events."""
        with self.lock:
            if isinstance(values,dict):values=values.values()
            if isinstance(values,str):values=[values]
            for value in values:
                if hasattr(value,'get_secret_value'):value=value.get_secret_value()
                if isinstance(value,str) and value:self._secrets.add(value)

    def _learn_credentials(self,value,depth=0):
        # Tool results can themselves contain JSON strings. Learn credential
        # values before redacting their escaped representation in outer JSON.
        if depth>32:return
        if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
        if isinstance(value,dict):
            for key,item in value.items():
                if _secret_key(key) and isinstance(item,str):self.add_secrets([item])
                self._learn_credentials(item,depth+1)
        elif isinstance(value,(list,tuple)):
            for item in value:self._learn_credentials(item,depth+1)
        elif isinstance(value,str):
            text=value.lstrip()
            if text.startswith(('{','[')):
                try:self._learn_credentials(json.loads(text),depth+1)
                except (ValueError,RecursionError):pass

    def _redact(self,value):
        if hasattr(value,'get_secret_value'):return _REDACTED
        if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
        if isinstance(value,dict):
            # A tool's JSON Schema may define a field called password. Preserve
            # its type/description contract; only credential values are secret.
            return {str(k):_REDACTED if _secret_key(k) and not _schema_definition(v) else self._redact(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [self._redact(v) for v in value]
        if isinstance(value,str):
            if 'data:' in value:
                # SSE can split a credential across semantic deltas, not just
                # byte chunks. Withhold that wire body rather than leak pieces
                # which can be reassembled; the normalized response is redacted
                # separately and retains its remaining content.
                parts=[]
                def collect(item):
                    if isinstance(item,dict):
                        for key,entry in item.items():
                            if key in {'content','reasoning_content','arguments'} and isinstance(entry,str):parts.append(entry)
                            else:collect(entry)
                    elif isinstance(item,list):
                        for entry in item:collect(entry)
                for line in value.splitlines():
                    if line.startswith('data:'):
                        try:collect(json.loads(line[5:]))
                        except (ValueError,RecursionError):pass
                joined=''.join(parts)
                self._learn_credentials(joined)
                if any(secret in joined for secret in self._secrets):return '[REDACTED: stream contains credentials]'
            for secret in sorted(self._secrets,key=len,reverse=True):
                for variant in {secret,json.dumps(secret,ensure_ascii=False)[1:-1]}:
                    value=value.replace(variant,_REDACTED)
            value=_URI_CREDENTIALS.sub(lambda m:m[1]+_REDACTED+'@',value)
            value=_NAMED_SECRET.sub(lambda m:m[1]+json.dumps(_REDACTED),value)
            return _BEARER.sub(lambda m:m[1]+' '+_REDACTED,value)
        return value

    @staticmethod
    def _append(path,data):
        _check_path(path)
        flags=os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(path,flags,0o600)
        try:
            os.chmod(path,0o600)
            offset=os.fstat(fd).st_size
            with os.fdopen(fd,'ab',closefd=False) as stream:
                stream.write(data);stream.flush();os.fsync(fd)
            return offset
        finally:os.close(fd)

    def _failed(self):
        self.error_code='trace_write_failed'
        self.error_count+=1
        try:
            _check_path(self.status_path)
            atomic_text(self.status_path,json.dumps({'error_code':self.error_code,'error_count':self.error_count}))
        except OSError:
            pass

    def __call__(self,kind,data):
        with self.lock:
            # Never silently skip a broken event then claim the archive complete.
            if self.error_code:return
            try:
                self._learn_credentials(data)
                event={'seq':self.sequence+1,'time':datetime.now(timezone.utc).isoformat(),
                    'kind':kind,'data':self._redact(data)}
                raw=(json.dumps(event,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')
                offset=self._append(self.path,raw)
                entry={'seq':event['seq'],'offset':offset,'length':len(raw)}
                self._append(self.index_path,(json.dumps(entry,separators=(',',':'))+'\n').encode('utf-8'))
                self.sequence+=1
            except (OSError,ValueError,TypeError,RecursionError):
                self._failed()

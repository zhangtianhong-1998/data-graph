"""Reuse authorized graph views across page and detail requests.

Published YAML is immutable aside from an in-place replacement, which changes
the file size or mtime. Callers must not mutate the returned graph.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import OrderedDict
from itertools import chain
from pathlib import Path
from typing import Callable

from lightgraph.contracts import digest

MAX_CACHED_VIEWS = 4
MAX_CACHED_RECORDS = 100_000
MAX_CACHE_BYTES = 256 * 1024 * 1024
Loader = Callable[[], tuple[dict, str]]

_lock = threading.Lock()
_views: OrderedDict[str, tuple[dict, str]] = OrderedDict()
_generations: dict[int, str] = {}
_inflight: dict[str, tuple[threading.Event, dict]] = {}
_pending: OrderedDict[str, tuple[Path, dict]] = OrderedDict()
_writer: threading.Thread | None = None


def view_token(key: dict) -> str:
    """Stable for one file, permission set, and scope selection."""
    return digest({'graph_view': 1, **key})


def remember_graph(graph: dict, generation: str) -> None:
    with _lock:
        _generations[id(graph)] = generation


def cached_generation(graph: dict) -> str | None:
    with _lock:
        return _generations.get(id(graph))


def clear_graph_views() -> None:
    """Drop memory only. A later read may still reuse a matching disk copy."""
    with _lock:
        _views.clear()
        _generations.clear()
        _pending.clear()


def flush_graph_views(timeout: float = 30) -> None:
    with _lock:
        writer = _writer
    if writer is not None:
        writer.join(timeout)


def load_graph_view(directory: Path, key: dict, loader: Loader) -> tuple[dict, str]:
    token = view_token(key)
    with _lock:
        cached = _views.get(token)
        if cached is not None:
            _views.move_to_end(token)
            _generations[id(cached[0])] = cached[1]
            return cached
        flight = _inflight.get(token)
        if flight is None:
            flight = (threading.Event(), {})
            _inflight[token] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight[0].wait()
        error = flight[1].get('error')
        if error is not None:
            raise error
        return flight[1]['value']
    try:
        value = _read_disk(directory, key)
        from_disk = value is not None
        if not from_disk:
            value = loader()
        with _lock:
            _store(token, value)
            flight[1]['value'] = value
            if not from_disk:
                _write_later(directory, key, token)
        return value
    except BaseException as error:
        flight[1]['error'] = error
        raise
    finally:
        with _lock:
            _inflight.pop(token, None)
        flight[0].set()


def _store(token: str, value: tuple[dict, str]) -> None:
    _views[token] = value
    _views.move_to_end(token)
    graph, generation = value
    _generations[id(graph)] = generation

    def records(graph):
        return len(graph.get('nodes', ())) + len(graph.get('edges', ()))

    total = sum(records(view) for view, _ in _views.values())
    while len(_views) > 1 and (len(_views) > MAX_CACHED_VIEWS or total > MAX_CACHED_RECORDS):
        dropped, (dropped_graph, _generation) = _views.popitem(last=False)
        total -= records(dropped_graph)
        _pending.pop(dropped, None)
        if dropped_graph is not graph:
            _generations.pop(id(dropped_graph), None)


def _disk_name(key: dict) -> str:
    identity = {name: key[name] for name in ('version', 'kind', 'path', 'access', 'scopes')}
    return digest(identity) + '.json'


def _read_disk(directory: Path, key: dict):
    path = directory / _disk_name(key)
    try:
        info = path.stat()
        if directory.is_symlink() or path.is_symlink() or not path.is_file() or info.st_size > MAX_CACHE_BYTES:
            return None
        with path.open('rb') as stream:
            # Format 2 is a JSON array with one header, followed by node records
            # and then edge records. Rebuild older, single-line disk caches.
            if stream.readline(2) != b'[\n':
                return None
            header = json.loads(stream.readline())
            if not isinstance(header, dict) or header.get('format') != 2 or header.get('key') != key:
                return None
            generation, graph = header.get('generation'), header.get('graph')
            if not isinstance(generation, str) or not isinstance(graph, dict):
                return None
            for field in ('nodes', 'edges'):
                count = header.get(field)
                if type(count) is not int or count < 0:
                    return None
                graph[field] = []
                for _ in range(count):
                    if stream.read(1) != b',':
                        return None
                    item = json.loads(stream.readline())
                    if not isinstance(item, dict):
                        return None
                    graph[field].append(item)
            if stream.read(3) != b']\n':
                return None
            return graph, generation
    except (OSError, ValueError, RecursionError):
        return None


def _write_disk(directory: Path, key: dict, value: tuple[dict, str]) -> None:
    if directory.is_symlink():
        return
    directory.mkdir(parents=True, exist_ok=True)
    graph, generation = value
    nodes, edges = graph['nodes'], graph['edges']
    header = {'format': 2, 'key': key, 'generation': generation,
              'graph': {k: v for k, v in graph.items() if k not in ('nodes', 'edges')},
              'nodes': len(nodes), 'edges': len(edges)}
    fd, temporary = tempfile.mkstemp(prefix='.view-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'[\n')
            for index, record in enumerate(chain((header,), nodes, edges)):
                data = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                if index:
                    stream.write(b',')
                if stream.tell() + len(data) + len(b'\n]\n') > MAX_CACHE_BYTES:
                    return
                stream.write(data)
                stream.write(b'\n')
            stream.write(b']\n')
        os.replace(temporary, directory / _disk_name(key))
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_later(directory: Path, key: dict, token: str) -> None:
    """Schedule under _lock. Waiting writes hold cache keys, not graph objects."""
    global _writer
    _pending[token] = directory, key
    if _writer is None:
        _writer = threading.Thread(target=_drain_writes, name='graph-view-cache', daemon=True)
        _writer.start()


def _drain_writes() -> None:
    global _writer
    while True:
        with _lock:
            if not _pending:
                _writer = None
                return
            token, (directory, key) = _pending.popitem(last=False)
            value = _views[token]
        try:
            _write_disk(directory, key, value)
        except (OSError, TypeError, ValueError, RecursionError):
            # Disk caching is optional, including for an unwritable workspace.
            continue

"""Reuse authorized graph views across page and detail requests.

Storage and access fingerprints identify each view. Callers treat its records as immutable.
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

_DISK_FORMAT = 3  # Earlier views can carry descriptions from a partially visible type.
MAX_CACHED_VIEWS = 4
MAX_CACHED_RECORDS = 100_000
MAX_CACHE_BYTES = 256 * 1024 * 1024
Loader = Callable[[], tuple[dict, str]]

_lock = threading.Lock()
_views: OrderedDict[str, tuple[dict, str]] = OrderedDict()
class _GraphView(dict):
    generation: str

_inflight: dict[str, tuple[threading.Event, dict]] = {}
_pending: OrderedDict[str, tuple[Path, dict]] = OrderedDict()
_writer: threading.Thread | None = None


def view_token(key: dict) -> str:
    """Stable for one file, permission set, and scope selection."""
    return digest({'graph_view': 1, **key})


def cached_generation(graph: dict) -> str | None:
    return graph.generation if isinstance(graph, _GraphView) else None


def clear_graph_views() -> None:
    """Drop memory only. A later read may still reuse a matching disk copy."""
    with _lock:
        _views.clear()
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
        graph, generation = value
        view = _GraphView(graph)
        view.generation = generation
        value = view, generation
        with _lock:
            _store(token, value)
            flight[1]['value'] = value
            resident = token in _views
            if not from_disk and resident:
                _write_later(directory, key, token)
        if not from_disk and not resident:
            # The request already owns this graph. Persist it before releasing
            # the shared flight; never queue an unbounded second retained view.
            try:
                _write_disk(directory, key, value)
            except (OSError, TypeError, ValueError, RecursionError):
                pass
        return value
    except BaseException as error:
        flight[1]['error'] = error
        raise
    finally:
        with _lock:
            _inflight.pop(token, None)
        flight[0].set()


def _store(token: str, value: tuple[dict, str]) -> None:
    graph, _ = value

    def records(graph):
        return len(graph.get('nodes', ())) + len(graph.get('edges', ()))

    if records(graph) > MAX_CACHED_RECORDS:
        return
    _views[token] = value
    _views.move_to_end(token)
    total = sum(records(view) for view, _ in _views.values())
    while len(_views) > MAX_CACHED_VIEWS or total > MAX_CACHED_RECORDS:
        dropped, (dropped_graph, _generation) = _views.popitem(last=False)
        total -= records(dropped_graph)
        _pending.pop(dropped, None)


def _disk_name(key: dict) -> str:
    identity = {name: key[name] for name in ('version', 'kind', 'path', 'access', 'scopes')}
    return digest(identity) + '.json'


def _read_disk(directory: Path, key: dict):
    from lightgraph.construction.paths import io_path
    directory = io_path(directory)
    path = directory / _disk_name(key)
    try:
        info = path.stat()
        if directory.is_symlink() or path.is_symlink() or not path.is_file() or info.st_size > MAX_CACHE_BYTES:
            return None
        with path.open('rb') as stream:
            # The cache is a JSON array with one header, followed by node records
            # and then edge records. Rebuild older, single-line disk caches.
            if stream.readline(2) != b'[\n':
                return None
            header = json.loads(stream.readline())
            if not isinstance(header, dict) or header.get('format') != _DISK_FORMAT or header.get('key') != key:
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
    from lightgraph.construction.paths import io_path
    directory = io_path(directory)
    if directory.is_symlink():
        return
    directory.mkdir(exist_ok=True)
    graph, generation = value
    nodes, edges = graph['nodes'], graph['edges']
    header = {'format': _DISK_FORMAT, 'key': key, 'generation': generation,
              'graph': {k: v for k, v in graph.items() if k not in ('nodes', 'edges')},
              'nodes': len(nodes), 'edges': len(edges)}
    fd, temporary = tempfile.mkstemp(prefix='.view-', dir=directory)
    tmp_io = io_path(temporary)
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
        current = directory / _disk_name(key)
        os.replace(tmp_io, current)
        # Only our own content-addressed cache files are eligible for eviction.
        entries = []
        for entry in directory.glob('*.json'):
            if len(entry.stem) == 64 and all(c in '0123456789abcdef' for c in entry.stem) and not entry.is_symlink():
                info = entry.stat()
                entries.append((info.st_mtime_ns, entry, info.st_size))
        retained = current.stat().st_size
        for _, entry, size in sorted(entries, reverse=True):
            if entry != current:
                if retained + size > MAX_CACHE_BYTES:
                    entry.unlink(missing_ok=True)
                else:
                    retained += size
    finally:
        tmp_io.unlink(missing_ok=True)


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

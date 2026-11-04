"""Remember one authorized graph so later pages do not parse it again.

Published YAML is immutable aside from an in-place replacement, which changes
the file size or mtime. Callers must not mutate the returned graph.
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from lightgraph.contracts import digest
from lightgraph.runtime.retention import atomic_text

MAX_CACHED_VIEWS = 4
MAX_CACHE_BYTES = 256 * 1024 * 1024
Loader = Callable[[], tuple[dict, str]]

_lock = threading.Lock()
_views: OrderedDict[str, tuple[dict, str]] = OrderedDict()
_generations: dict[int, str] = {}
_inflight: dict[str, tuple[threading.Event, dict]] = {}
_writers: list[threading.Thread] = []


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


def flush_graph_views(timeout: float = 30) -> None:
    with _lock:
        writers = list(_writers)
    for writer in writers:
        writer.join(timeout)
    with _lock:
        _writers[:] = [writer for writer in _writers if writer.is_alive()]


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
        if value is None:
            value = loader()
            _write_later(directory, key, value)
        with _lock:
            _store(token, value)
            flight[1]['value'] = value
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
    while len(_views) > MAX_CACHED_VIEWS:
        _dropped, (dropped_graph, _generation) = _views.popitem(last=False)
        if dropped_graph is not graph:
            _generations.pop(id(dropped_graph), None)


def _disk_name(key: dict) -> str:
    identity = {name: key[name] for name in ('version', 'kind', 'path', 'access', 'scopes')}
    return digest(identity) + '.json'


def _read_disk(directory: Path, key: dict):
    path = directory / _disk_name(key)
    try:
        info = path.stat()
        if path.is_symlink() or not path.is_file() or info.st_size > MAX_CACHE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get('format') != 1 or payload.get('key') != key:
        return None
    graph, generation = payload.get('graph'), payload.get('generation')
    if not isinstance(generation, str) or not isinstance(graph, dict):
        return None
    if not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        return None
    return graph, generation


def _write_later(directory: Path, key: dict, value: tuple[dict, str]) -> None:
    if directory.is_symlink():
        return

    def write() -> None:
        try:
            # Dumping a large graph holds the interpreter. Let the page response finish first.
            time.sleep(0.25)
            if directory.is_symlink():
                return
            text = json.dumps({'format': 1, 'key': key, 'generation': value[1], 'graph': value[0]},
                              ensure_ascii=False, separators=(',', ':'))
            if len(text.encode('utf-8')) > MAX_CACHE_BYTES:
                return
            atomic_text(directory / _disk_name(key), text)
        except (OSError, TypeError, ValueError):
            return

    thread = threading.Thread(target=write, name='graph-view-cache', daemon=True)
    with _lock:
        _writers[:] = [item for item in _writers if item.is_alive()]
        _writers.append(thread)
    thread.start()

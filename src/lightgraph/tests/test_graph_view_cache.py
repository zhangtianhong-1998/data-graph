"""Authorized graph views stay in memory and can be reopened without YAML."""
import json
import threading

import pytest

from lightgraph.web import graph_view_cache as cache
from lightgraph.web.graph_view_cache import (
    clear_graph_views, flush_graph_views, load_graph_view, view_token,
)


@pytest.fixture(autouse=True)
def reset_views():
    clear_graph_views()
    yield
    flush_graph_views()
    clear_graph_views()


def key(path, **changes):
    value = {'version': 1, 'kind': 'published', 'path': str(path), 'size': 3,
             'mtime_ns': '10', 'access': 'access', 'scopes': ['public']}
    value.update(changes)
    return value


def test_one_build_serves_later_reads_until_the_file_identity_changes(tmp_path):
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return {'nodes': [{'id': 'n', 'label': '名称'}], 'edges': []}, 'gen-1'

    current = key(tmp_path / 'graph.yaml')
    assert view_token(current) != view_token(key(tmp_path / 'graph.yaml', mtime_ns='11'))
    assert load_graph_view(tmp_path, current, loader)[0]['nodes'][0]['label'] == '名称'
    assert load_graph_view(tmp_path, current, loader)[1] == 'gen-1'
    assert calls == 1
    flush_graph_views()
    clear_graph_views()
    assert load_graph_view(tmp_path, current, loader)[0]['nodes'][0]['id'] == 'n'
    assert calls == 1
    assert load_graph_view(tmp_path, key(tmp_path / 'graph.yaml', scopes=['A']), loader)[1]
    assert calls == 2


def test_failed_build_is_not_reused(tmp_path):
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        raise RuntimeError('missing')

    current = key(tmp_path / 'missing.yaml')
    for _ in range(2):
        with pytest.raises(RuntimeError, match='missing'):
            load_graph_view(tmp_path, current, loader)
    assert calls == 2


def test_overlapping_readers_share_one_build(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return {'nodes': [{'id': 'a'}], 'edges': []}, 'g'

    current = key(tmp_path / 'shared.yaml')
    results, errors = [], []

    def read():
        try:
            results.append(load_graph_view(tmp_path, current, loader))
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=read)
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=read)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert errors == [] and calls == 1 and results[0] == results[1]


def test_corrupt_disk_copy_is_ignored(tmp_path):
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return {'nodes': [], 'edges': []}, 'gen'

    current = key(tmp_path / 'broken.yaml')
    load_graph_view(tmp_path, current, loader)
    flush_graph_views()
    for path in tmp_path.glob('*.json'):
        path.write_text('{', encoding='utf-8')
    clear_graph_views()
    load_graph_view(tmp_path, current, loader)
    assert calls == 2


def test_oversize_refresh_stops_early_and_preserves_previous_complete_cache(tmp_path, monkeypatch):
    previous = key(tmp_path / 'graph.yaml')
    old_view = {'nodes': [{'id': 'n'}], 'edges': [], 'status': 'ok'}
    load_graph_view(tmp_path, previous, lambda: (old_view, 'v1'))
    flush_graph_views()
    current = {**previous, 'mtime_ns': '11'}
    encoded = []
    dumps = json.dumps

    def track(value, **kwargs):
        if 'row' in value:
            encoded.append(value['row'])
        return dumps(value, **kwargs)

    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 1500)
    monkeypatch.setattr(cache.json, 'dumps', track)
    graph = {'nodes': [{'row': i, 'label': '码' * 200} for i in range(100)], 'edges': []}
    assert load_graph_view(tmp_path, current, lambda: (graph, 'v2'))[1] == 'v2'
    flush_graph_views()
    assert 0 < len(encoded) < 5
    assert list(tmp_path.glob('.view-*')) == []
    clear_graph_views()
    assert load_graph_view(tmp_path, previous, lambda: pytest.fail('old cache overwritten')) == (old_view, 'v1')


def test_switching_large_views_evicts_older_versions_and_skips_evicted_writes(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    writes = []

    def write(directory, key, value):
        writes.append(value[1])
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(cache, 'MAX_CACHED_RECORDS', 3)
    monkeypatch.setattr(cache, '_write_disk', write)
    graph = {'nodes': [{'id': 'one'}, {'id': 'two'}], 'edges': []}
    try:
        for revision in ('v1', 'v2', 'v3'):
            load_graph_view(tmp_path, key(tmp_path / revision), lambda: (graph.copy(), revision))
            assert entered.wait(5)
    finally:
        release.set()
        flush_graph_views()
    assert writes == ['v1', 'v3']
    reloaded = load_graph_view(tmp_path, key(tmp_path / 'v2'), lambda: (graph.copy(), 'rebuilt'))
    assert reloaded[1] == 'rebuilt'

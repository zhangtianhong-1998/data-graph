"""Authorized graph views stay in memory and can be reopened without YAML."""
import threading

import pytest

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

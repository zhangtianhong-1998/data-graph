import errno
from pathlib import Path

import pytest

from lightgraph.retrieval.sync import _publish_staging, active_index, sync_graph_to_retrieval


def test_publish_staging_retries_transient_sharing_lock(tmp_path, monkeypatch):
    staging = tmp_path / 'staging'
    staging.mkdir()
    destination = tmp_path / 'published'
    original = Path.rename
    calls = []
    waits = []

    def rename(path, target):
        calls.append(path)
        if len(calls) <= 3:
            error = PermissionError(errno.EACCES, 'sharing violation')
            error.winerror = 32
            raise error
        return original(path, target)

    monkeypatch.setattr(Path, 'rename', rename)
    monkeypatch.setattr('lightgraph.retrieval.sync.time.sleep', waits.append)
    _publish_staging(staging, destination)
    assert destination.is_dir() and not staging.exists()
    assert len(calls) == 4 and waits == [.05] * 3


@pytest.mark.parametrize('error, attempts', [(PermissionError(errno.EACCES, 'locked'), 11),
                                          (OSError(errno.ENOSPC, 'full'), 1)])
def test_failed_publish_preserves_active_generation(tmp_path, monkeypatch, error, attempts):
    graph = {'nodes': [], 'edges': [], 'support_visibility_version': 1}
    original = sync_graph_to_retrieval(graph, tmp_path, generation='original')
    before = (tmp_path / 'current.json').read_bytes()
    calls = []

    def fail(path, target):
        calls.append(path)
        raise error

    monkeypatch.setattr(Path, 'rename', fail)
    monkeypatch.setattr('lightgraph.retrieval.sync.time.sleep', lambda _: None)
    with pytest.raises(OSError):
        sync_graph_to_retrieval(graph, tmp_path, generation='replacement')
    assert len(calls) == attempts
    assert (tmp_path / 'current.json').read_bytes() == before
    path, manifest = active_index(tmp_path, 'original')
    assert path.is_dir() and manifest['build'] == original['build']


def test_retention_pins_active_reader_while_replacing_idle_generations(tmp_path):
    from lightgraph.retrieval import SubgraphRetriever
    from lightgraph.retrieval.query import RetrieveSubgraphRequest

    graph = {'support_visibility_version': 1, 'nodes': [
        {'id': nid, 'type': 'Object', 'identity': {'code': value}, 'supports': [
            {'scope_ref': 'public', 'label': nid}]} for nid, value in [('zero', 0), ('boolean', False)]], 'edges': []}
    sync_graph_to_retrieval(graph, tmp_path, generation='original')
    reader = SubgraphRetriever(tmp_path)
    old_root = reader.root
    for i in range(4):
        sync_graph_to_retrieval(graph, tmp_path, generation=str(i))
    assert len(list((tmp_path / 'builds').iterdir())) == 3
    for term, expected in [('0', 'zero'), ('false', 'boolean')]:
        answer = RetrieveSubgraphRequest(mode='hierarchy', query=term).run(reader, ['public'])
        assert answer['hierarchy']['anchor_id'] == expected
    del reader
    import gc
    gc.collect()
    sync_graph_to_retrieval(graph, tmp_path)
    assert not old_root.exists() and len(list((tmp_path / 'builds').iterdir())) == 2

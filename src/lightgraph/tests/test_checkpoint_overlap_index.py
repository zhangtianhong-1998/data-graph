"""The overlap directory is pinned to a checkpoint; unselected evidence remains unread."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3

import pytest

from lightgraph.construction import checkpoint_overlap_index as store, checkpoints
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_batch_overlap_patterns import unit


def commit(root, owner='north'):
    return checkpoints.commit_checkpoint(
        root, 'run_' + '1' * 24, owner, unit(owner, 'Region', 'code'), graph_kind='node_delta', input_digest='same')


def legacy_marker(root, record):
    directory = (root / record['relative_path']).parent
    historical = {k: v for k, v in record.items() if k != 'overlap_index_digest'}
    (directory / 'checkpoint.json').write_text(json.dumps(historical), encoding='utf-8')
    (directory / store.FILENAME).unlink()
    return directory, historical


def test_index_is_pinned_and_only_selected_blocks_are_loaded(tmp_path, monkeypatch):
    record = commit(tmp_path)
    repeated = commit(tmp_path)
    assert record == repeated and len(record['overlap_index_digest']) == 64
    accesses = []
    read = store._read_block
    def observed(db, address):
        accesses.append(address)
        return read(db, address)
    monkeypatch.setattr(store, '_read_block', observed)
    monkeypatch.setattr(checkpoints, 'read_graph_record', lambda *a, **kw: pytest.fail('graph decoded'))
    index = checkpoints.load_checkpoint_overlap_index(tmp_path, record)
    family, = index['families']
    assert accesses == [record['overlap_index_digest']] and family['type'] == 'Region'
    assert family['evidence']
    assert len(accesses) == 2 and family['evidence'] and len(accesses) == 2
    assert checkpoints.read_checkpoint_overlap_review(tmp_path, record) is None


def test_legacy_prepare_verifies_once_and_leaves_history_unchanged(tmp_path, monkeypatch):
    directory, record = legacy_marker(tmp_path, commit(tmp_path))
    original = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.rglob('*')
                if p.is_file() and p.name != '.writer.lock'}
    reads = []
    load = checkpoints.read_graph_record
    def counted(*args, **kwargs):
        reads.append(1)
        return load(*args, **kwargs)
    monkeypatch.setattr(checkpoints, 'read_graph_record', counted)
    assert checkpoints.load_checkpoint_overlap_index(tmp_path, record) is None and not reads
    assert checkpoints.prepare_checkpoint_overlap_index(tmp_path, record) == 'built' and reads == [1]
    assert checkpoints.prepare_checkpoint_overlap_index(tmp_path, record) == 'reused' and reads == [1]
    assert checkpoints.load_checkpoint_overlap_index(tmp_path, record)['families'][0]['evidence']
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in original} == original
    # Losing the legacy commit receipt cannot confer authority on an orphan database.
    (directory / store.RECEIPT).unlink()
    assert checkpoints.load_checkpoint_overlap_index(tmp_path, record) is None


@pytest.mark.parametrize('legacy', [False, True])
def test_tampered_member_block_rejected_on_access_and_repaired_explicitly(tmp_path, legacy):
    record = commit(tmp_path)
    directory = (tmp_path / record['relative_path']).parent
    if legacy:
        directory, record = legacy_marker(tmp_path, record)
        checkpoints.prepare_checkpoint_overlap_index(tmp_path, record)
    with closing(sqlite3.connect(directory / store.FILENAME)) as db:
        header = json.loads(db.execute('SELECT payload FROM blocks JOIN manifest USING (address)').fetchone()[0])
        addr = header['index']['families'][0]['blocks']['evidence']
        db.execute("UPDATE blocks SET payload=? WHERE address=?", (b'{}', addr))
        db.commit()
    index = checkpoints.load_checkpoint_overlap_index(tmp_path, record)
    assert index['families'][0]['type'] == 'Region'
    with pytest.raises(LightGraphError, match='workflow_checkpoint_invalid'):
        _ = index['families'][0]['evidence']
    assert checkpoints.prepare_checkpoint_overlap_index(tmp_path, record) == 'built'
    assert checkpoints.load_checkpoint_overlap_index(tmp_path, record)['families'][0]['evidence']


def test_copied_index_rejected_for_another_artifact(tmp_path):
    a = commit(tmp_path)
    b = commit(tmp_path, 'south')
    def path(record):
        return (tmp_path / record['relative_path']).parent / store.FILENAME
    path(b).write_bytes(path(a).read_bytes())
    with pytest.raises(LightGraphError, match='workflow_checkpoint_invalid'):
        checkpoints.load_checkpoint_overlap_index(tmp_path, b)


def test_concurrent_legacy_build_produces_one_index(tmp_path):
    _, record = legacy_marker(tmp_path, commit(tmp_path))
    def prepare(_):
        return checkpoints.prepare_checkpoint_overlap_index(tmp_path, record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(prepare, range(2))) == ['built', 'reused']
    assert checkpoints.load_checkpoint_overlap_index(tmp_path, record)['families'][0]['evidence']

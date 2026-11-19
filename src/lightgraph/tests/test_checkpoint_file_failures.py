import errno
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lightgraph.construction import checkpoints
from lightgraph.construction.artifact_io import WorkflowPersistenceError
from lightgraph.errors import LightGraphError


def locked():
    exc = PermissionError(errno.EACCES, 'PRIVATE-ROW', 'PRIVATE-PATH')
    exc.winerror = 32
    return exc


def commit(output):
    graph = {'nodes': [{'id': 'a', 'type': 'T'}], 'edges': []}
    return checkpoints.commit_checkpoint(output, 'run_' + 'a' * 24, 'unit',
        {'graph': graph, 'evidence_records': {'ref': {'revision': 1}}}, input_digest='input')


@pytest.mark.parametrize('stage', ['graph', 'evidence', 'marker'])
def test_transient_checkpoint_file_failure_commits_one_readable_artifact(tmp_path, monkeypatch, stage):
    failed = []
    real_graph = checkpoints.write_graph_record
    real_text = checkpoints.atomic_text

    def write_graph(path, graph):
        if stage == 'graph' and not failed:
            failed.append(True)
            path.mkdir()
            (path / 'nodes.parquet').write_bytes(b'incomplete')
            raise locked()
        return real_graph(path, graph)

    def write_text(path, value):
        expected = {'evidence': 'evidence.json', 'marker': 'checkpoint.json'}.get(stage)
        if path.name == expected and not failed:
            failed.append(True)
            raise locked()
        return real_text(path, value)

    monkeypatch.setattr(checkpoints, 'write_graph_record', write_graph)
    monkeypatch.setattr(checkpoints, 'atomic_text', write_text)
    record = commit(tmp_path)
    assert failed == [True] and record == commit(tmp_path)
    restored = checkpoints.load_checkpoint(tmp_path, record)
    assert restored['graph']['nodes'][0]['id'] == 'a' and restored['evidence_records'] == {'ref': {'revision': 1}}


@pytest.mark.parametrize(('error', 'attempts'), [(locked(), 4), (OSError(errno.ENOSPC, 'PRIVATE'), 1),
                                                  (ValueError('PRIVATE'), 1)])
def test_failed_checkpoint_marker_preserves_reusable_graph_and_safe_details(tmp_path, monkeypatch, error, attempts):
    original = checkpoints.atomic_text
    calls = []

    def write(path, value):
        if path.name == 'checkpoint.json':
            calls.append(path)
            raise error
        return original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, 'atomic_text', write)
        with pytest.raises(WorkflowPersistenceError) as captured:
            commit(tmp_path)
    assert len(calls) == attempts and not calls[0].exists()
    detail = captured.value.detail
    assert detail['stage'] == 'checkpoint_marker_write' and detail['attempts'] == attempts
    assert 'PRIVATE' not in str(detail) and str(tmp_path) not in str(detail)
    if isinstance(error, PermissionError):
        assert detail['winerror'] == 32 and detail['errno'] == errno.EACCES
    def do_not_rewrite(*args):
        pytest.fail('checkpoint resume must reuse the verified graph')
    monkeypatch.setattr(checkpoints, 'write_graph_record', do_not_rewrite)
    record = commit(tmp_path)
    assert Path(tmp_path, record['relative_path']).is_dir()
    assert checkpoints.load_checkpoint(tmp_path, record)['evidence_records']


def test_temporary_install_lock_reuses_completed_parquet(tmp_path, monkeypatch):
    install = Path.rename
    attempts = []

    def rename(staged, destination):
        attempts.append(staged)
        if len(attempts) == 1:
            assert (staged / 'metadata.yaml').is_file()
            raise locked()
        return install(staged, destination)

    monkeypatch.setattr(Path, 'rename', rename)
    record = commit(tmp_path)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert checkpoints.load_checkpoint(tmp_path, record)['graph']['nodes'][0]['id'] == 'a'


def test_concurrent_conflicting_writes_cannot_mix_graph_and_evidence(tmp_path):
    def submit(value):
        contributor = {'graph': {'nodes': [{'id': value, 'type': 'Item'}], 'edges': []},
                       'evidence_records': {'ref': value}}
        try:
            return checkpoints.commit_checkpoint(tmp_path, 'run_' + 'e' * 24, 'merge', contributor, input_digest='same')
        except LightGraphError as exc:
            assert exc.code in {'workflow_checkpoint_immutable', 'workflow_checkpoint_busy'}
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = [record for record in pool.map(submit, ['first', 'second']) if record is not None]
    assert len(records) == 1
    data = checkpoints.load_checkpoint(tmp_path, records[0])
    assert data['graph']['nodes'][0]['id'] == data['evidence_records']['ref']

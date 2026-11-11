"""Fresh runs isolate construction state without deleting previous outcomes."""
from pathlib import Path
import subprocess
import sys

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.tests.construction_worker_fixture import install_scripted_worker
from lightgraph.tests.test_web_execution import wait_run
from lightgraph.tests.web_client import RevisionClient
from lightgraph.web.app import create_app
from lightgraph.web.execution import RunManager, read_graph, read_metadata
from lightgraph.web.run_traces import read_run_trace
from lightgraph.web.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('运行模式测试', 'alpha')
    workspace.add_file('alpha', 'metrics.csv', b'metric_code,metric_name\nK1,Revenue\nK2,Cost\n')
    config = workspace.get_config('alpha')
    config['goal']['purpose'] = '发现指标对象'
    config['model'] = {'mode': 'in_environment', 'model': 'scripted-test',
                       'base_url': 'http://127.0.0.1:9999/v1'}
    workspace.save_config('alpha', config)
    return workspace


def test_fresh_run_preserves_history_and_continue_uses_new_lineage(workspace, monkeypatch):
    install_scripted_worker(monkeypatch)
    manager = RunManager(workspace)
    try:
        first = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
        assert first['status'] == 'completed', first
        assert first['run_mode'] == 'continue' and first['output_path'] == 'output'
        old_output = Path(workspace.resolved_config('alpha').output_dir)
        marker = old_output / 'construction/harness/session-index.sqlite'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b'OLD_SESSION_MEMORY')
        old_files = {p.relative_to(old_output): p.read_bytes() for p in old_output.rglob('*') if p.is_file()}
        config_before = workspace.get_config('alpha')
        revision = workspace.config_revision('alpha')
        second = wait_run(manager, 'alpha', manager.start('alpha', run_mode='fresh')['run_id'])
        assert second['status'] == 'completed', second
        new_output = Path(workspace.resolved_config('alpha').output_dir)
        assert new_output == old_output / 'generations' / second['run_id']
        assert second['output_path'] == f"output/generations/{second['run_id']}"
        assert (new_output / 'construction/drafts').is_dir()
        assert not (new_output / 'construction/harness/session-index.sqlite').exists()
        assert {node['label'] for node in read_graph(workspace, 'alpha')['nodes']} == {'Revenue', 'Cost'}
        assert {relative: (old_output / relative).read_bytes() for relative in old_files} == old_files
        assert workspace.config_revision('alpha') == revision
        assert workspace.get_config('alpha') == config_before
        # Service restart and ordinary saves must not reset the selected lineage.
        reloaded = Workspace(workspace.root)
        reloaded.save_config('alpha', config_before)
        assert Path(reloaded.resolved_config('alpha').output_dir) == new_output
        assert reloaded.resolved_config('alpha').sources[0].path == workspace.resolved_config('alpha').sources[0].path
        third = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
        assert third['status'] == 'completed'
        assert third['run_mode'] == 'continue' and third['output_path'] == second['output_path']
        assert manager.get('alpha', first['run_id'])['output_path'] == 'output'
        assert manager.get('alpha', first['run_id'])['report'] == first['report']
        assert read_run_trace(manager, 'alpha', first['run_id'])['status'] == 'available'
        assert len(manager.list('alpha')) == 3
    finally:
        manager.shutdown()


def test_failed_launch_restores_previous_generation_without_erasing_it(workspace, monkeypatch):
    previous = 'run_' + '1' * 24
    workspace.select_output_generation('alpha', previous, create=True)
    marker = Path(workspace.resolved_config('alpha').output_dir) / 'kept.txt'
    marker.write_bytes(b'previous publication')
    def fail(*args, **kwargs):
        raise OSError('cannot start worker')
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', fail)
    manager = RunManager(workspace)
    try:
        result = manager.start('alpha', run_mode='fresh')
        assert result['status'] == 'failed'
        assert workspace.output_generation('alpha') == previous
        assert marker.read_bytes() == b'previous publication'
        assert workspace.generation_path('alpha', result['run_id']).is_dir()
    finally:
        manager.shutdown()


def test_first_fresh_run_migrates_legacy_metadata_before_any_cached_get(workspace, monkeypatch):
    original = read_metadata(workspace, 'alpha', refresh=True)
    preview = workspace.project_path('alpha') / 'metadata/catalog-preview.yaml'
    preview.unlink()  # Simulate a project saved before project-level previews existed.
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *a, **kw: pytest.fail('fresh migration must not connect'))
    manager = RunManager(workspace)
    try:
        run = wait_run(manager, 'alpha', manager.start('alpha', run_mode='fresh')['run_id'])
        assert run['status'] == 'failed'  # Deliberately no run output.
        assert preview.exists()
        result = read_metadata(Workspace(workspace.root), 'alpha')
        assert result['tables'] == original['tables']
        assert result['using_saved_metadata'] and not result['requires_refresh']
    finally:
        manager.shutdown()


def test_fresh_run_stays_on_current_generation_if_preview_migration_cannot_save(workspace, monkeypatch):
    read_metadata(workspace, 'alpha', refresh=True)
    (workspace.project_path('alpha') / 'metadata/catalog-preview.yaml').unlink()
    previous_output = workspace.resolved_config('alpha').output_dir
    def failed(*args, **kwargs):
        raise PermissionError('DO_NOT_EXPOSE_PRIVATE_PATH')
    monkeypatch.setattr('lightgraph.web.execution._save_metadata_preview', failed)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', lambda *a, **kw: pytest.fail('migration must finish before launching'))
    manager = RunManager(workspace)
    try:
        with pytest.raises(LightGraphError, match='metadata_preview_persistence_failed'):
            manager.start('alpha', run_mode='fresh')
        assert workspace.output_generation('alpha') is None
        assert workspace.resolved_config('alpha').output_dir == previous_output
        assert not manager.list('alpha')
    finally:
        manager.shutdown()


def test_active_run_blocks_fresh_and_does_not_change_pointer(workspace, monkeypatch):
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys,time; sys.stdin.buffer.read(); time.sleep(30)'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace, cancel_timeout=.05)
    try:
        run = manager.start('alpha')
        with pytest.raises(LightGraphError, match='project_run_active'):
            manager.start('alpha', run_mode='fresh')
        assert workspace.output_generation('alpha') is None
        assert not (workspace.project_path('alpha') / 'output/generations').exists()
        manager.cancel('alpha', run['run_id'])
    finally:
        manager.shutdown()


@pytest.mark.parametrize('value', ["run_id: ../other", "run_id: run_123", "run_id: []", "run_id: null"])
def test_output_generation_pointer_rejects_invalid_paths(workspace, value):
    (workspace.project_path('alpha') / 'output-head.yaml').write_text(value)
    with pytest.raises(LightGraphError, match='invalid_output_generation'):
        workspace.resolved_config('alpha')


def test_api_accepts_both_modes_and_legacy_empty_request(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'api-workspace', frontend_dir=tmp_path / 'no-dist')
    with RevisionClient(app) as client:
        client.post('/api/projects', json={'name': 'run mode', 'project_id': 'alpha'})
        captured = []
        def start(pid, **options):
            captured.append(options.get('run_mode', 'continue'))
            return {'run_mode': captured[-1]}
        monkeypatch.setattr(app.state.manager, 'start', start)
        for body, expected in [(None, 'continue'), ({}, 'continue'), ({'run_mode': 'continue'}, 'continue'), ({'run_mode': 'fresh'}, 'fresh')]:
            response = client.post('/api/projects/alpha/runs', **({'json': body} if body is not None else {}))
            assert response.status_code == 202, response.text
            assert response.json()['run_mode'] == expected
        assert client.post('/api/projects/alpha/runs', json={'run_mode': 'erase'}).status_code == 422
        assert captured == ['continue', 'continue', 'continue', 'fresh']


def test_failed_fresh_worker_keeps_new_head_and_old_files(workspace, monkeypatch):
    old_output = Path(workspace.resolved_config('alpha').output_dir)
    marker = old_output / 'previous-graph.yaml'
    marker.write_bytes(b'old graph stays on disk')
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    manager = RunManager(workspace)
    try:
        failed = wait_run(manager, 'alpha', manager.start('alpha', run_mode='fresh')['run_id'])
        assert failed['status'] == 'failed'
        assert workspace.output_generation('alpha') == failed['run_id']
        assert not read_graph(workspace, 'alpha')['nodes']
        assert marker.read_bytes() == b'old graph stays on disk'
        assert Path(workspace.resolved_config('alpha').output_dir) != old_output
    finally:
        manager.shutdown()


@pytest.mark.parametrize('entry', ['pointer', 'generation_parent'])
def test_output_generation_rejects_symlinks(workspace, tmp_path, entry):
    import os
    outside = tmp_path / 'outside'
    outside.mkdir()
    project = workspace.project_path('alpha')
    target = (project / 'output-head.yaml' if entry == 'pointer'
              else project / 'output/generations')
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows symlink creation requires Developer Mode')
        raise
    with pytest.raises(LightGraphError, match='workspace_symlink_forbidden'):
        if entry == 'pointer':
            workspace.resolved_config('alpha')
        else:
            workspace.select_output_generation('alpha', 'run_' + '2' * 24, create=True)
    assert not list(outside.iterdir())


def test_concurrent_fresh_starts_create_only_one_active_lineage(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    real_popen = subprocess.Popen
    def child(*args, **kwargs):
        return real_popen([sys.executable, '-c', 'import sys,time; sys.stdin.buffer.read(); time.sleep(30)'], **kwargs)
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)
    barrier = threading.Barrier(2)
    manager = RunManager(workspace, cancel_timeout=.05)
    def start():
        barrier.wait(timeout=3)
        try:
            return manager.start('alpha', run_mode='fresh')
        except LightGraphError as error:
            return str(error)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        running = [result for result in results if isinstance(result, dict)]
        assert len(running) == 1 and 'project_run_active' in results
        assert workspace.output_generation('alpha') == running[0]['run_id']
        assert len(list((workspace.project_path('alpha') / 'output/generations').iterdir())) == 1
        manager.cancel('alpha', running[0]['run_id'])
    finally:
        manager.shutdown()

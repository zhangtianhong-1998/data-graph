"""Run baselines identify code and authorized inputs without exporting their contents."""
import json

import pytest
import yaml

from lightgraph.construction.checkpoints import commit_checkpoint
from lightgraph.errors import LightGraphError
from lightgraph.runtime.run_baseline import capture_baseline
from lightgraph.web.workspace import Workspace


@pytest.fixture
def baseline_input(tmp_path):
    repo = tmp_path / 'repo'
    code = repo / 'src/lightgraph/example.py'
    code.parent.mkdir(parents=True)
    code.write_text('version = 1\n')
    frontend = repo / 'src/workbench/dist'
    frontend.mkdir(parents=True)
    (frontend / 'app.js').write_text('const version = 1;')
    data = tmp_path / 'data.csv'
    data.write_text('code,name\n1,PRIVATE-ROW-CONTENT\n')
    config = tmp_path / 'run.yaml'
    config.write_text(yaml.safe_dump({
        'sources': [{'id': 'file', 'kind': 'csv_file', 'path': 'data.csv', 'encoding': 'utf-8',
                     'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'},
                    {'id': 'db', 'kind': 'mysql_database', 'dsn_env': 'BASELINE_TEST_DSN',
                     'allowed_tables': ['public.items']}],
        'output_dir': 'output', 'model': {'api_key': 'PRIVATE-API-KEY'},
    }))
    return config, repo, data, code


def test_fingerprints_follow_files_permissions_and_frontend_without_copying_secrets(baseline_input, monkeypatch):
    config, repo, data, code = baseline_input
    monkeypatch.setenv('BASELINE_TEST_DSN', 'mysql://user:PRIVATE-PASSWORD@private-host/private-db')
    first = capture_baseline(config, repo=repo, run_id='experiment')
    text = json.dumps(first)
    assert all(secret not in text for secret in ('PRIVATE-ROW-CONTENT', 'PRIVATE-API-KEY', 'PRIVATE-PASSWORD', 'private-host'))
    assert first['live_database_reads'] == 0
    assert first['database_sources'][0]['snapshot_verified'] is False
    assert first['observer_process']['remote_service_verified'] is False
    assert first['observer_process']['repository_import_match'] is False
    assert first['frontend']['present'] is True
    code.write_text('version = 2\n')
    data.write_text('code,name\n2,CHANGED-PRIVATE-ROW\n')
    second = capture_baseline(config, repo=repo, run_id='experiment')
    assert first['source']['working_tree_sha256'] != second['source']['working_tree_sha256']
    assert first['inputs'] != second['inputs']
    assert first['baseline_id'] != second['baseline_id']
    modified = yaml.safe_load(config.read_text())
    modified['sources'][1]['allowed_tables'] = ['public.other']
    config.write_text(yaml.safe_dump(modified))
    third = capture_baseline(config, repo=repo)
    assert third['permissions'] != second['permissions']


def test_workbench_baseline_follows_active_output_generation(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Project', 'project')
    run_id = 'run_' + 'a' * 24
    workspace.select_output_generation('project', run_id, create=True)
    config = workspace.resolved_config('project')
    graph = {'nodes': [{'id': 'PRIVATE-ENTITY-ID'}], 'edges': []}
    commit_checkpoint(config.output_dir, run_id, 'unit', {'graph': graph}, input_digest='frozen')
    baseline = capture_baseline(workspace.project_path('project') / 'config.yaml', repo=tmp_path)
    assert baseline['configuration']['origin'] == 'workbench_config'
    assert baseline['artifacts']['output_dir'] == config.output_dir
    assert baseline['artifacts']['checkpoints'][0]['node_id'] == 'unit'
    assert 'PRIVATE-ENTITY-ID' not in json.dumps(baseline)


def test_baseline_rejects_input_replaced_during_capture(baseline_input, monkeypatch):
    from lightgraph.runtime import run_baseline
    config, repo, data, _ = baseline_input
    read = run_baseline._Files.read
    changed = False

    def replace_after_read(self, path):
        nonlocal changed
        result = read(self, path)
        if str(path) == str(data) and not changed:
            changed = True
            data.write_text('code,name\n3,changed-during-capture\n')
        return result

    monkeypatch.setattr(run_baseline._Files, 'read', replace_after_read)
    with pytest.raises(LightGraphError, match='baseline_input_changed'):
        capture_baseline(config, repo=repo)


def test_baseline_rejects_checkpoint_added_during_capture(baseline_input, monkeypatch):
    from lightgraph.runtime import run_baseline
    config, repo, _, _ = baseline_input
    checkpoint_root = config.parent / 'output/checkpoints'
    checkpoint_root.mkdir(parents=True)
    artifacts = run_baseline._artifacts

    def add_after_scan(config, files):
        result = artifacts(config, files)
        (checkpoint_root / 'checkpoint.json').write_text('{}')
        return result

    monkeypatch.setattr(run_baseline, '_artifacts', add_after_scan)
    with pytest.raises(LightGraphError, match='baseline_input_changed'):
        capture_baseline(config, repo=repo)

"""Run the CLI with its original files, real isolated workers and published checkpoints."""
import json
from pathlib import Path

import pytest
import yaml

from lightgraph import cli
from lightgraph.construction.workflow import load_workflow_state
from lightgraph.runtime.config import load_config
from lightgraph.tests.construction_worker_fixture import install_scripted_worker
from lightgraph.web.construction_graph import load_graph


@pytest.fixture
def standalone(tmp_path):
    config_dir = tmp_path / 'configuration'
    inputs = tmp_path / 'inputs'
    config_dir.mkdir()
    inputs.mkdir()
    path = config_dir / 'parallel-input.yaml'
    cfg = {
        'goal': {'purpose': '提取两个目录的指标'},
        'model': {'mode': 'in_environment', 'model': 'scripted-test', 'base_url': 'http://127.0.0.1:9999/v1'},
        'output_dir': '../results/published-data',
        'sources': [],
        'workflow': {'enabled': True, 'max_parallel_workers': 2, 'units': [], 'edges': [],
                     'merge_nodes': [{'node_id': 'merge', 'merge_mode': 'deterministic'}]},
    }
    for name in ('east', 'west'):
        (inputs / (name + '.csv')).write_text(f'metric_code,metric_name\n{name},Name {name}\n')
        cfg['sources'].append({'id': name, 'kind': 'csv_file', 'path': f'../inputs/{name}.csv',
                               'encoding': 'utf-8', 'delimiter': ',', 'header': True,
                               'null_values': [''], 'type_inference': 'text'})
        cfg['workflow']['units'].append({'unit_id': name, 'source_ids': [name]})
        cfg['workflow']['edges'].append({'edge_id': name, 'source': name, 'target': 'merge'})
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    return path, cfg


@pytest.mark.parametrize('command', ['run', 'build-graph'])
def test_standalone_yaml_runs_both_scoped_workers_and_publishes(standalone, command, monkeypatch, capsys):
    install_scripted_worker(monkeypatch)
    path, _ = standalone
    original = path.read_bytes()
    exit_code = cli.main([command, str(path)])
    record = json.loads(capsys.readouterr().out)
    assert exit_code == 0, record
    config = load_config(path)
    state = load_workflow_state(config.output_dir, record['run_id'])
    assert record['workflow_state']['parent_run_id'] == record['run_id']
    assert state['merge']['publication_status'] == 'published'
    for unit in ('east', 'west'):
        assert state['artifacts'][unit]['artifact_status'] == 'committed'
        assert state['units'][unit]['status'] == 'completed'
        assert state['units'][unit]['nodes'] == 1
    assert state['artifacts']['merge']['upstream_artifacts'] == [
        state['artifacts'][unit]['artifact_id'] for unit in ('east', 'west')]
    graph = load_graph(config)
    assert {node['label'] for node in graph['nodes']} == {'Name east', 'Name west'}
    assert path.read_bytes() == original
    assert not list(path.parent.parent.rglob('project.yaml'))


def test_standalone_yaml_rejects_cross_project_inheritance_before_writing(standalone, capsys):
    path, cfg = standalone
    cfg['inherited_graphs'] = [{'inherit_id': 'base', 'source_project_id': 'another', 'allowed_scopes': ['public']}]
    path.write_text(yaml.safe_dump(cfg))
    assert cli.main(['run', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'inherited_graph_workspace_required'
    assert not Path(load_config(path).output_dir).exists()


def test_standalone_rechecks_yaml_before_publication(standalone, capsys, monkeypatch):
    from lightgraph.construction import workflow
    path, cfg = standalone
    install_scripted_worker(monkeypatch)
    load_unit = workflow.load_unit_contributor

    def change_after_extract(*args, **kwargs):
        contributor = load_unit(*args, **kwargs)
        cfg['scopes'] = ['narrowed']
        path.write_text(yaml.safe_dump(cfg))
        return contributor

    monkeypatch.setattr(workflow, 'load_unit_contributor', change_after_extract)
    assert cli.main(['run', str(path)]) in (1, 2)
    record = json.loads(capsys.readouterr().out)
    assert record['error_code'] == 'input_dependency_changed'
    assert record['workflow_state']['merge']['publication_status'] != 'published'


def test_standalone_does_not_claim_managed_output(standalone, capsys, tmp_path):
    from lightgraph.web.workspace import Workspace
    path, cfg = standalone
    workspace = Workspace(tmp_path / 'owned')
    workspace.create('Managed', 'existing')
    cfg['output_dir'] = str(workspace.project_path('existing') / 'output')
    path.write_text(yaml.safe_dump(cfg))
    assert cli.main(['build-graph', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'workflow_requires_project_workspace'
    assert not (Path(cfg['output_dir']) / 'runs').exists()


def test_standalone_manual_merge_verifies_producer_inputs(standalone, monkeypatch, capsys):
    path, cfg = standalone
    cfg['workflow'].update(edges=[], merge_nodes=[], legacy_merge=False)
    path.write_text(yaml.safe_dump(cfg))
    install_scripted_worker(monkeypatch)
    assert cli.main(['run', str(path)]) == 0
    capsys.readouterr()
    assert cli.main(['merge-graphs', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['nodes'] == 2
    current = load_graph(load_config(path))['version_id']
    cfg['sources'][0]['null_values'].append('NA')
    path.write_text(yaml.safe_dump(cfg))
    assert cli.main(['merge-graphs', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'workflow_resume_input_changed'
    assert load_graph(load_config(path))['version_id'] == current


@pytest.mark.parametrize('keep_pinned_peer', [False, True])
def test_manual_merge_cannot_publish_unverified_legacy_unit(standalone, monkeypatch, capsys, keep_pinned_peer):
    import shutil
    from lightgraph.construction.workspace import read_internal_yaml, write_yaml
    path, cfg = standalone
    cfg['workflow'].update(edges=[], merge_nodes=[], legacy_merge=False)
    path.write_text(yaml.safe_dump(cfg))
    install_scripted_worker(monkeypatch)
    assert cli.main(['run', str(path)]) == 0
    run_id = json.loads(capsys.readouterr().out)['run_id']
    output = Path(load_config(path).output_dir)
    shutil.copytree(output / 'attempts' / run_id / 'west', output / 'units' / 'west')
    manifest = output / 'construction' / 'manifest.yaml'
    if keep_pinned_peer:
        value = read_internal_yaml(manifest)
        value['current_graph']['workflow_units'].pop('west')
        write_yaml(manifest, value)
    else:
        manifest.unlink()
    before = manifest.read_bytes() if keep_pinned_peer else None
    (path.parent.parent / 'inputs' / 'west.csv').write_text('metric_code,metric_name\nwest,New Name\n')
    assert cli.main(['merge-graphs', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'workflow_checkpoint_missing'
    assert (manifest.read_bytes() if manifest.exists() else None) == before

"""Offline structure exports retain checks without claiming instance validation."""
import json
from pathlib import Path

import pytest

from lightgraph import cli
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.structure import publish_structure, save_structure_check
from lightgraph.construction.structure_export import load_published_structures
from lightgraph.construction.workspace import GraphWorkspace, safe_load_yaml, write_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig


@pytest.fixture
def published(tmp_path):
    config = RunConfig(sources=[{'id': 's', 'kind': 'gauss_database',
        'allow_fact_data': False, 'allowed_tables': ['public.items']}],
        output_dir=str(tmp_path / 'output'), construction_target='structure')
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code')])
    workspace = GraphWorkspace(Path(config.output_dir) / 'construction', 'recorded-context')
    workspace.save_draft({'draft_id': 'items', 'entities': [{'type': 'Item', 'key_names': ['id'],
        'mappings': [{'source_id': 's', 'table': 'items', 'key_columns': ['code']}]}]})
    save_structure_check(workspace, 'items', [table], access=access_fingerprint(config), goal=config.goal)
    publish_structure(workspace, 'items', access=access_fingerprint(config))
    return config, workspace, table


def test_export_is_read_only_and_preserves_exact_published_plans(published, monkeypatch):
    config, workspace, table = published
    before = {str(path.relative_to(workspace.root)): path.read_bytes()
              for path in workspace.root.rglob('*') if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('offline export must neither query nor write workspace records')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', forbidden)
    monkeypatch.setattr(GraphWorkspace, '_save_manifest', forbidden)
    monkeypatch.setattr(GraphWorkspace, '_append_record', forbidden)
    result = load_published_structures(config)
    assert result['target'] == 'graph_structure'
    assert result['data_validation'] == result['instance_enumeration'] == 'not_run'
    assert 'nodes' not in result and 'edges' not in result
    entry = workspace._manifest()['structure_publications']['items']
    assert result['plans'] == [workspace._read_record(entry['path'])]
    assert result['plans'][0]['context_fingerprint'] == 'recorded-context'
    assert {str(path.relative_to(workspace.root)): path.read_bytes()
            for path in workspace.root.rglob('*') if path.is_file()} == before


def test_cli_exports_structure_without_connecting_sources(published, tmp_path, monkeypatch, capsys):
    config, _, _ = published
    config_path, output = tmp_path / 'config.yaml', tmp_path / 'structure.yaml'
    write_yaml(config_path, config)
    def forbidden(*args, **kwargs):
        raise AssertionError('export must not create a database connection')
    monkeypatch.setattr(cli, 'open_sources', forbidden)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', forbidden)
    assert cli.main(['export-structure', str(config_path), str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['target'] == 'graph_structure' and result['plans'] == 1
    assert result['data_validation'] == 'not_run' and result['format'] == 'yaml'
    exported = safe_load_yaml(output.read_text(encoding='utf-8'))
    assert exported['plans'][0]['draft']['entities'][0]['type'] == 'Item'
    assert exported['plans'][0]['metrics']['fact_queries'] == 0


@pytest.mark.parametrize('present', [False, True])
def test_no_publications_has_a_clear_code_and_does_not_create_workspace(tmp_path, present):
    config = RunConfig(sources=[], output_dir=str(tmp_path / 'empty'))
    root = Path(config.output_dir) / 'construction'
    if present:
        GraphWorkspace(root, 'empty-context')
    with pytest.raises(LightGraphError, match='structure_publication_required'):
        load_published_structures(config)
    assert root.exists() is present


def test_cli_missing_publication_reports_code_without_writing_export(tmp_path, capsys):
    config_path, output = tmp_path / 'config.yaml', tmp_path / 'structure.yaml'
    write_yaml(config_path, RunConfig(sources=[], output_dir=str(tmp_path / 'empty')))
    assert cli.main(['export-structure', str(config_path), str(output)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'structure_publication_required'
    assert not output.exists() and not (tmp_path / 'empty').exists()


@pytest.mark.parametrize('change', ['revision', 'context', 'digest', 'published', 'access'])
def test_rejects_stale_tampered_or_unauthorized_plans(published, change):
    config, workspace, _ = published
    manifest = workspace._manifest()
    if change in {'revision', 'context'}:
        value = workspace.get_draft('items').draft.model_dump(mode='json', by_alias=True)
        value['purpose'] = 'new goal'
        if change == 'context':
            workspace = GraphWorkspace(workspace.root, 'new-context')
        workspace.save_draft(value)
    elif change == 'access':
        config.sources[0].allowed_columns = {'public.items': []}
    else:
        entry = (manifest['structure_checks']['items']['current'] if change == 'digest'
                 else manifest['structure_publications']['items'])
        path = workspace.root / entry['path']
        artifact = safe_load_yaml(path.read_text(encoding='utf-8'))
        artifact['draft']['purpose'] = 'tampered'
        write_yaml(path, artifact)
    with pytest.raises(LightGraphError) as error:
        load_published_structures(config)
    assert error.value.code == {
        'revision': 'structure_publication_required', 'context': 'structure_publication_required',
        'access': 'graph_access_context_changed', 'digest': 'validated_structure_mismatch',
        'published': 'structure_publication_required'}[change]


@pytest.mark.parametrize('record_kind', ['publication', 'check', 'draft'])
def test_manifest_record_paths_cannot_escape_workspace(published, tmp_path, record_kind):
    config, workspace, _ = published
    manifest = workspace._manifest()
    if record_kind == 'publication':
        entry, field = manifest['structure_publications']['items'], 'path'
    elif record_kind == 'check':
        entry, field = manifest['structure_checks']['items']['current'], 'path'
    else:
        entry, field = manifest['drafts']['items'], 'current'
    outside = tmp_path / 'outside.yaml'
    outside.write_bytes((workspace.root / entry[field]).read_bytes())
    entry[field] = str(outside)
    workspace._save_manifest(manifest)
    with pytest.raises(LightGraphError, match='invalid_graph_workspace_record'):
        load_published_structures(config)


def test_symlinked_manifest_is_not_trusted(published, tmp_path):
    config, workspace, _ = published
    outside = tmp_path / 'outside.yaml'
    outside.write_bytes(workspace.manifest_path.read_bytes())
    workspace.manifest_path.unlink()
    try:
        workspace.manifest_path.symlink_to(outside)
    except OSError:
        pytest.skip('platform does not permit creating test symlinks')
    with pytest.raises(LightGraphError, match='workspace_path_invalid'):
        load_published_structures(config)


def test_export_keeps_all_current_published_plans(published):
    config, workspace, table = published
    draft = workspace.get_draft('items').draft.model_dump(mode='json', by_alias=True)
    draft['draft_id'] = 'other'
    workspace.save_draft(draft)
    save_structure_check(workspace, 'other', [table], access=access_fingerprint(config))
    publish_structure(workspace, 'other', access=access_fingerprint(config))
    assert [plan['draft_id'] for plan in load_published_structures(config)['plans']] == ['items', 'other']

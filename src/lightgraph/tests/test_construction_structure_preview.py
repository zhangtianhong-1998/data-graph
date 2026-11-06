"""Checked plans remain inspectable after failed instances and later revisions."""
from pathlib import Path

import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.structure import save_structure_check
from lightgraph.construction.structure_preview import load_structure_preview
from lightgraph.construction.workspace import GraphWorkspace, safe_load_yaml, write_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.web_client import RevisionClient
from lightgraph.web.app import create_app


def populate(config):
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code')])
    workspace = GraphWorkspace(Path(config.output_dir) / 'construction', 'observed-context')
    draft = {'draft_id': 'full', 'purpose': '完整结构目标', 'entities': [
        {'type': f'Type{index}', 'key_names': ['code'], 'mappings': [
            {'source_id': 's', 'table': 'items', 'key_columns': ['code']}]} for index in range(9)],
        'relations': [{'type': f'Relation{index}', 'source_type': f'Type{index}',
            'target_type': f'Type{index + 1}', 'mappings': [{'source_id': 's', 'table': 'items',
                'source_key_columns': ['code'], 'target_key_columns': ['code']}]} for index in range(5)]}
    workspace.save_draft(draft)
    check = save_structure_check(workspace, 'full', [table], access=access_fingerprint(config))
    # A source capability failure changes instance state, not the saved plan.
    workspace.record_validation('full', revision=1, passed=False,
        diagnostics=[{'code': 'runtime_capability_unverified'}], metrics={'nodes': 0, 'mappings_checked': 0})
    return workspace, table, draft, check


@pytest.fixture
def checked(tmp_path):
    config = RunConfig(sources=[{'id': 's', 'kind': 'gauss_database', 'allow_fact_data': False,
                                'allowed_tables': ['public.items']}], output_dir=str(tmp_path / 'output'))
    return config, *populate(config)


def test_failed_instances_do_not_hide_unpublished_checked_structure(checked, monkeypatch):
    config, workspace, _, _, _ = checked
    before = {path: path.read_bytes() for path in workspace.root.rglob('*') if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('preview must not connect or write')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', forbidden)
    monkeypatch.setattr(GraphWorkspace, '_save_manifest', forbidden)
    monkeypatch.setattr(GraphWorkspace, '_append_record', forbidden)
    result = load_structure_preview(config)
    assert result['status'] == 'available'
    assert result['data_validation'] == result['instance_enumeration'] == 'not_run'
    assert result['selected']['passed'] and result['selected']['preview_only']
    assert result['selected']['entity_type_count'] == 9 and result['selected']['relation_type_count'] == 5
    assert len(result['selected']['draft']['entities']) == 9
    assert result['selected']['is_current_revision'] and not result['selected']['historical']
    assert workspace._manifest().get('structure_publications', {}) == {}
    assert {path: path.read_bytes() for path in workspace.root.rglob('*') if path.is_file()} == before


def test_history_survives_new_smaller_draft_and_is_explicitly_selectable(checked):
    config, workspace, table, draft, _ = checked
    draft['entities'] = draft['entities'][:1]; draft['relations'] = []
    workspace.save_draft(draft)
    historical = load_structure_preview(config)
    assert historical['selected']['revision'] == 1 and historical['selected']['historical']
    save_structure_check(workspace, 'full', [table], access=access_fingerprint(config))
    current = load_structure_preview(config)
    assert current['selected']['revision'] == 2 and current['selected']['entity_type_count'] == 1
    assert len(current['plans']) == 2
    old = load_structure_preview(config, draft_id='full', revision=1)
    assert old['selected']['entity_type_count'] == 9 and old['selected']['historical']
    assert workspace.get_draft('full').revision == 2


def test_failed_structure_check_is_visible_with_its_diagnostics(checked):
    config, workspace, table, draft, _ = checked
    draft['entities'][0]['mappings'][0]['label_column'] = 'absent'
    workspace.save_draft(draft)
    save_structure_check(workspace, 'full', [table], access=access_fingerprint(config))
    selected = load_structure_preview(config)['selected']
    assert not selected['passed'] and selected['diagnostics'][0]['code'] == 'unauthorized_column'
    assert selected['data_validation'] == 'not_run'


def test_old_draft_without_new_mapping_defaults_keeps_signed_structure_shape(checked):
    from lightgraph.contracts import digest
    config, workspace, table, _, _ = checked
    path = workspace.root / workspace._manifest()['drafts']['full']['current']
    payload = safe_load_yaml(path.read_text(encoding='utf-8'))
    for entity in payload['draft']['entities']:
        for mapping in entity['mappings']:
            mapping.pop('kind', None)
    for relation in payload['draft']['relations']:
        for mapping in relation['mappings']:
            for key in ('source_metadata', 'target_metadata', 'metadata_basis'):
                mapping.pop(key, None)
    payload['fingerprint'] = digest(payload['draft'])
    write_yaml(path, payload)
    result = save_structure_check(workspace, 'full', [table], access=access_fingerprint(config))
    assert result['passed']
    selected = load_structure_preview(config)['selected']
    assert selected['passed'] and selected['draft'] == payload['draft']


def test_empty_preview_does_not_create_a_workspace(tmp_path):
    config = RunConfig(sources=[], output_dir=str(tmp_path / 'empty'))
    assert load_structure_preview(config)['selected'] is None
    assert not Path(config.output_dir).exists()
    with pytest.raises(LightGraphError, match='structure_preview_not_found'):
        load_structure_preview(config, draft_id='missing')


@pytest.mark.parametrize('selection', [{'revision': 1}, {'draft_id': 'full', 'revision': 0},
                                     {'draft_id': 'full', 'revision': True}])
def test_rejects_invalid_selection(checked, selection):
    with pytest.raises(LightGraphError, match='invalid_structure_selection'):
        load_structure_preview(checked[0], **selection)


def test_checks_artifact_digest_and_rejects_path_escape(checked, tmp_path):
    config, workspace, _, _, check = checked
    path = workspace.root / check['structure_artifact']
    original = path.read_bytes()
    artifact = safe_load_yaml(path.read_text(encoding='utf-8'))
    artifact['draft']['purpose'] = 'UNTRUSTED-CONTENT'
    write_yaml(path, artifact)
    with pytest.raises(LightGraphError, match='validated_structure_mismatch'):
        load_structure_preview(config)
    path.write_bytes(original)
    outside = tmp_path / 'outside.yaml'; outside.write_bytes(original)
    manifest = workspace._manifest()
    manifest['structure_checks']['full']['history'][0]['path'] = str(outside)
    workspace._save_manifest(manifest)
    with pytest.raises(LightGraphError, match='invalid_graph_workspace_record'):
        load_structure_preview(config)


@pytest.mark.parametrize('kind', ['manifest', 'artifact'])
def test_symlinks_cannot_redirect_preview_records(checked, tmp_path, kind):
    config, workspace, _, _, check = checked
    path = workspace.manifest_path if kind == 'manifest' else workspace.root / check['structure_artifact']
    outside = tmp_path / 'outside.yaml'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip('platform does not permit creating test symlinks')
    with pytest.raises(LightGraphError, match='workspace_path_invalid' if kind == 'manifest' else 'invalid_graph_workspace_record'):
        load_structure_preview(config)


def test_revoked_history_is_hidden_and_does_not_block_new_authorized_version(checked):
    config, workspace, table, draft, _ = checked
    config.sources[0].allowed_columns = {'public.items': ['code']}
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        load_structure_preview(config)
    draft['purpose'] = '当前授权的方案'
    workspace.save_draft(draft)
    save_structure_check(workspace, 'full', [table], access=access_fingerprint(config))
    result = load_structure_preview(config)
    assert [plan['revision'] for plan in result['plans']] == [2]
    assert '完整结构目标' not in str(result)
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        load_structure_preview(config, draft_id='full', revision=1)


@pytest.fixture
def api(tmp_path):
    with RevisionClient(create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'no-dist')) as client:
        for pid in ('alpha', 'beta'):
            assert client.post('/api/projects', json={'name': pid, 'project_id': pid}).status_code == 201
        config = client.get('/api/projects/alpha/config').json()
        config['sources'] = [{'id': 's', 'kind': 'gauss_database', 'allow_fact_data': False,
                              'allowed_tables': ['public.items'], 'dsn_env': 'UNUSED_TEST_DSN'}]
        response = client.put('/api/projects/alpha/config', json=config)
        assert response.status_code == 200, response.text
        current = client.app.state.workspace.resolved_config('alpha')
        workspace, _, _, _ = populate(current)
        yield client, workspace


def test_api_reads_empty_instance_project_and_keeps_project_isolation(api, monkeypatch):
    client, _ = api
    def forbidden(*args, **kwargs):
        raise AssertionError('preview must not open sources')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', forbidden)
    response = client.get('/api/projects/alpha/graph/structure')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['selected']['entity_type_count'] == 9
    assert client.get('/api/projects/beta/graph/structure').json()['selected'] is None
    assert client.get('/api/projects/beta/graph/structure?draft_id=full&revision=1').status_code == 404
    assert client.get('/api/projects/missing/graph/structure').status_code == 404


def test_api_permission_change_denies_old_content(api):
    client, _ = api
    config = client.get('/api/projects/alpha/config').json()
    config['goal']['excluded_columns'] = ['code']
    assert client.put('/api/projects/alpha/config', json=config).status_code == 200
    response = client.get('/api/projects/alpha/graph/structure')
    assert response.status_code == 400 and response.json()['error']['code'] == 'graph_access_context_changed'
    assert 'Type0' not in response.text and '完整结构目标' not in response.text


def test_api_rechecks_configuration_before_returning_preview(api, monkeypatch):
    from lightgraph.construction import structure_preview
    client, _ = api
    original = structure_preview.load_structure_preview
    def concurrent_change(config, **selection):
        result = original(config, **selection)
        workspace = client.app.state.workspace
        current = workspace.get_config('alpha')
        current['goal']['excluded_columns'] = ['code']
        workspace.save_config('alpha', current)
        return result
    monkeypatch.setattr(structure_preview, 'load_structure_preview', concurrent_change)
    response = client.get('/api/projects/alpha/graph/structure')
    assert response.status_code == 409 and response.json()['error']['code'] == 'graph_read_conflict'
    assert 'Type0' not in response.text

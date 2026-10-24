"""Input deletion cannot mutate a database, broaden scopes, or erase graph history."""
from copy import deepcopy
from io import BytesIO

import openpyxl
import pytest
from fastapi.testclient import TestClient

from lightgraph.web.app import create_app
from lightgraph.web.execution import read_metadata
from lightgraph.web.metadata_views import preview_file_metadata


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'none')) as client:
        client.post('/api/projects', json={'name': 'A', 'project_id': 'alpha'})
        client.post('/api/projects', json={'name': 'B', 'project_id': 'beta'})
        client.headers['X-Workbench-Session'] = client.post('/api/projects/alpha/session').json()['session_id']
        yield client


def upload(client, name, content):
    response = client.post('/api/projects/alpha/files', files={'file': (name, content)})
    assert response.status_code == 201, response.text
    return response.json()


def db(client, tables=None, sid='db'):
    config = client.get('/api/projects/alpha/config').json()
    config['sources'].append({'id': sid, 'kind': 'gauss_database', 'dsn_env': 'UNSET_DELETE_DSN',
                              'allowed_tables': tables or ['public.items', 'public.other'],
                              'allowed_columns': {'public.items': ['code']}})
    result = client.put('/api/projects/alpha/config', json=config)
    assert result.status_code == 200, result.text
    return result.json()


def test_delete_project_removes_only_project_and_sessions(client):
    workspace = client.app.state.workspace
    upload(client, 'doc.md', b'# doc')
    path = workspace.project_path('alpha')
    (path / '.connections').mkdir()
    (path / '.connections/secret.enc').write_bytes(b'encrypted profile')
    (path / 'output/graph.sqlite').write_bytes(b'project result')
    beta = workspace.project_path('beta') / 'output/keep'
    beta.write_text('keep')
    old_session = client.headers['X-Workbench-Session']
    second = client.post('/api/projects/alpha/session').json()['session_id']
    assert client.delete('/api/projects/alpha').json() == {'deleted': True, 'project_id': 'alpha'}
    assert not path.exists() and beta.read_text() == 'keep'
    assert client.get('/api/projects/alpha').status_code == 404
    client.post('/api/projects', json={'name': 'new', 'project_id': 'alpha'})
    for token in [old_session, second]:
        assert client.get('/api/projects/alpha/credentials', headers={'X-Workbench-Session': token}).status_code == 400


@pytest.mark.parametrize('selection', ['project', 'file', 'source', 'table'])
def test_delete_needs_own_session_and_idle_project(client, monkeypatch, selection):
    file = upload(client, 'data.csv', b'code\nA\n')
    db(client)
    url = {'project': '/api/projects/alpha', 'file': f"/api/projects/alpha/files/{file['file_id']}",
           'source': '/api/projects/alpha/sources/db', 'table': '/api/projects/alpha/sources/db/tables'}[selection]
    data = {'table': 'public.items'} if selection == 'table' else None
    def request(headers):
        return client.request('DELETE', url, json=data, headers=headers)
    assert request({'X-Workbench-Session': 'invalid'}).status_code == 400
    other = client.post('/api/projects/beta/session').json()['session_id']
    assert request({'X-Workbench-Session': other}).status_code == 400
    monkeypatch.setattr(client.app.state.manager, 'active', lambda pid: pid == 'alpha')
    assert request({}).status_code == 409
    assert client.app.state.workspace.file_path('alpha', file['file_id']).exists()


def test_csv_delete_removes_config_preview_keeps_other_metadata_and_history(client, monkeypatch):
    first = upload(client, 'first.csv', b'code\nA\n')
    second = upload(client, 'second.csv', b'code\nB\n')
    workspace = client.app.state.workspace
    preview_file_metadata(workspace, 'alpha', first['file_id'])
    preview_file_metadata(workspace, 'alpha', second['file_id'])
    path = workspace.project_path('alpha')
    (path / 'runs/run_history.json').write_text('{"status":"completed"}')
    (path / 'output/retained-result.json').write_text('published result')
    import lightgraph.runtime.config as runtime
    monkeypatch.setattr(runtime, 'open_sources', lambda *_a, **_k: pytest.fail('deletion must not query sources'))
    response = client.delete(f"/api/projects/alpha/files/{first['file_id']}")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['removed_file_ids'] == [first['file_id']]
    assert len(value['config']['sources']) == 1
    assert not (path / first['relative_path']).exists()
    assert not (path / f"metadata/{first['file_id']}.yaml").exists()
    assert (path / second['relative_path']).exists()
    assert (path / 'runs/run_history.json').exists()
    assert (path / 'output/retained-result.json').read_text() == 'published result'
    metadata = read_metadata(workspace, 'alpha')
    assert [table['name'] for table in metadata['tables']] == ['second']
    assert not metadata['requires_refresh']
    assert client.delete(f"/api/projects/alpha/files/{first['file_id']}").status_code == 404


def test_database_table_delete_never_leaves_empty_authorization(client):
    db(client)
    first = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert first.status_code == 200, first.text
    source = first.json()['config']['sources'][0]
    assert source['allowed_tables'] == ['public.other'] and source['allowed_columns'] == {}
    assert first.json()['removed_source_ids'] == []
    last = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.other'})
    assert last.status_code == 200, last.text
    assert last.json()['config']['sources'] == []
    assert last.json()['removed_source_ids'] == ['db']


def test_source_delete_clears_saved_credentials_and_all_session_copies(client):
    db(client)
    ws = client.app.state.workspace
    config = ws.get_config('alpha')
    config['sources'][0].update(credential_mode='session', credential_revision='r1')
    ws.save_config('alpha', config)
    source = ws.resolved_config('alpha').sources[0]
    second = client.post('/api/projects/alpha/session').json()['session_id']
    store = client.app.state.credentials
    for token in [client.headers['X-Workbench-Session'], second]:
        store.set_source('alpha', token, source, 'SYNTHETIC_DSN')
    paths = ['.connections/db.enc', '.connection-drafts/source-db.enc']
    for relative in paths:
        path = ws.project_path('alpha') / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'fake encrypted data')
    result = client.delete('/api/projects/alpha/sources/db')
    assert result.status_code == 200, result.text
    for relative in paths:
        assert not (ws.project_path('alpha') / relative).exists()
    for token in [client.headers['X-Workbench-Session'], second]:
        assert store.resolve('alpha', token, ws.resolved_config('alpha'))[0] == {}


def test_table_deletion_deactivates_top_section_and_short_column_bindings(client):
    db(client)
    top = upload(client, 'top.md', b'---\npurpose: data_description\nscope: {tables: [public.items]}\n---\n# schema')
    section = upload(client, 'section.md', b'---\npurpose: data_description\nsections:\n  Notes:\n    scope: {tables: [db.public.items]}\n---\n# Notes\ncolumn guidance')
    column = upload(client, 'column.md', b'---\npurpose: data_description\nscope: {source_ids: [db], columns: [code]}\n---\n# code')
    global_doc = upload(client, 'global.md', b'# Global guidance')
    result = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert result.status_code == 200, result.text
    value = result.json()
    assert set(value['inactive_document_ids']) == {top['file_id'], section['file_id'], column['file_id']}
    assert value['config']['documents'] == [global_doc['relative_path']]
    file = next(f for f in value['project']['files'] if f['file_id'] == top['file_id'])
    assert file['scope']['tables'] == ['public.items'] and file['inactive_reason'] == 'binding_target_removed'
    # Explicit global rebinding is authorized; an implicit global conversion is not.
    rebound = client.patch(f"/api/projects/alpha/files/{top['file_id']}/binding", json={'purpose': 'data_description', 'scope': {}})
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()['inactive_reason'] is None
    assert top['relative_path'] in client.get('/api/projects/alpha/config').json()['documents']
    # Section-specific obsolete bindings keep the document inactive.
    rebound = client.patch(f"/api/projects/alpha/files/{section['file_id']}/binding", json={'purpose': 'data_description', 'scope': {}})
    assert rebound.status_code == 200 and rebound.json()['inactive_reason'] == 'binding_target_removed'


def test_excel_without_preview_deactivates_sheet_bindings_and_delete_whole_file(client):
    book = openpyxl.Workbook()
    book.active.title = 'Items'
    book.active.append(['code'])
    content = BytesIO()
    book.save(content)
    file = upload(client, 'book.xlsx', content.getvalue())
    doc = upload(client, 'bound.md', b'---\nscope: {tables: [Items]}\n---\n# schema')
    result = client.delete(f"/api/projects/alpha/files/{file['file_id']}")
    assert result.status_code == 200, result.text
    assert result.json()['inactive_document_ids'] == [doc['file_id']]


def test_markdown_yaml_deletion_detaches_config_and_capability_is_protected(client):
    doc = upload(client, 'doc.md', b'# notes')
    rules = upload(client, 'rules.yaml', b'rules: []')
    agent = upload(client, 'agent.yaml', b'framework: agentscope\nllm: {mode: disabled}')
    capability = upload(client, 'cap.yaml', b'product: gauss')
    config = db(client)
    config['sources'][0]['capability_manifest'] = capability['relative_path']
    config['rules'] = [rules['relative_path']]
    config['agent_config'] = agent['relative_path']
    config.pop('model')
    assert client.put('/api/projects/alpha/config', json=config).status_code == 200
    blocked = client.delete(f"/api/projects/alpha/files/{capability['file_id']}")
    assert blocked.status_code == 400 and blocked.json()['error']['code'] == 'file_in_use'
    for file in [doc, rules, agent]:
        response = client.delete(f"/api/projects/alpha/files/{file['file_id']}")
        assert response.status_code == 200, response.text
    config = client.get('/api/projects/alpha/config').json()
    assert config['documents'] == [] and config['rules'] == []
    assert config['agent_config'] is None and config['model']['mode'] == 'disabled'


def test_removing_source_detaches_rule_files_with_nested_target_refs(client):
    db(client)
    rules = upload(client, 'rules.yaml', b'rules:\n- relation:\n    target: {source_id: db, schema: public, table: items}')
    config = client.get('/api/projects/alpha/config').json()
    config['rules'] = [rules['relative_path']]
    client.put('/api/projects/alpha/config', json=config)
    response = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert response.status_code == 200, response.text
    assert response.json()['config']['rules'] == []
    assert response.json()['detached_rule_paths'] == [rules['relative_path']]
    assert client.app.state.workspace.file_path('alpha', rules['file_id']).exists()


def test_delete_rolls_back_on_file_unlink_failure(client, monkeypatch):
    from pathlib import Path
    file = upload(client, 'data.csv', b'code\nA\n')
    ws = client.app.state.workspace
    before = ws.get_config('alpha')
    path = ws.file_path('alpha', file['file_id'])
    unlink = Path.unlink
    def fail(target, *args, **kwargs):
        if target == path:
            raise OSError('cannot unlink')
        return unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    response = client.delete(f"/api/projects/alpha/files/{file['file_id']}")
    assert response.status_code == 500
    assert path.exists() and ws.get_config('alpha') == before
    assert ws.get('alpha')['files'][0]['file_id'] == file['file_id']


@pytest.mark.parametrize('project_delete', [True, False])
def test_delete_rejects_symlink_without_touching_external_files(client, tmp_path, project_delete):
    file = upload(client, 'data.csv', b'code\nA\n')
    ws = client.app.state.workspace
    path = ws.file_path('alpha', file['file_id'])
    outside = tmp_path / 'outside.csv'
    outside.write_bytes(b'KEEP')
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows account cannot create symlinks')
        raise
    url = '/api/projects/alpha' if project_delete else f"/api/projects/alpha/files/{file['file_id']}"
    assert client.delete(url).status_code == 400
    assert outside.read_bytes() == b'KEEP'


def test_table_delete_discards_stale_drafts_and_blocks_delayed_autosave(client):
    from lightgraph.tests.test_web_connection_drafts import draft_payload
    db(client)
    payload = draft_payload(20)
    payload['draft']['form'].update(id='db', tables='public.items\npublic.other')
    payload['draft']['checked'] = ['public.items', 'public.other']
    for key in ['source-db', 'new']:
        assert client.put('/api/projects/alpha/database/drafts/' + key, json=payload).status_code == 200
    result = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert result.status_code == 200, result.text
    payload['revision'] = 21
    for key in ['source-db', 'new']:
        path = '/api/projects/alpha/database/drafts/' + key
        assert client.get(path).json()['saved'] is False
        assert client.put(path, json=payload).json()['revision'] > 21
        assert client.get(path).json()['saved'] is False


@pytest.mark.parametrize('delete_file', [True, False])
def test_rule_file_dependency_closure_detaches_valid_cross_file_references(client, delete_file):
    import yaml
    from lightgraph.contracts import RuleSpec
    db(client)
    entity = RuleSpec(rule_id='entity', kind='column_group_entity',
                      source={'source_id': 'db', 'schema': 'public', 'table': 'items'},
                      identity={'key_columns': ['code']})
    target = RuleSpec(rule_id='target', kind='column_group_entity',
                      source={'source_id': 'db', 'schema': 'public', 'table': 'other'},
                      identity={'key_columns': ['code']})
    relation = RuleSpec(rule_id='rel', kind='reference',
                       source={'source_id': 'db', 'schema': 'public', 'table': 'other'},
                       identity={'key_columns': ['code']},
                       relation={'source_rule_id': 'entity', 'target_rule_id': 'target',
                                 'source_columns': ['code'], 'target_columns': ['code']})
    files = [upload(client, f'{rule.rule_id}.yaml', yaml.safe_dump(
        {'rules': [rule.model_dump(mode='json', by_alias=True)]}).encode()) for rule in [entity, target, relation]]
    config = client.get('/api/projects/alpha/config').json()
    config['rules'] = [f['relative_path'] for f in files]
    client.put('/api/projects/alpha/config', json=config)
    if delete_file:
        response = client.delete(f"/api/projects/alpha/files/{files[0]['file_id']}")
    else:
        response = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert response.status_code == 200, response.text
    assert response.json()['config']['rules'] == [files[1]['relative_path']]
    assert set(response.json()['detached_rule_paths']) == {files[0]['relative_path'], files[2]['relative_path']}
    assert client.app.state.workspace.file_path('alpha', files[2]['file_id']).exists()


def test_dotted_workbook_sheet_binding_is_deactivated(client):
    book = openpyxl.Workbook()
    book.active.title = 'items.v1'
    book.active.append(['code'])
    content = BytesIO()
    book.save(content)
    file = upload(client, 'book.xlsx', content.getvalue())
    doc = upload(client, 'bound.md', b'---\nscope: {tables: [public.items.v1]}\n---\n# schema')
    response = client.delete(f"/api/projects/alpha/files/{file['file_id']}")
    assert response.status_code == 200, response.text
    assert response.json()['inactive_document_ids'] == [doc['file_id']]


def test_invalid_inactive_doc_rebind_does_not_change_file_or_config(client):
    file = upload(client, 'items.csv', b'code\nA\n')
    other = upload(client, 'other.csv', b'code\nB\n')
    doc = upload(client, 'bound.md', b'---\npurpose: data_description\nscope: {tables: [items]}\n---\n# schema')
    ws = client.app.state.workspace
    preview_file_metadata(ws, 'alpha', file['file_id'])
    preview_file_metadata(ws, 'alpha', other['file_id'])
    assert client.delete(f"/api/projects/alpha/files/{file['file_id']}").status_code == 200
    original = ws.file_path('alpha', doc['file_id']).read_bytes()
    config = ws.get_config('alpha')
    response = client.patch(f"/api/projects/alpha/files/{doc['file_id']}/binding", json={
        'purpose': 'data_description', 'scope': {'tables': ['missing']}})
    assert response.status_code == 400, response.text
    assert ws.file_path('alpha', doc['file_id']).read_bytes() == original
    assert ws.get_config('alpha') == config
    response = client.patch(f"/api/projects/alpha/files/{doc['file_id']}/binding", json={
        'purpose': 'data_description', 'scope': {'tables': ['other']}})
    assert response.status_code == 200, response.text
    assert response.json()['inactive_reason'] is None


@pytest.mark.parametrize('selection', ['project', 'file'])
def test_windows_reparse_point_rejected_for_root_and_parent(client, monkeypatch, selection):
    from pathlib import Path
    file = upload(client, 'data.csv', b'code\nA\n')
    ws = client.app.state.workspace
    root = ws.project_path('alpha')
    reparse = root if selection == 'project' else root / 'files'
    original_lstat = Path.lstat
    class WindowsReparseStat:
        def __init__(self, original):
            self.original = original
            self.st_file_attributes = 0x400
        def __getattr__(self, name):
            return getattr(self.original, name)
    def lstat(path, *args, **kwargs):
        value = original_lstat(path, *args, **kwargs)
        return WindowsReparseStat(value) if path == reparse else value
    monkeypatch.setattr(Path, 'lstat', lstat)
    url = '/api/projects/alpha' if selection == 'project' else f"/api/projects/alpha/files/{file['file_id']}"
    response = client.delete(url)
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'workspace_symlink_forbidden'
    assert (root / file['relative_path']).exists()


def test_db_table_delete_prunes_cached_table_and_fk_without_querying(client, monkeypatch):
    from lightgraph.catalog import DeclaredCatalog, SourceDescription, CATALOG_VERSION
    from lightgraph.contracts import TableSpec, digest
    from lightgraph.web.execution import write_metadata_context
    db(client)
    ws = client.app.state.workspace
    tables = [TableSpec(source_id='db', schema='public', name=name, columns=[{'name': 'code'}],
                        foreign_keys=[{'name': 'link', 'columns': ['code'], 'target_schema': 'public',
                                       'target_table': 'items', 'target_columns': ['code']}] if name == 'other' else [])
              for name in ['items', 'other']]
    sources = [SourceDescription(source_id='db', product='GaussDB', constraint_coverage={})]
    catalog = DeclaredCatalog(tables=tables, sources=sources,
        revision=digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in tables], [s.model_dump() for s in sources]))
    ws._write_yaml(ws._path('alpha', 'output/metadata.yaml'), {'declared': catalog.model_dump(mode='json', by_alias=True)})
    write_metadata_context(ws.resolved_config('alpha'), ws.project_path('alpha'))
    import lightgraph.runtime.config as runtime
    monkeypatch.setattr(runtime, 'open_sources', lambda *_a, **_k: pytest.fail('deletion cannot query database'))
    result = client.request('DELETE', '/api/projects/alpha/sources/db/tables', json={'table': 'public.items'})
    assert result.status_code == 200, result.text
    view = read_metadata(ws, 'alpha')
    assert [table['name'] for table in view['tables']] == ['other']
    assert view['tables'][0]['foreign_keys'] == [] and not view['requires_refresh']


def test_doc_rebind_write_failure_restores_frontmatter_and_membership(client, monkeypatch):
    file = upload(client, 'items.csv', b'code\nA\n')
    doc = upload(client, 'bound.md', b'---\npurpose: data_description\nscope: {tables: [items]}\n---\n# schema')
    assert client.delete(f"/api/projects/alpha/files/{file['file_id']}").status_code == 200
    ws = client.app.state.workspace
    original_config = ws.get_config('alpha')
    path = ws.file_path('alpha', doc['file_id'])
    original_text = path.read_bytes()
    save = ws._save
    fail_once = True
    def fail(project):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError('save failed')
        return save(project)
    monkeypatch.setattr(ws, '_save', fail)
    response = client.patch(f"/api/projects/alpha/files/{doc['file_id']}/binding", json={'purpose': 'data_description', 'scope': {}})
    assert response.status_code == 500
    assert ws.get_config('alpha') == original_config
    assert path.read_bytes() == original_text
    assert ws.get('alpha')['files'][0]['inactive_reason'] == 'binding_target_removed'


@pytest.mark.parametrize('sid', ['analytics.db', '经营库'])
def test_imported_source_ids_remain_removable_without_profile_paths(client, sid):
    db(client, sid=sid)
    result = client.delete('/api/projects/alpha/sources/' + sid)
    assert result.status_code == 200, result.text
    assert result.json()['removed_source_ids'] == [sid]

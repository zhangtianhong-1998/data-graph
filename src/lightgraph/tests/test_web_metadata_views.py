"""Input previews must work independently of unavailable database connections."""
from io import BytesIO

import openpyxl
import pytest
import yaml

from lightgraph.errors import LightGraphError
from lightgraph.web.metadata_views import metadata_yaml, preview_file_metadata
from lightgraph.web.workspace import Workspace


def project(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.create("接入测试", "alpha")
    return workspace


def test_csv_preview_ignores_unavailable_database_and_never_exports_cells(tmp_path):
    workspace = project(tmp_path)
    file = workspace.add_file("alpha", "items.csv", b"code,label\nA,SAMPLE_CELL_SENTINEL\n")
    config = workspace.get_config("alpha")
    config["sources"].append({"id": "unavailable", "kind": "mysql_database",
                              "dsn_env": "UNSET_PREVIEW_DSN", "allowed_tables": ["test.items"]})
    workspace.save_config("alpha", config)
    result = preview_file_metadata(workspace, "alpha", file["file_id"])
    assert len(result["tables"]) == 1
    assert [c["name"] for c in result["tables"][0]["columns"]] == ["code", "label"]
    assert "SAMPLE_CELL_SENTINEL" not in str(result)
    catalog = yaml.safe_load(result["yaml"])
    assert catalog["artifact"] == "declared_metadata"
    assert {f["name"] for f in result["files"]} == {"tables.yaml", "constraints.yaml", "foreign_keys.yaml"}
    saved = list((workspace.project_path("alpha") / "metadata").glob("f_*.yaml"))
    assert len(saved) == 1
    assert "SAMPLE_CELL_SENTINEL" not in saved[0].read_text(encoding="utf-8")
    # A single input preview cannot pretend to be the complete project's catalog.
    cached = metadata_yaml(workspace, "alpha")
    assert cached["requires_refresh"] and cached["missing_sources"] == ["unavailable"]
    assert len(cached["files"]) == 3


def test_excel_preview_includes_each_visible_sheet_and_no_invented_foreign_key(tmp_path):
    workspace = project(tmp_path)
    book = openpyxl.Workbook()
    book.active.title = "经营指标"
    book.active.append(["指标编号", "中文名称"])
    book.active.append(["2150", "SECRET_CELL_LABEL"])
    sheet = book.create_sheet("组织")
    sheet.append(["组织编号", "上级编号"])
    sheet.append([1, 0])
    data = BytesIO()
    book.save(data)
    book.close()
    file = workspace.add_file("alpha", "items.xlsx", data.getvalue())
    result = preview_file_metadata(workspace, "alpha", file["file_id"])
    assert {table["name"] for table in result["tables"]} == {"经营指标", "组织"}
    assert "SECRET_CELL_LABEL" not in str(result)
    files = {f["name"]: yaml.safe_load(f["content"]) for f in result["files"]}
    assert files["foreign_keys.yaml"]["foreign_keys"] == []
    assert files["constraints.yaml"]["sources"][0]["constraint_coverage"]["primary_key"] == "not_declared"


def test_preview_requires_a_project_data_source(tmp_path):
    workspace = project(tmp_path)
    file = workspace.add_file("alpha", "notes.md", b"# notes")
    with pytest.raises(LightGraphError, match="file_metadata_source_missing"):
        preview_file_metadata(workspace, "alpha", file["file_id"])
    workspace.create("隔离项目", "beta")
    with pytest.raises(LightGraphError, match="project_file_not_found"):
        preview_file_metadata(workspace, "beta", file["file_id"])


def test_export_uses_only_current_cache_without_opening_sources(tmp_path, monkeypatch):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file("alpha", "items.csv", b"code,label\nA,CELL_VALUE\n")
    read_metadata(workspace, "alpha", refresh=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("export must not open source")
    monkeypatch.setattr("lightgraph.runtime.config.open_sources", forbidden)
    result = metadata_yaml(workspace, "alpha")
    assert not result["requires_refresh"]
    assert len(result["files"]) == 3
    assert "CELL_VALUE" not in str(result)
    config = workspace.get_config("alpha")
    config["sources"][0]["table_name"] = "changed"
    workspace.save_config("alpha", config)
    assert metadata_yaml(workspace, "alpha")["requires_refresh"]


def test_saved_catalog_survives_fresh_generation_and_server_reload(tmp_path, monkeypatch):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file('alpha', 'items.csv', b'code,label\nA,PRIVATE_CELL\n')
    initial = read_metadata(workspace, 'alpha', refresh=True)
    workspace.select_output_generation('alpha', 'run_' + '1' * 24, create=True)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',
                        lambda *a, **kw: pytest.fail('GET must not reopen a source'))
    restored = Workspace(workspace.root)
    view = read_metadata(restored, 'alpha')
    assert view['tables'] == initial['tables'] and view['using_saved_metadata']
    assert not view['requires_refresh'] and not view['refresh_failed']
    files = metadata_yaml(restored, 'alpha')['files']
    assert len(files) == 3 and 'PRIVATE_CELL' not in str(files)
    # A saved preview is not an alternative authorization source.
    config = restored.get_config('alpha')
    config['sources'][0]['table_name'] = 'changed'
    restored.save_config('alpha', config)
    assert read_metadata(restored, 'alpha')['tables'] == []
    assert metadata_yaml(restored, 'alpha')['files'] == []


@pytest.mark.parametrize('legacy', [False, True])
def test_failed_refresh_keeps_last_successful_preview_and_reports_staleness(tmp_path, monkeypatch, legacy):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file('alpha', 'items.csv', b'code,label\nA,PRIVATE_CELL\n')
    initial = read_metadata(workspace, 'alpha', refresh=True)
    if legacy:
        (workspace.project_path('alpha') / 'metadata/catalog-preview.yaml').unlink()
    def failed(*args, **kwargs):
        raise LightGraphError('database_connection_failed')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', failed)
    with pytest.raises(LightGraphError, match='database_connection_failed'):
        read_metadata(workspace, 'alpha', refresh=True)
    view = read_metadata(Workspace(workspace.root), 'alpha')
    assert view['tables'] == initial['tables']
    assert view['requires_refresh'] and view['using_saved_metadata'] and view['refresh_failed']
    files = metadata_yaml(workspace, 'alpha')
    assert files['refresh_failed'] and len(files['files']) == 3
    assert 'PRIVATE_CELL' not in (workspace.project_path('alpha') / 'metadata/catalog-preview.yaml').read_text()


def test_partial_refresh_after_fresh_retains_only_unchanged_saved_sources(tmp_path, monkeypatch):
    from lightgraph.runtime.config import open_sources
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file('alpha', 'first.csv', b'code,label\nA,FIRST_PRIVATE\n')
    workspace.add_file('alpha', 'second.csv', b'code,label\nB,SECOND_PRIVATE\n')
    original = read_metadata(workspace, 'alpha', refresh=True)
    original_ids = {table['source_id'] for table in original['tables']}
    workspace.select_output_generation('alpha', 'run_' + '2' * 24, create=True)
    workspace.add_file('alpha', 'new.csv', b'code,label\nC,NEW_PRIVATE\n')
    added_id = next(source['id'] for source in workspace.get_config('alpha')['sources'] if source['id'] not in original_ids)
    opened = []
    def selected_only(config, *args, **kwargs):
        opened.append([source.id for source in config.sources])
        return open_sources(config, *args, **kwargs)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', selected_only)
    refreshed = read_metadata(workspace, 'alpha', refresh=True, source_ids=[added_id])
    assert opened == [[added_id]]
    assert {table['source_id'] for table in refreshed['tables']} == original_ids | {added_id}
    assert not refreshed['requires_refresh']
    assert read_metadata(Workspace(workspace.root), 'alpha')['tables'] == refreshed['tables']
    # A credential or permission revision must not reuse the old declaration,
    # even while a different source is being refreshed successfully.
    config = workspace.get_config('alpha')
    changed_id = config['sources'][0]['id']
    config['sources'][0]['credential_revision'] = 'changed-revision'
    workspace.save_config('alpha', config)
    refreshed = read_metadata(workspace, 'alpha', refresh=True, source_ids=[added_id])
    assert refreshed['requires_refresh'] and refreshed['missing_sources'] == [changed_id]
    assert changed_id not in {table['source_id'] for table in refreshed['tables']}
    assert 'PRIVATE' not in str(metadata_yaml(Workspace(workspace.root), 'alpha'))


def test_cache_copy_write_failure_keeps_valid_get_and_yaml_readable(tmp_path, monkeypatch):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file('alpha', 'items.csv', b'code,label\nA,PRIVATE_CELL\n')
    initial = read_metadata(workspace, 'alpha', refresh=True)
    (workspace.project_path('alpha') / 'metadata/catalog-preview.yaml').unlink()
    def failed(*args, **kwargs):
        raise PermissionError('DO_NOT_EXPOSE_PRIVATE_PATH')
    monkeypatch.setattr('lightgraph.web.execution._save_metadata_preview', failed)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', lambda *a, **kw: pytest.fail('cached read must not connect'))
    view = read_metadata(workspace, 'alpha')
    assert view['tables'] == initial['tables'] and not view['requires_refresh']
    assert view['metadata_cache_warning'] == 'metadata_preview_persistence_failed'
    exported = metadata_yaml(workspace, 'alpha')
    assert len(exported['files']) == 3 and exported['metadata_cache_warning'] == view['metadata_cache_warning']
    assert 'DO_NOT_EXPOSE_PRIVATE_PATH' not in str(view) + str(exported)
    with pytest.raises(LightGraphError, match='metadata_preview_persistence_failed'):
        read_metadata(workspace, 'alpha', refresh=True)
    # A failed cache copy does not bypass changed source authorization.
    config = workspace.get_config('alpha')
    config['sources'][0]['credential_revision'] = 'new-authorization'
    workspace.save_config('alpha', config)
    assert read_metadata(workspace, 'alpha')['tables'] == []


def test_cache_copy_path_policy_failure_is_not_downgraded_to_warning(tmp_path, monkeypatch):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    workspace.add_file('alpha', 'items.csv', b'code,label\nA,PRIVATE_CELL\n')
    read_metadata(workspace, 'alpha', refresh=True)
    def forbidden(*args, **kwargs):
        raise LightGraphError('workspace_path_invalid')
    monkeypatch.setattr('lightgraph.web.execution._save_metadata_preview', forbidden)
    with pytest.raises(LightGraphError, match='workspace_path_invalid'):
        read_metadata(workspace, 'alpha')


def test_adapter_closes_if_catalog_preview_fails(tmp_path, monkeypatch):
    workspace = project(tmp_path)
    file = workspace.add_file("alpha", "items.csv", b"code,label\nA,CELL_VALUE\n")
    class Broken:
        closed = False
        def describe(self):
            raise LightGraphError("catalog_read_failed")
        def close(self):
            self.closed = True
    adapter = Broken()
    monkeypatch.setattr("lightgraph.runtime.config.open_sources", lambda *a, **kw: [adapter])
    with pytest.raises(LightGraphError, match="catalog_read_failed"):
        preview_file_metadata(workspace, "alpha", file["file_id"])
    assert adapter.closed


def test_uploaded_file_catalog_survives_reload_without_reopening_source(tmp_path, monkeypatch):
    from lightgraph.web.execution import read_metadata
    workspace = project(tmp_path)
    first = workspace.add_file("alpha", "items.csv", b"code,label\nA,FIRST_CELL\n")
    preview_file_metadata(workspace, "alpha", first["file_id"])
    second = workspace.add_file("alpha", "regions.csv", b"code,label\nB,SECOND_CELL\n")
    preview_file_metadata(workspace, "alpha", second["file_id"])
    monkeypatch.setattr("lightgraph.runtime.config.open_sources",
                        lambda *a, **kw: pytest.fail("cached GET reopened a source"))
    fresh_workspace = Workspace(workspace.root)
    metadata = read_metadata(fresh_workspace, "alpha")
    assert not metadata["requires_refresh"]
    assert {table["name"] for table in metadata["tables"]} == {"items", "regions"}
    assert "FIRST_CELL" not in str(metadata) and "SECOND_CELL" not in str(metadata)


def test_selected_metadata_yaml_is_scoped_and_never_opens_sources(tmp_path, monkeypatch):
    from lightgraph.tests.web_client import RevisionClient as TestClient
    from lightgraph.web.app import create_app

    workspace = project(tmp_path)
    for name in ('first', 'second'):
        file = workspace.add_file('alpha', f'{name}.csv', b'code,label\nA,PRIVATE_SAMPLE\n')
        preview_file_metadata(workspace, 'alpha', file['file_id'])
    source = workspace.get_config('alpha')['sources'][0]['id']
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',
                        lambda *a, **kw: pytest.fail('selection must not reconnect'))
    with TestClient(create_app(workspace.root, frontend_dir=tmp_path / 'absent')) as client:
        result = client.get('/api/projects/alpha/metadata/yaml',
                            params={'source_id': source, 'schema': 'public', 'table': 'first'})
        assert result.status_code == 200
        files = {item['name']: yaml.safe_load(item['content']) for item in result.json()['files']}
        assert [(t['source_id'], t['name']) for t in files['tables.yaml']['tables']] == [(source, 'first')]
        assert len(files['constraints.yaml']['tables']) == 1
        assert {s['source_id'] for s in files['constraints.yaml']['sources']} == {source}
        assert 'PRIVATE_SAMPLE' not in result.text
        # A mismatched source/table pair cannot silently fall back to all tables.
        mismatch = client.get('/api/projects/alpha/metadata/yaml',
                              params={'source_id': source, 'table': 'second'})
        assert mismatch.json()['files'] == []

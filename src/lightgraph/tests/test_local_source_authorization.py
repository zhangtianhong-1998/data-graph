from pathlib import Path

import openpyxl
import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.workflow import build_unit_config
from lightgraph.construction.workspace import write_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime import config as runtime_config
from lightgraph.runtime.config import RunConfig, SourceConfig, WorkflowUnitConfig, open_sources
from lightgraph.sources import CSVSource, ExcelSource
from lightgraph.tests.test_sources_csv import FORMAT, rows
from lightgraph.web.construction_graph import load_prepared
from lightgraph.web.execution import read_metadata
from lightgraph.web.workspace import Workspace


def workbook(tmp_path):
    path = tmp_path / 'source.xlsx'
    book = openpyxl.Workbook()
    selected = book.active
    selected.title = 'selected'
    selected.append(['code', 'label', 'excluded'])
    selected.append(['001', '收入', '=1+1'])
    selected.append([None, None, 'excluded value'])
    other = book.create_sheet('other')
    other.append(['code', 'label'])
    other.append(['002', '另一个工作表'])
    book.save(path)
    book.close()
    return path


@pytest.mark.parametrize('metadata_only', [False, True])
def test_workflow_excel_unit_imports_only_selected_sheet_and_columns(tmp_path, metadata_only):
    path = workbook(tmp_path)
    config = RunConfig(sources=[{
        'id': 'local', 'kind': 'excel_workbook', 'path': str(path),
        'allowed_columns': {'public.selected': ['code', 'label'], 'public.other': ['code']},
    }])
    unit = WorkflowUnitConfig(unit_id='selected',
        source_table_overrides={'local': ['public.selected']})
    scoped = build_unit_config(config, unit, unit_output_dir=tmp_path / 'unit')
    sources = open_sources(scoped, metadata_only=metadata_only)
    try:
        source = sources[0]
        assert [table.qualified_name for table in source.describe()] == ['public.selected']
        assert source.describe()[0].column_names == ['code', 'label']
        assert source.issue_counts == {}
        with pytest.raises(LightGraphError, match='^unauthorized_object$'):
            source.compiler.guard.validate('SELECT code FROM public.other')
        with pytest.raises(LightGraphError, match='^unauthorized_column$'):
            source.compiler.guard.validate('SELECT excluded FROM public.selected')
        if metadata_only:
            with pytest.raises(LightGraphError, match='^metadata_only_query_forbidden$'):
                rows(source)
            assert source._input_bytes == 0
        else:
            assert rows(source) == [('001', '收入'), (None, None)]
    finally:
        for source in sources:
            source.close()
    assert config.sources[0].allowed_tables == []
    with ExcelSource(path) as original:
        assert len(original.describe()) == 2


@pytest.mark.parametrize('metadata_only', [False, True])
def test_excel_unselected_sheet_is_skipped_before_cell_reading(tmp_path, monkeypatch, metadata_only):
    path = workbook(tmp_path)
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet
    original = ReadOnlyWorksheet.iter_rows
    def selected_only(sheet, **kwargs):
        assert sheet.title == 'selected'
        return original(sheet, **kwargs)
    monkeypatch.setattr(ReadOnlyWorksheet, 'iter_rows', selected_only)
    with ExcelSource(path, allowed_tables=['public.selected'],
                     allowed_columns={'public.selected': ['code']}, metadata_only=metadata_only) as source:
        assert source.describe()[0].column_names == ['code']


@pytest.mark.parametrize('excluded_value,expected', [('=1+1', []), ('', [(None,)])])
def test_excel_column_selection_preserves_full_import_row_set(tmp_path, excluded_value, expected):
    path = tmp_path / 'row-presence.xlsx'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['code', 'excluded'])
    sheet.append([None, excluded_value])
    book.save(path)
    book.close()

    for selection in (None, {'public.Sheet': ['code']}):
        with ExcelSource(path, allowed_columns=selection) as source:
            values = [row for batch in source.project(source.describe()[0], ['code'], distinct=False)
                      for row in batch]
            assert values == expected
            if selection:
                assert source.issue_counts == {}


@pytest.mark.parametrize('metadata_only', [False, True])
def test_csv_open_sources_respects_qualified_table_and_column_scope(tmp_path, metadata_only):
    path = tmp_path / 'source.csv'
    path.write_text('code,label,excluded\n001,收入,private\nNULL,NULL,private\n', encoding='utf-8')
    config = RunConfig(sources=[{
        'id': 'local', 'kind': 'csv_file', 'path': str(path), **FORMAT,
        'schema_name': 'uploads', 'table_name': 'logical',
        'allowed_tables': ['uploads.logical'], 'allowed_columns': {'uploads.logical': ['label', 'code']},
    }])
    sources = open_sources(config, metadata_only=metadata_only)
    try:
        source = sources[0]
        assert source.describe()[0].qualified_name == 'uploads.logical'
        assert source.describe()[0].column_names == ['code', 'label']
        with pytest.raises(LightGraphError, match='^unauthorized_column$'):
            source.compiler.guard.validate('SELECT excluded FROM uploads.logical')
        if metadata_only:
            assert source._input_bytes == 0
            with pytest.raises(LightGraphError, match='^metadata_only_query_forbidden$'):
                rows(source)
        else:
            assert rows(source) == [('001', '收入'), (None, None)]
    finally:
        for source in sources:
            source.close()


@pytest.mark.parametrize('kind', ['excel', 'csv'])
@pytest.mark.parametrize('metadata_only', [False, True])
@pytest.mark.parametrize('selection,code', [
    ({'allowed_tables': ['public.missing']}, 'authorized_table_unavailable_or_view'),
    ({'allowed_columns': {'public.missing': ['code']}}, 'unauthorized_column_scope'),
    ({'allowed_columns': {'public.selected': ['missing']}}, 'authorized_column_unavailable'),
    ({'allowed_columns': {'public.selected': []}}, 'authorized_column_unavailable'),
])
def test_local_source_rejects_missing_or_empty_authorization(tmp_path, kind, metadata_only, selection, code):
    if kind == 'excel':
        path = workbook(tmp_path)
        factory, options = ExcelSource, {}
    else:
        path = tmp_path / 'selected.csv'
        path.write_text('code,label\n001,收入\n', encoding='utf-8')
        factory, options = CSVSource, FORMAT
    with pytest.raises(LightGraphError, match='^' + code + '$'):
        factory(path, **options, **selection, metadata_only=metadata_only)


@pytest.mark.parametrize('selection,legacy,changed', [
    ({}, 'cf3abc5590cd63317a9ba402d411556edc1410bdbe8a57199ea67829c5d96e64', False),
    ({'allowed_tables': ['public.items']},
     '82b016e9ba615efb0398c507cf0b0adc407358cf894eaa63f029e4a584145787', True),
    ({'allowed_columns': {'public.items': ['code']}},
     'fe37607b687d6d9d101c28a3b19ff257a26e17d9ffe5bb0120fd7074580acd47', True),
])
def test_file_scope_fingerprint_migrates_only_selected_sources(selection, legacy, changed):
    config = RunConfig(sources=[{
        'id': 'local', 'kind': 'excel_workbook', 'path': 'stable.xlsx', **selection,
    }])
    assert (access_fingerprint(config) != legacy) is changed


@pytest.mark.parametrize('kind', ['excel', 'csv'])
def test_legacy_file_publication_and_metadata_require_refresh_after_scope_enforcement(tmp_path, monkeypatch, kind):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('File permissions', 'alpha')
    if kind == 'excel':
        workspace.add_file('alpha', 'source.xlsx', workbook(tmp_path).read_bytes())
    else:
        workspace.add_file('alpha', 'selected.csv', b'code,label,excluded\n001,label,private\n')
    config = workspace.get_config('alpha')
    config['sources'][0].update(
        allowed_tables=['public.selected'], allowed_columns={'public.selected': ['code']})
    workspace.save_config('alpha', config)
    resolved = workspace.resolved_config('alpha')

    def legacy_open(config, **kwargs):
        unscoped = config.model_copy(deep=True)
        for source in unscoped.sources:
            source.allowed_tables = []
            source.allowed_columns = None
        return open_sources(unscoped, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(SourceConfig, 'file_scope_revision', property(lambda self: {}))
        legacy.setattr(runtime_config, 'open_sources', legacy_open)
        old_access = access_fingerprint(resolved)
        cached = read_metadata(workspace, 'alpha', refresh=True)
        assert any(len(table['columns']) > 1 for table in cached['tables'])

    output = Path(resolved.output_dir)
    write_yaml(output / 'construction' / 'manifest.yaml',
               {'current_graph': {'access_fingerprint': old_access}})
    with pytest.raises(LightGraphError, match='^graph_access_context_changed$'):
        load_prepared(resolved)
    cached = read_metadata(workspace, 'alpha')
    assert cached['requires_refresh'] and cached['tables'] == []
    refreshed = read_metadata(workspace, 'alpha', refresh=True)
    assert not refreshed['requires_refresh']
    assert [table['name'] for table in refreshed['tables']] == ['selected']
    assert [column['name'] for column in refreshed['tables'][0]['columns']] == ['code']

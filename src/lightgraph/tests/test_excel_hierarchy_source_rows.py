"""Worksheet membership is physical provenance, independently of a mapping's fact grain."""
import hashlib

import openpyxl
import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.projection_reuse import ImmutableProjections
from lightgraph.contracts import ExtractionGoal
from lightgraph.errors import LightGraphError
from lightgraph.sources.excel import ExcelSource
from lightgraph.sources.scoped import GoalScopedSource


def workbook(path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'tree'
    sheet.append(['title'])
    sheet.append(['parent', 'child', 'project', 'region', 'rowid', 'private'])
    sheet.append(['A', 'B', 'M', 'north', 'not a row number', 200])
    sheet.append(['A', 'B', 'M', 'hidden', 'do not use', 201])
    sheet.row_dimensions[4].hidden = True
    sheet.append(['A', 'B', 'M', 'south', 'shadow', 202])
    sheet.append(['A', 'B', 'K', 'north', 'shadow', 203])
    sheet.append(['A', 'B', 'M', 'north', 'shadow', 204])
    sheet.append(['A', 'B', 'M', 'north', 'shadow', 205])
    book.save(path)
    book.close()


def hierarchy(context=(), predicate=None):
    base = {'source_id': 'excel', 'table': 'tree'}
    relation = {**base, 'source_key_columns': ['parent'], 'target_key_columns': ['child'],
                'context_columns': list(context), 'predicate': predicate}
    return {'draft_id': 'h', 'entities': [{'type': 'Unit', 'key_names': ['code'], 'mappings': [
        {**base, 'key_columns': [col]} for col in ['parent', 'child']]}], 'relations': [
        {'type': 'contains', 'source_type': 'Unit', 'target_type': 'Unit',
         'hierarchy_orientation': 'parent_to_child', 'mappings': [relation]}]}


def extract(source, draft, **options):
    result = execute_draft(draft, [source], source.describe(), **options)
    assert result['passed'], result['diagnostics']
    edge, = result['graph']['edges']
    return edge['supports']


def test_membership_survives_projection_cache_and_keeps_scope_grain(tmp_path):
    path = tmp_path / 'test.xlsx'
    workbook(path)
    expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    columns = {'public.tree': ['parent', 'child', 'project', 'rowid']}
    with ExcelSource(path, header_row=2, allowed_columns=columns) as source:
        view = GoalScopedSource(source, ExtractionGoal(excluded_columns=['rowid']))
        cache = ImmutableProjections({'excel': expected_sha}, 65536)
        for _ in range(2):
            supports = extract(view, hierarchy(['project']), enable_co_projection=True, immutable_projections=cache)
            rows = {s['row_context']['values']['project']: s['source_row_witness']['rows'] for s in supports}
            assert rows == {'M': [[3, 3], [5, 5], [7, 8]], 'K': [[6, 6]]}
            for support in supports:
                witness = support['source_row_witness']
                assert witness['snapshot'] == expected_sha and witness['kind'] == 'excel_rows_v1'
                assert set(support['provenance']['columns']) == {'parent', 'child', 'project'}
                assert set(support['row_context']['values']) == {'project'}
        with pytest.raises(LightGraphError, match='unauthorized_column'):
            with view.sheet_row_projection(source.describe()[0], ['rowid'], predicate=None, limit=10, witness_bytes=100):
                pass
        with pytest.raises(LightGraphError, match='unauthorized_column'):
            with source.sheet_row_projection(source.describe()[0], ['private'], predicate=None, limit=10, witness_bytes=100):
                pass

    # A different unit can legitimately see different columns in the same sheet.
    # Its finer projection/predicate is tied to just the rows that satisfied it.
    other = {'public.tree': ['parent', 'child', 'region']}
    with ExcelSource(path, header_row=2, allowed_columns=other) as source:
        support, = extract(source, hierarchy(predicate={'column':'region','op':'eq','value':'north'}))
        assert support['source_row_witness'] == {
            'kind':'excel_rows_v1', 'snapshot':expected_sha, 'rows':[[3,3],[6,8]]}
        assert 'row_context' not in support


def test_changed_workbook_and_shadowed_sqlite_locators(tmp_path):
    path = tmp_path / 'test.xlsx'
    workbook(path)
    with ExcelSource(path, header_row=2) as source:
        orig, = extract(source, hierarchy())
    edited = openpyxl.load_workbook(path)
    edited.active.cell(row=6, column=2, value='C')
    edited.save(path)
    edited.close()
    with ExcelSource(path, header_row=2) as source:
        after = extract(source, hierarchy(predicate={'column':'child','op':'eq','value':'B'}))[0]
        assert after['source_row_witness']['snapshot'] != orig['source_row_witness']['snapshot']
    edited = openpyxl.load_workbook(path)
    edited.active.cell(row=2, column=4, value='_rowid_')
    edited.active.cell(row=2, column=6, value='OID')
    edited.save(path)
    edited.close()
    with ExcelSource(path, header_row=2) as source:
        support, = extract(source, hierarchy(predicate={'column':'child','op':'eq','value':'B'}))
        assert 'source_row_witness' not in support


@pytest.mark.parametrize('real_first', [True, False])
def test_optional_observer_preserves_sqlite_distinct_representative_and_limits(tmp_path, real_first):
    import sqlite3
    from zipfile import ZipFile
    from lightgraph.contracts import QueryLimits
    from lightgraph.source_values import scalar_kind

    path = tmp_path / 'source.xlsx'
    raw_path = tmp_path / 'raw.xlsx'
    book = openpyxl.Workbook()
    book.active.append(['number'])
    for value in [1, 1, 2, 3, 2, 3]:
        book.active.append([value])
    book.save(raw_path)
    book.close()
    # The normal workbook writer serializes 1.0 and 1 identically. Preserve
    # distinct Excel numeric representations in the actual input XML.
    coord = 'A2' if real_first else 'A3'
    old = f'<c r="{coord}" t="n"><v>1</v></c>'.encode()
    with ZipFile(raw_path) as original, ZipFile(path, 'w') as out:
        for entry in original.infolist():
            content = original.read(entry)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                assert content.count(old) == 1
                content = content.replace(old, old.replace(b'>1<', b'>1.0<'))
            out.writestr(entry, content)

    # A full row locator cannot fit in the SQL batch. Only source values
    # consume that return budget; locators have their own working-set allowance.
    with ExcelSource(path, limits=QueryLimits(max_batch_bytes=16)) as source:
        table, = source.describe()
        def read(stream):
            return [(scalar_kind(row[0]), row[0]) for batch in stream for row in batch]
        start = source.stats.returned_bytes
        plain = read(source.project(table, ['number']))
        ordinary_bytes = source.stats.returned_bytes - start
        start = source.stats.returned_bytes
        with source.sheet_row_projection(table, ['number'], predicate=None, limit=10,
                                         witness_bytes=4096) as (stream, witness):
            assert read(stream) == plain
            assert witness({'number': 1}) is None
            assert witness({'number': 2})['rows'] == [[4, 4], [6, 6]]
        assert source.stats.returned_bytes - start == ordinary_bytes
        assert plain[0][0] == ('real' if real_first else 'integer')
        with source.sheet_row_projection(table, ['number'], predicate=None, limit=10,
                                         witness_bytes=1) as (stream, witness):
            assert read(stream) == plain and witness({'number': 2}) is None
        source._db.setlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG, 1)
        with source.sheet_row_projection(table, ['number'], predicate=None, limit=10,
                                         witness_bytes=4096) as (stream, witness):
            assert read(stream) == plain and witness({'number': 2}) is None


def test_graph_budget_prioritizes_the_complete_preexisting_graph(tmp_path, monkeypatch):
    path = tmp_path / 'source.xlsx'
    workbook(path)
    with ExcelSource(path, header_row=2) as source:
        draft = hierarchy(['region'])
        with monkeypatch.context() as no_locator:
            no_locator.setattr(source, 'sheet_row_snapshot', lambda table: None)
            baseline = execute_draft(draft, [source], source.describe())
        assert baseline['passed']
        cap = baseline['metrics']['working_memory_estimated_peak_bytes']
        actual = execute_draft(draft, [source], source.describe(), max_working_bytes=cap)
        assert actual['passed'] and actual['graph'] == baseline['graph']


def test_tight_excel_import_budget_discards_all_positions_and_keeps_rows(tmp_path):
    path = tmp_path / 'large.xlsx'
    book = openpyxl.Workbook()
    sheets = [book.active, book.create_sheet('second')]
    for sheet in sheets:
        sheet.append(['parent', 'child'])
        for i in range(400):
            sheet.append(['head', f'item{i}'])
    book.save(path)
    book.close()
    with ExcelSource(path) as source:
        import_bytes = source._input_bytes
        assert all(source.sheet_row_snapshot(t) for t in source.describe())
    with ExcelSource(path, max_memory_bytes=import_bytes) as source:
        assert source._input_bytes == import_bytes
        for table in source.describe():
            assert source.sheet_row_snapshot(table) is None
            assert sum(len(batch) for batch in source.project(table, ['child'])) == 400

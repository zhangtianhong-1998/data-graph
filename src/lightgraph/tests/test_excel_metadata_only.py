"""Header-only discovery neither imports Excel facts nor infers their types."""
import openpyxl
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources import ExcelSource
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.models import COLUMN_IDENTITY_NAMES
from lightgraph.construction.workspace import dump_yaml
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint


def workbook(tmp_path):
    path = tmp_path / "metadata.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "items"
    sheet.append(["id", "label", "hidden"])
    sheet.column_dimensions["C"].hidden = True
    sheet.append([1, "one", "private-value"])
    sheet.append([2, "=1+1", "other-private-value"])
    sheet.append([3, "three", "another-private-value"])
    book.save(path)
    book.close()
    return path


def test_metadata_only_skips_instance_import_and_inference(tmp_path, monkeypatch):
    path = workbook(tmp_path)
    def forbidden_inference(_self):
        pytest.fail("metadata-only must not infer types from fact rows")
    monkeypatch.setattr(ExcelSource, "_infer_loaded_types", forbidden_inference)
    with ExcelSource(path, metadata_only=True, max_rows=1) as source:
        assert source._input_bytes == 0
        assert source.stats.queries == 0
        table = source.describe()[0]
        assert table.column_names == ["id", "label"]
        assert {column.data_type for column in table.columns} == {"unknown"}
        assert source._pending_formula_columns == {}
        assert source.issues == []
        with pytest.raises(LightGraphError, match="metadata_only_query_forbidden"):
            list(source.project(table, ["id"]))
        assert source.stats.queries == 0


def test_full_excel_path_still_loads_types_and_formula_issues(tmp_path):
    path = workbook(tmp_path)
    with ExcelSource(path) as source:
        assert source._input_bytes > 0
        assert source.describe()[0].columns[0].data_type == "integer"
        assert source.issue_counts == {"formula_cache_missing": 1}
        table = source.describe()[0]
        rows = [row for batch in source.project(table, ["id"]) for row in batch.rows()]
        assert rows == [(1,), (2,), (3,)]


def test_open_sources_passes_metadata_only_to_excel(tmp_path):
    path = workbook(tmp_path)
    config = RunConfig(sources=[{"id": "book", "kind": "excel_workbook", "path": str(path)}])
    sources = open_sources(config, metadata_only=True)
    try:
        assert sources[0].metadata_only is True
        assert sources[0]._input_bytes == 0
        assert sources[0].describe()[0].source_id == "book"
    finally:
        for source in sources:
            source.close()


def test_metadata_only_preserves_explicit_hidden_column_choice(tmp_path):
    with ExcelSource(workbook(tmp_path), metadata_only=True, include_hidden=True) as source:
        assert source.describe()[0].column_names == ["id", "label", "hidden"]


def test_projection_records_original_excel_ordinals_after_hidden_and_unselected_columns(tmp_path):
    path = tmp_path / 'positions.xlsx'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'items'
    sheet.append(['private', 'code', 'unselected', 'caption'])
    sheet.append(['x', '001', 'y', 'One'])
    sheet.column_dimensions['A'].hidden = True
    book.save(path)
    book.close()
    with ExcelSource(path, source_id='book', allowed_columns={'public.items': ['code', 'caption']}) as source:
        tables = source.describe()
        assert tables[0].column_names == ['code', 'caption']
        draft = {'draft_id': 'items', 'entities': [{'type': 'Item', 'key_names': ['id'], 'mappings': [
            {'source_id': 'book', 'table': 'items', 'key_columns': ['code'], 'label_column': 'caption'}]}]}
        result = execute_draft(draft, [source], tables)
        assert result['passed'], result['diagnostics']
        node, = result['graph']['nodes']
        support, = node['supports']
        assert support['provenance']['source_column_ordinals'] == {'code': 2, 'caption': 4}


def test_excel_metadata_probe_and_validation_retain_only_observed_authorized_positions(tmp_path):
    path = tmp_path / 'positions.xlsx'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'items'
    sheet.append(['heading'])
    sheet.append(['hidden', 'code', 'secret', 'caption', 'outside', 'excluded'])
    sheet.append(['private', 'object-1', 'private', 'name-1', 'private', 'private'])
    sheet.column_dimensions['A'].hidden = True
    book.save(path)
    book.close()
    config = RunConfig(sources=[{'id': 'book', 'kind': 'excel_workbook', 'path': str(path),
        'header_row': 2, 'allowed_tables': ['public.items'],
        'allowed_columns': {'public.items': ['caption', 'code', 'excluded', 'secret']}}],
        goal={'excluded_columns': ['excluded']}, model={'mode': 'in_environment'})
    expected = {'code': 2, 'caption': 4}
    output = tmp_path / 'work'
    tools = ConstructionTools(config, output / 'construction')
    try:
        table, = tools.call('describe_sources')['tables']
        assert table['source_column_ordinals'] == expected
        probe = tools.call('inspect_source', table=table['table'], operation='get_metadata')
        assert probe['data']['tables'][0]['source_column_ordinals'] == expected
        evidence = tools.workspace.read_evidence(probe['evidence_ref'])
        assert evidence.details['source_column_ordinals'] == expected
        assert 'object-1' not in str(evidence) and 'name-1' not in str(evidence)
        source = {'source_id': 'book', 'table': 'items'}
        raw = {'draft_id': 'items', 'entities': [
            {'type': 'Item', 'key_names': ['id'], 'mappings': [
                {**source, 'key_columns': ['code'], 'label_column': 'caption'}]},
            {'type': 'Field', 'key_names': COLUMN_IDENTITY_NAMES, 'mappings': [
                {**source, 'kind': 'metadata_column', 'column': 'code', 'evidence_refs': [probe['evidence_ref']]}]}]}
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(raw))['status'] == 'ok'
        structure = tools.call('validate_graph_draft', draft_id='items', mode='structure')
        checked = tools.workspace._read_record(structure['structure_artifact'])
        assert [b['source_column_ordinals'] for b in checked['bindings']] == [expected, {'code': 2}]
        assert tools.call('validate_graph_draft', draft_id='items')['passed']
        assert tools.call('publish_graph_draft', draft_id='items')['nodes'] == 2
        for node in tools.workspace.read_published_graph()['nodes']:
            origin = node['supports'][0]['provenance']
            assert origin['source_column_ordinals'] == (expected if node['type'] == 'Item' else {'code': 2})
        contributor = load_unit_contributor(output, 'extract')
        record = commit_checkpoint(output, 'run_' + 'a' * 24, 'extract', contributor, input_digest='test')
        restored = load_checkpoint(output, record)
        assert restored['evidence_records'][evidence.ref]['details']['source_column_ordinals'] == expected
    finally:
        tools.close()


def test_excel_identity_distinguishes_timestamps_text_and_cell_errors(tmp_path):
    from datetime import datetime

    stamp = datetime(2026, 1, 2, 3, 4, 5)
    path = tmp_path / "types.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "items"
    sheet.append(["key", "invalid"])
    sheet.append([stamp, "#N/A"])
    sheet.append([stamp.isoformat(), "usable"])
    sheet.append(["#N/A", "usable"])
    sheet['A4'].data_type = 's'
    book.save(path)
    book.close()

    def draft(column):
        return {"draft_id": "typed", "entities": [{"type": "Item", "key_names": ["code"],
                "mappings": [{"source_id": "sheet", "table": "items", "key_columns": [column]}]}]}

    config = RunConfig(sources=[{"id": "sheet", "kind": "excel_workbook", "path": str(path)}],
                       model={"mode": "in_environment"})
    tools = ConstructionTools(config, tmp_path / "out")
    try:
        assert tools.call("save_graph_draft", yaml_text=dump_yaml(draft("key")))["status"] == "ok"
        assert tools.call("validate_graph_draft", draft_id="typed")["passed"]
        assert tools.call("publish_graph_draft", draft_id="typed")["nodes"] == 3
        nodes = tools.workspace.read_published_graph()["nodes"]
        assert {(n["identity"]["code"], n["identity_types"]["code"]) for n in nodes} == {
            (stamp.isoformat(), "datetime"), (stamp.isoformat(), "text"), ("#N/A", "text")}
        assert tools.call("save_graph_draft", yaml_text=dump_yaml(draft("invalid")))["status"] == "ok"
        checked = tools.call("validate_graph_draft", draft_id="typed")
        assert not checked["passed"]
        assert "excel_error_cell_in_projection" in {d["code"] for d in checked["diagnostics"]}
    finally:
        tools.close()


def test_excel_temporal_predicates_and_authorized_sql_share_iso_literal_semantics(tmp_path):
    from datetime import date, datetime, time

    path = tmp_path / 'comparison.xlsx'
    workbook = openpyxl.Workbook()
    workbook.iso_dates = True
    sheet = workbook.active
    sheet.title = 'items'
    sheet.append(['id', 'day', 'at_time', 'stamp', 'label', 'mixed'])
    stamp = datetime(2026, 1, 2, 1, 3, 5)
    sheet.append(['early', date(2026, 1, 2), time(1, 3, 5), stamp, stamp.isoformat(), stamp])
    sheet.append(['late', date(2026, 1, 3), time(1, 3, 6), datetime(2026, 1, 3), 'x', stamp.isoformat()])
    workbook.save(path)
    workbook.close()

    with ExcelSource(path) as adapter:
        table, = adapter.describe()

        def sql(statement, **parameters):
            validated = adapter.compiler.guard.validate(statement, parameters)
            return [row[0] for batch in adapter.execute(validated) for row in batch.rows()]

        for column, val in [('day', '2026-01-02'), ('at_time', '01:03:05'), ('stamp', stamp.isoformat())]:
            for op in ('eq', 'le'):
                assert [row[0] for batch in adapter.project(table, ['id'], {'column': column, 'op': op, 'value': val})
                        for row in batch.rows()] == ['early']
            assert sql(f"SELECT id FROM public.items WHERE {column} = :p", p=val) == ['early']
        assert sql("SELECT id FROM public.items WHERE stamp = :v AND label = :v", v=stamp.isoformat()) == ['early']
        assert sql("SELECT id FROM public.items WHERE '2026-01-02' >= day") == ['early']
        assert sql("SELECT id FROM public.items WHERE at_time BETWEEN '01:03:05' AND '01:03:05.0'") == ['early']
        assert sql("WITH a(k,v) AS (SELECT id,stamp FROM public.items) SELECT k FROM a WHERE v IN (:s)", s=stamp.isoformat()) == ['early']
        assert sql("SELECT id FROM public.items WHERE mixed = :v", v=stamp.isoformat()) == ['late']
        assert sql("SELECT id FROM public.items WHERE :v BETWEEN day AND day", v='2026-01-02') == ['early']
        assert sql("SELECT id FROM public.items WHERE :v IN (SELECT day FROM public.items)", v='2026-01-02') == ['early', 'late']
        with pytest.raises(LightGraphError, match='excel_temporal_comparison_unsupported'):
            sql("SELECT id FROM public.items WHERE stamp LIKE :v", v='2026%')

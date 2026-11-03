"""Header-only discovery neither imports Excel facts nor infers their types."""
import openpyxl
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources import ExcelSource


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

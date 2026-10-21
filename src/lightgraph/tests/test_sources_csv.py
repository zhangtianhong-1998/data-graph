from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, SourceConfig, open_sources
from lightgraph.sources import CSVSource, MemorySource


FORMAT = dict(encoding="utf-8", delimiter=",", header=True,
              null_values=["NULL"], type_inference="text")


def rows(source, columns=None):
    table = source.describe()[0]
    return [row for batch in source.project(table, columns or table.column_names)
            for row in batch.rows()]


def test_csv_is_text_exact_and_memory_only(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text('code,name,extra\n001,"Hello, world",NULL\n0002,"line\nbreak",\n 03 ,中文,001\n')
    before = set(tmp_path.iterdir())
    with CSVSource(path, source_id="csv_codes", **FORMAT) as source:
        assert rows(source) == [
            ("001", "Hello, world", None), ("0002", "line\nbreak", ""),
            (" 03 ", "中文", "001"),
        ]
        table = source.describe()[0]
        assert (table.source_id, table.schema_name, table.name) == ("csv_codes", "public", "codes")
        assert table.foreign_keys == [] and table.unique_keys == []
        assert all(column.data_type == "text" for column in table.columns)
        source._db.set_authorizer(None)
        try:
            assert source._db.execute("PRAGMA temp_store").fetchone()[0] == 2
            assert all(not entry[2] for entry in source._db.execute("PRAGMA database_list"))
        finally:
            source._db.set_authorizer(source._authorize)
        assert source.format_metadata["input_revision"]
        assert str(path) not in json.dumps(source.format_metadata)
    assert set(tmp_path.iterdir()) == before


def test_explicit_utf16_semicolon_no_header(tmp_path):
    path = tmp_path / "sheet.csv"
    path.write_text("001;说明\n002;NULL\n", encoding="utf-16")
    options = {**FORMAT, "encoding": "utf-16", "delimiter": ";", "header": False}
    with CSVSource(path, source_id="s", columns=["code", "description"],
                   table_name="logical", schema_name="uploads", **options) as source:
        assert rows(source) == [("001", "说明"), ("002", None)]
        assert source.describe()[0].qualified_name == "uploads.logical"


@pytest.mark.parametrize("content", [
    "a,b\n1\n", "a,b\n1,2,3\n", "a,a\n1,2\n", ",b\n1,2\n",
    'a,b\n1,"unterminated\n', 'a,b\n1,"bad"tail\n', "a,b\n\n",
])
def test_csv_bad_format_never_repaired(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content)
    with pytest.raises(LightGraphError) as error:
        CSVSource(path, **FORMAT)
    assert "unterminated" not in str(error.value) and "tail" not in str(error.value)
    assert list(tmp_path.iterdir()) == [path]


def test_csv_wrong_encoding_does_not_fallback(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes("code,name\n001,中文\n".encode("utf-16"))
    with pytest.raises(LightGraphError):
        CSVSource(path, **FORMAT)


@pytest.mark.parametrize("options", [
    {"max_file_bytes": 5}, {"max_rows": 1}, {"max_columns": 1},
    {"max_memory_bytes": 16},
])
def test_csv_explicit_budgets(tmp_path, options):
    path = tmp_path / "codes.csv"
    path.write_text("code,label\n001,A\n002,B\n")
    with pytest.raises(LightGraphError):
        CSVSource(path, **FORMAT, **options)
    assert list(tmp_path.iterdir()) == [path]


def test_csv_metadata_only_reads_header_without_instance_queries(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text("code,label\nmalformed,row,not,parsed\n")
    with CSVSource(path, **FORMAT, metadata_only=True) as source:
        assert source.stats.queries == 0
        assert source.describe()[0].column_names == ["code", "label"]
        assert "input_revision" not in source.format_metadata
        with pytest.raises(LightGraphError, match="metadata_only_query_forbidden"):
            rows(source)
        assert source.stats.queries == 0


def test_csv_config_requires_explicit_format_and_unique_source_id():
    for missing in FORMAT:
        options = {key: value for key, value in FORMAT.items() if key != missing}
        with pytest.raises(ValidationError):
            SourceConfig(id="s", kind="csv_file", path="a.csv", **options)
    with pytest.raises(ValidationError):
        RunConfig(sources=[
            SourceConfig(id="s", kind="csv_file", path="a.csv", **FORMAT),
            SourceConfig(id="s", kind="excel_workbook", path="b.xlsx"),
        ])


def test_mixed_csv_excel_and_database_substitute(tmp_path, monkeypatch):
    import lightgraph.sources
    csv_path = tmp_path / "codes.csv"
    csv_path.write_text("code\n001\n")
    excel_path = tmp_path / "codes.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "codes"
    workbook.active.append(["code"])
    workbook.active.append(["001"])
    workbook.save(excel_path)

    def database(_dsn, *, source_id, limits, query_budget, **_kwargs):
        table = TableSpec(source_id=source_id, name="codes", columns=[ColumnSpec(name="code")])
        return MemorySource([table], {"public.codes": [("001",)]},
                            limits=limits, query_budget=query_budget)

    monkeypatch.setattr(lightgraph.sources, "GaussSource", database)
    monkeypatch.setenv("TEST_CSV_DSN", "synthetic-only")
    config = RunConfig(deployment_retention_verified=True, sources=[
        SourceConfig(id="csv", kind="csv_file", path=str(csv_path), **FORMAT),
        SourceConfig(id="excel", kind="excel_workbook", path=str(excel_path)),
        SourceConfig(id="db", kind="gauss_database", dsn_env="TEST_CSV_DSN",
                     allowed_tables=["public.codes"]),
    ])
    sources = open_sources(config)
    try:
        assert {source.describe()[0].source_id for source in sources} == {"csv", "excel", "db"}
        assert all(rows(source) == [("001",)] for source in sources)
        assert len({id(source.query_budget) for source in sources}) == 1
    finally:
        for source in sources:
            source.close()

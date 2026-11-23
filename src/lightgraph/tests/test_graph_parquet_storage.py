"""Parquet snapshots retain exact graph rows and never publish partial writes."""
from copy import deepcopy

import pyarrow.parquet as pq
import pytest

from lightgraph.construction import graph_storage
from lightgraph.construction.graph_storage import read_graph_record, write_graph_record
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


def graph():
    return {
        "format_version": 1, "target": "graph", "access_fingerprint": "authorized",
        "definitions": {"entities": [{"type": "项目", "key_names": ["编号"]}], "relations": []},
        "nodes": [
            {"id": "n2", "type": "项目", "label": "中文 / 0012", "identity": {"编号": "0012"},
             "properties": {"nullable": [None], "enabled": [True], "large": [2**80],
                            "amount": [-0.0, 1.5], "nested": [{"text": "\n☃"}]},
             "supports": [{"scope_ref": "scope", "label": "中文 / 0012"}],
             "provenance": [{"source_id": "s", "table": "t"}]},
            {"id": "n1", "type": "项目", "label": None, "properties": {}},
            {"id": "n3", "type": "项目"},
        ],
        "edges": [{"id": "e1", "type": "属于", "source": "n2", "target": "n1",
                   "properties": {}, "supports": []}],
        "evidence_catalog": {"e1": {"kind": "schema"}},
    }


def test_exact_roundtrip_and_queryable_columns_across_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_storage, "_BATCH_ROWS", 1)
    original = graph()
    before = deepcopy(original)
    path = tmp_path / "record"
    write_graph_record(path, original)
    loaded = read_graph_record(path)
    assert loaded == before == original
    assert digest(loaded) == digest(original)
    assert graph_storage._graph_digest(loaded) == digest(loaded)
    assert graph_storage._graph_digest(loaded, "workflow_merge_v2", "域") == digest("workflow_merge_v2", "域", loaded)
    assert "label" not in loaded["nodes"][2]
    assert loaded["nodes"][1]["label"] is None
    assert [row["id"] for row in loaded["nodes"]] == ["n2", "n1", "n3"]
    assert pq.read_table(path / "nodes.parquet", columns=["id", "type", "label"]).to_pylist() == [
        {"id": "n2", "type": "项目", "label": "中文 / 0012"},
        {"id": "n1", "type": "项目", "label": None},
        {"id": "n3", "type": "项目", "label": None},
    ]
    parquet = pq.ParquetFile(path / "nodes.parquet")
    assert parquet.metadata.num_row_groups == 3
    assert parquet.metadata.row_group(0).column(0).compression == "ZSTD"
    assert pq.read_table(path / "edges.parquet", columns=["source", "target"]).to_pylist() == [
        {"source": "n2", "target": "n1"},
    ]


@pytest.mark.parametrize("value", [{}, {"nodes": []}, {"nodes": [], "edges": []}])
def test_empty_and_absent_collections_have_typed_queryable_files(tmp_path, value):
    path = tmp_path / "empty"
    write_graph_record(path, value)
    assert read_graph_record(path) == value
    assert pq.read_table(path / "nodes.parquet").column_names == [
        "id", "type", "source", "target", "label", "payload"]
    assert pq.read_table(path / "edges.parquet").num_rows == 0


def test_immutable_record_cannot_be_replaced(tmp_path):
    path = tmp_path / "record"
    write_graph_record(path, graph())
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    with pytest.raises(LightGraphError, match="graph_parquet_record_exists"):
        write_graph_record(path, {"nodes": [], "edges": []})
    assert {file.name: file.read_bytes() for file in path.iterdir()} == before


def test_interrupted_write_never_publishes_metadata(tmp_path, monkeypatch):
    path = tmp_path / "interrupted"
    real_write = graph_storage._write_rows

    def fail_edges(file, rows):
        if file.name == "edges.parquet":
            raise OSError("simulated interruption")
        real_write(file, rows)

    monkeypatch.setattr(graph_storage, "_write_rows", fail_edges)
    with pytest.raises(OSError, match="simulated interruption"):
        write_graph_record(path, graph())
    assert (path / "nodes.parquet").is_file()
    assert not (path / "metadata.yaml").exists()
    with pytest.raises(LightGraphError, match="invalid_graph_parquet_record"):
        read_graph_record(path)


def test_metadata_publish_failure_never_exposes_partial_record(tmp_path, monkeypatch):
    path = tmp_path / "interrupted"

    def fail_replace(*args):
        raise OSError("simulated metadata interruption")

    monkeypatch.setattr(graph_storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated metadata interruption"):
        write_graph_record(path, graph())
    assert not (path / "metadata.yaml").exists()
    assert not list(path.glob(".metadata-*"))


def test_file_and_metadata_corruption_are_detected(tmp_path):
    file_path = tmp_path / "file"
    write_graph_record(file_path, graph())
    nodes = file_path / "nodes.parquet"
    nodes.write_bytes(nodes.read_bytes() + b"corruption")
    with pytest.raises(LightGraphError, match="graph_parquet_checksum_mismatch"):
        read_graph_record(file_path)
    metadata_path = tmp_path / "metadata"
    write_graph_record(metadata_path, graph())
    metadata = safe_load_yaml((metadata_path / "metadata.yaml").read_text())
    metadata["graph_metadata"]["access_fingerprint"] = "changed"
    (metadata_path / "metadata.yaml").write_text(dump_yaml(metadata))
    with pytest.raises(LightGraphError, match="graph_parquet_checksum_mismatch"):
        read_graph_record(metadata_path)


@pytest.mark.parametrize("name", ["../nodes.parquet", "/tmp/nodes.parquet", "nested/nodes.parquet"])
def test_metadata_cannot_redirect_files(tmp_path, name):
    path = tmp_path / "record"
    write_graph_record(path, graph())
    metadata = safe_load_yaml((path / "metadata.yaml").read_text())
    metadata["collections"]["nodes"]["file"] = name
    (path / "metadata.yaml").write_text(dump_yaml(metadata))
    with pytest.raises(LightGraphError, match="invalid_graph_parquet_record"):
        read_graph_record(path)


def test_record_files_cannot_be_symlinks(tmp_path):
    path = tmp_path / "record"
    write_graph_record(path, graph())
    nodes = path / "nodes.parquet"
    other = tmp_path / "outside.parquet"
    nodes.rename(other)
    try:
        nodes.symlink_to(other)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(LightGraphError, match="invalid_graph_parquet_record"):
        read_graph_record(path)


def test_metadata_row_count_must_match_parquet(tmp_path):
    path = tmp_path / "record"
    write_graph_record(path, graph())
    metadata = safe_load_yaml((path / "metadata.yaml").read_text())
    metadata["collections"]["nodes"]["rows"] += 1
    (path / "metadata.yaml").write_text(dump_yaml(metadata))
    with pytest.raises(LightGraphError, match="invalid_graph_parquet_record"):
        read_graph_record(path)


def test_integer_row_keys_rejected_instead_of_coerced(tmp_path):
    path = tmp_path / "record"
    value = graph()
    value["nodes"][0]["properties"] = {1: ["value"]}
    with pytest.raises(LightGraphError, match="invalid_graph_parquet_record"):
        write_graph_record(path, value)
    assert not path.exists()


def test_metadata_limit_is_enforced_before_record_publication(tmp_path, monkeypatch):
    from lightgraph.construction import workspace
    monkeypatch.setattr(workspace, "INTERNAL_YAML_MAX_BYTES", 300)
    path = tmp_path / "record"
    with pytest.raises(LightGraphError, match="yaml_document_too_large"):
        write_graph_record(path, {"notes": "x" * 1000, "nodes": [], "edges": []})
    assert not path.exists()


def test_rows_are_not_subject_to_the_yaml_record_limit(tmp_path, monkeypatch):
    from lightgraph.construction import workspace
    monkeypatch.setattr(workspace, "INTERNAL_YAML_MAX_BYTES", 2000)
    value = {"nodes": [{"id": "large", "label": "x" * 20000}], "edges": []}
    path = tmp_path / "record"
    write_graph_record(path, value)
    assert read_graph_record(path) == value
def test_parquet_fsync_uses_writable_descriptor(tmp_path, monkeypatch):
    """Enforce Windows _commit semantics even on POSIX test hosts."""
    import errno
    import os
    from pathlib import Path
    from lightgraph.construction.graph_storage import read_graph_record, write_graph_record

    modes = {}
    flushed = []
    original_open, original_fsync = Path.open, os.fsync

    def record_open(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path.suffix == '.parquet':
            modes[os.fstat(stream.fileno()).st_ino] = mode
        return stream

    def windows_fsync(fd):
        inode = os.fstat(fd).st_ino
        if inode in modes:
            mode = modes[inode]
            if '+' not in mode and not any(flag in mode for flag in 'wax'):
                raise OSError(errno.EBADF, 'fsync requires a writable descriptor')
            flushed.append(inode)
        original_fsync(fd)

    monkeypatch.setattr(Path, 'open', record_open)
    monkeypatch.setattr(os, 'fsync', windows_fsync)
    graph = {'nodes': [{'id': 'n1', 'type': 'Item'}], 'edges': []}
    path = tmp_path / 'snapshot'
    write_graph_record(path, graph)
    assert len(flushed) == 2
    assert read_graph_record(path) == graph

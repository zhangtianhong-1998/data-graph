"""Immutable graph snapshots with bounded Parquet batches and exact JSON rows.

The query columns are projections; ``payload`` is the lossless source of truth
for heterogeneous properties, supports and provenance. A record is readable
only after its metadata is published. Callers advance their manifest afterwards.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError

_FORMAT = "parquet_v1"
_FIELDS = ("id", "type", "source", "target", "label")
_BATCH_ROWS = 4096
_BATCH_BYTES = 8 * 1024 * 1024


def _arrow():
    # Keep YAML-only history usable without importing Arrow at module import.
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise LightGraphError("graph_parquet_dependency_missing") from None
    return pa, pq


def _schema(pa):
    return pa.schema([(name, pa.large_string()) for name in (*_FIELDS, "payload")])


def _json_value(value: Any, depth: int = 0) -> None:
    """Do not silently coerce integer dictionary keys into JSON string keys."""
    if depth > 64:
        raise LightGraphError("invalid_graph_parquet_record")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _json_value(child, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise LightGraphError("invalid_graph_parquet_record")


def _graph_digest(graph: dict) -> str:
    """Match contracts.digest(graph) without building another graph-sized string."""
    checksum = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode([graph]):
        checksum.update(part.encode("utf-8"))
    return checksum.hexdigest()


def _file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _projection(row: dict) -> dict:
    return {key: value if value is None or isinstance(value, str) else canonical(value)
            for key in _FIELDS for value in [row.get(key)]}


def _write_rows(path: Path, rows: list[dict]) -> None:
    pa, pq = _arrow()
    schema = _schema(pa)
    with pq.ParquetWriter(path, schema, compression="zstd", use_dictionary=list(_FIELDS)) as writer:
        batch, size = [], 0
        for row in rows:
            payload = canonical(row)
            batch.append({**_projection(row), "payload": payload})
            size += len(payload.encode("utf-8"))
            if len(batch) >= _BATCH_ROWS or size >= _BATCH_BYTES:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                batch, size = [], 0
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    os.chmod(path, 0o600)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _publish_metadata(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_graph_record(path: Path, graph: dict) -> None:
    """Create a new snapshot directory; never overwrite an existing record.

    JSON object keys in node/edge rows must be strings. Metadata retains the
    existing workspace YAML scalar rules. Failure leaves an unpublished orphan,
    which must never be installed in the owning workspace manifest.
    """
    from .workspace import INTERNAL_YAML_MAX_BYTES, _check_yaml_tree, dump_yaml

    path = Path(path)
    if not isinstance(graph, dict):
        raise LightGraphError("invalid_graph_parquet_record")
    _check_yaml_tree(graph)
    for category in ("nodes", "edges"):
        rows = graph.get(category, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise LightGraphError("invalid_graph_parquet_record")
        _json_value(rows)
    graph_metadata = {key: value for key, value in graph.items() if key not in ("nodes", "edges")}
    metadata = {"format_version": 1, "storage_layout": _FORMAT,
                "graph_metadata": graph_metadata, "graph_digest": _graph_digest(graph), "collections": {}}
    # Reject oversized metadata before starting any files. File details below
    # only add a fixed small amount, checked again before publication.
    if len(dump_yaml(metadata).encode("utf-8")) > INTERNAL_YAML_MAX_BYTES:
        raise LightGraphError("yaml_document_too_large")
    _arrow()
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise LightGraphError("graph_parquet_record_exists") from None
    for category in ("nodes", "edges"):
        file = path / f"{category}.parquet"
        rows = graph.get(category, [])
        _write_rows(file, rows)
        metadata["collections"][category] = {
            "file": file.name, "present": category in graph,
            "rows": len(rows), "sha256": _file_digest(file),
        }
    serialized = dump_yaml(metadata)
    if len(serialized.encode("utf-8")) > INTERNAL_YAML_MAX_BYTES:
        raise LightGraphError("yaml_document_too_large")
    _publish_metadata(path / "metadata.yaml", serialized)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_rows(path: Path, expected_rows: int) -> list[dict]:
    pa, pq = _arrow()
    result = []
    with pq.ParquetFile(path) as parquet:
        if (not parquet.schema_arrow.equals(_schema(pa))
                or parquet.metadata.num_rows != expected_rows):
            raise LightGraphError("invalid_graph_parquet_record")
        for batch in parquet.iter_batches(batch_size=_BATCH_ROWS):
            for projected in batch.to_pylist():
                row = json.loads(projected.pop("payload"), object_pairs_hook=_unique_object)
                if not isinstance(row, dict):
                    raise LightGraphError("invalid_graph_parquet_record")
                _json_value(row)
                if _projection(row) != projected:
                    raise LightGraphError("invalid_graph_parquet_record")
                result.append(row)
    if len(result) != expected_rows:
        raise LightGraphError("invalid_graph_parquet_record")
    return result


def read_graph_record(path: Path) -> dict:
    """Read and verify one snapshot; the caller enforces its workspace boundary."""
    from .workspace import INTERNAL_YAML_MAX_BYTES, safe_load_yaml

    path = Path(path)
    try:
        metadata_path = path / "metadata.yaml"
        if path.is_symlink() or metadata_path.is_symlink() or not path.is_dir():
            raise LightGraphError("invalid_graph_parquet_record")
        if metadata_path.stat().st_size > INTERNAL_YAML_MAX_BYTES:
            raise LightGraphError("yaml_document_too_large")
        metadata = safe_load_yaml(metadata_path.read_text(encoding="utf-8"),
                                  max_bytes=INTERNAL_YAML_MAX_BYTES)
        if (not isinstance(metadata, dict) or metadata.get("format_version") != 1
                or metadata.get("storage_layout") != _FORMAT
                or not isinstance(metadata.get("graph_metadata"), dict)
                or not isinstance(metadata.get("collections"), dict)
                or set(metadata["collections"]) != {"nodes", "edges"}
                or {"nodes", "edges"} & set(metadata["graph_metadata"])):
            raise LightGraphError("invalid_graph_parquet_record")
        graph = metadata["graph_metadata"]
        for category in ("nodes", "edges"):
            entry = metadata["collections"][category]
            if (not isinstance(entry, dict) or entry.get("file") != f"{category}.parquet"
                    or not isinstance(entry.get("present"), bool)
                    or type(entry.get("rows")) is not int or entry["rows"] < 0
                    or not entry["present"] and entry["rows"] != 0):
                raise LightGraphError("invalid_graph_parquet_record")
            file = path / entry["file"]
            if file.is_symlink() or file.resolve().parent != path.resolve():
                raise LightGraphError("invalid_graph_parquet_record")
            if _file_digest(file) != entry.get("sha256"):
                raise LightGraphError("graph_parquet_checksum_mismatch")
            rows = _read_rows(file, entry["rows"])
            if entry["present"]:
                graph[category] = rows
        if _graph_digest(graph) != metadata.get("graph_digest"):
            raise LightGraphError("graph_parquet_checksum_mismatch")
        return graph
    except LightGraphError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise LightGraphError("invalid_graph_parquet_record") from None

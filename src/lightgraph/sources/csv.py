"""Strict, explicitly configured CSV input backed only by in-memory SQLite."""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
from pathlib import Path

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from .memory import MemorySource


class _BoundedInput(io.RawIOBase):
    """Hash original input while bounding bytes without making a parsed copy."""

    def __init__(self, stream, maximum):
        self.stream = stream
        self.maximum = maximum
        self.count = 0
        self.hasher = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, buffer):
        size = self.stream.readinto(buffer)
        if size:
            self.count += size
            if self.count > self.maximum:
                raise LightGraphError("csv_file_limit")
            self.hasher.update(memoryview(buffer)[:size])
        return size

    def close(self):
        self.stream.close()
        super().close()


class _BoundedLines:
    def __init__(self, stream, maximum):
        self.stream = stream
        self.maximum = maximum
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(self.maximum + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        # csv.reader may join many physical lines into one quoted record.
        if self.record_chars > self.maximum:
            raise LightGraphError("memory_budget_exhausted")
        return line


class CSVSource(MemorySource):
    """Read a CSV using explicit format choices; never infer numeric codes.

    ``text`` preserves every non-NULL field verbatim, including whitespace and
    leading zeroes. NULL tokens are exact and apply to quoted and unquoted cells.
    Header-free files require declared column names; malformed rows fail closed.
    """

    def __init__(self, path, *, source_id="csv", encoding, delimiter, header,
                 null_values, type_inference, columns=None, table_name=None,
                 schema_name="public", header_row=1, limits=None,
                 max_file_bytes=200 * 1024 * 1024, max_rows=200000,
                 max_columns=2000, max_memory_bytes=128 * 1024 * 1024,
                 query_budget=None, metadata_only=False):
        path = Path(path)
        if (not isinstance(source_id, str) or not source_id or
                not isinstance(delimiter, str) or len(delimiter) != 1 or
                delimiter in '\r\n"\x00' or not isinstance(header, bool) or
                not isinstance(null_values, list) or
                any(not isinstance(v, str) for v in null_values) or
                type_inference != "text" or not isinstance(header_row, int) or
                not 1 <= header_row <= 100 or (not header and header_row != 1) or
                (header and columns is not None) or (not header and not columns)):
            raise LightGraphError("csv_format_config_invalid")
        if min(max_file_bytes, max_rows, max_columns, max_memory_bytes) <= 0:
            raise LightGraphError("csv_format_config_invalid")
        try:
            codecs.lookup(encoding)
        except (LookupError, TypeError):
            raise LightGraphError("csv_encoding_invalid") from None
        if not path.is_file() or path.stat().st_size > max_file_bytes:
            raise LightGraphError("csv_file_limit")
        self.metadata_only = metadata_only
        self.format_metadata = {
            "encoding": encoding, "delimiter": delimiter, "header": header,
            "header_row": header_row, "null_values": list(null_values),
            "type_inference": "text", "table_name": table_name or path.stem,
            "schema_name": schema_name,
        }
        before = path.stat()
        null_tokens = set(null_values)
        try:
            bounded = _BoundedInput(path.open("rb"), max_file_bytes)
            with io.TextIOWrapper(io.BufferedReader(bounded), encoding=encoding,
                                  errors="strict", newline="") as stream:
                lines = _BoundedLines(stream, max(1, max_memory_bytes // 8))
                reader = csv.reader(lines, delimiter=delimiter, strict=True)

                def next_record():
                    lines.record_chars = 0
                    return next(reader)

                if header:
                    names = None
                    for _ in range(header_row):
                        names = next_record()
                else:
                    names = list(columns)
                if (not names or len(names) > max_columns or
                        any(not isinstance(n, str) or not n or "\x00" in n for n in names)):
                    raise LightGraphError("csv_header_invalid")
                if len(names) != len(set(names)):
                    raise LightGraphError("csv_duplicate_header")
                table = TableSpec(
                    source_id=source_id, schema=schema_name,
                    name=table_name or path.stem,
                    columns=[ColumnSpec(name=n, data_type="text",
                                        comment="CSV field parsed as text; no declared type")
                             for n in names],
                )

                def records():
                    count = 0
                    while True:
                        try:
                            raw = next_record()
                        except StopIteration:
                            return
                        count += 1
                        if count > max_rows:
                            raise LightGraphError("csv_row_limit")
                        if len(raw) != len(names):
                            raise LightGraphError("csv_row_shape_invalid")
                        if any("\x00" in cell for cell in raw):
                            raise LightGraphError("csv_value_invalid")
                        yield tuple(None if cell in null_tokens else cell for cell in raw)

                super().__init__(
                    [table], {table.qualified_name: () if metadata_only else records()},
                    limits=limits, max_memory_bytes=max_memory_bytes,
                    query_budget=query_budget,
                )
                if not metadata_only:
                    self.format_metadata["input_revision"] = bounded.hasher.hexdigest()
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise LightGraphError("csv_input_changed")
        except LightGraphError:
            if hasattr(self, "_db") and not getattr(self, "_closed", False):
                self.close()
            raise
        except (UnicodeError, csv.Error, StopIteration, OSError, ValueError, TypeError):
            if hasattr(self, "_db") and not getattr(self, "_closed", False):
                self.close()
            raise LightGraphError("csv_parse_failed") from None

    def execute(self, query, *, row_limit=None):
        if self.metadata_only:
            raise LightGraphError("metadata_only_query_forbidden")
        yield from super().execute(query, row_limit=row_limit)

    def capabilities(self):
        result = super().capabilities()
        result.product = "csv_memory"
        return result

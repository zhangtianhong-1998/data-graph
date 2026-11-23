"""Read-only Excel input; no formula execution, network access, or staging files."""
from __future__ import annotations
from array import array
import hashlib
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, time as daytime
import re
import sqlite3
import time
import zipfile
from xml.etree import ElementTree as ET
import openpyxl
from lightgraph.contracts import TableSpec, ColumnSpec, QueryLimits
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler, SQLGuard
from lightgraph.query.compiler import ident
from lightgraph.query.predicate import ScopeConjunction
from .base import row_bytes
from .memory import MemorySource
from .budget import BudgetedCursor


def _cell_value(cell, cached):
    if cell is None:
        return None
    if cell.data_type == "f":
        return cached.value if cached is not None else None
    if cell.value is None and cell.data_type == "inlineStr":
        return ""
    return cell.value


class ExcelSource(MemorySource):
    def __init__(self, path: str | Path, *, source_id: str = "excel", limits: QueryLimits | None = None, header_row: int = 1, include_hidden: bool = False, max_file_bytes: int = 200 * 1024 * 1024, max_rows: int = 200000, max_columns: int = 2000, max_memory_bytes: int = 128 * 1024 * 1024, query_budget=None, metadata_only: bool = False, allowed_tables: list[str] | None = None, allowed_columns: dict[str, list[str]] | None = None):
        path = Path(path)
        self._sheet_rows = {}
        self._position_bytes = 0
        self._witness_table = None
        self.workbook_snapshot = None
        selected_tables = set(allowed_tables) if allowed_tables is not None else None
        selected_columns = allowed_columns or {}
        if allowed_tables is not None and len(allowed_tables) != len(selected_tables):
            raise LightGraphError("duplicate_authorized_table")
        if selected_tables is not None and set(selected_columns) - selected_tables:
            raise LightGraphError("unauthorized_column_scope")
        self.metadata_only = metadata_only
        self.issues: list[dict] = []
        self.issue_counts: dict[str, int] = {}
        self._pending_formula_columns: dict[str, set[str]] = {}
        if not path.is_file() or path.stat().st_size > max_file_bytes: raise LightGraphError("excel_file_limit")
        if path.suffix.lower() != ".xlsx": raise LightGraphError("excel_format_unsupported")
        if header_row < 1 or header_row > 100: raise LightGraphError("excel_header_invalid")
        before = path.stat()
        try:
            if not metadata_only:
                with path.open("rb") as source_file:
                    self.workbook_snapshot = hashlib.file_digest(source_file, "sha256").hexdigest()
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if sum(x.file_size for x in members) > max_memory_bytes * 4 or len(members) > 10000:
                    raise LightGraphError("excel_expansion_limit")
                if any("vbaproject" in x.filename.lower() or "externallinks/" in x.filename.lower() for x in members):
                    raise LightGraphError("excel_active_content_forbidden")
                # Reject XML entity declarations before XML readers see them.
                for member in members:
                    if member.filename.endswith(".xml"):
                        with archive.open(member) as stream:
                            tail = b""
                            while block := stream.read(65536):
                                data = tail + block
                                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                                    raise LightGraphError("excel_xml_entities_forbidden")
                                tail = data[-32:]
            book = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
            values_book = None if metadata_only else openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
        except LightGraphError: raise
        except Exception: raise LightGraphError("excel_parse_failed") from None
        tables, rows, physical_columns = [], {}, {}
        try:
            for ws in book.worksheets:
                if selected_tables is not None and f"public.{ws.title}" not in selected_tables:
                    continue
                if ws.sheet_state != "visible" and not include_hidden: continue
                if (ws.max_column or 0) > max_columns: raise LightGraphError("excel_column_limit")
                # Count while reading too: dimensions in source files may lie.
                source_rows = ws.iter_rows(min_row=header_row, max_row=header_row if metadata_only else None)
                value_rows = iter(()) if values_book is None else values_book[ws.title].iter_rows(min_row=header_row)
                header = next(source_rows, ()); next(value_rows, ())
                if metadata_only:
                    source_rows.close()
                if not header: continue
                if any(c.data_type == "f" for c in header): raise LightGraphError("excel_formula_header")
                last = max((i for i,c in enumerate(header) if c.value is not None), default=-1)
                if last < 0: continue
                names = [str(c.value) if c.value is not None else "" for c in header[:last+1]]
                if any(not n for n in names): raise LightGraphError("excel_merged_or_missing_header_requires_schema")
                if len(names) != len(set(names)): raise LightGraphError("excel_duplicate_header")
                table = TableSpec(source_id=source_id, schema="public", name=ws.title, columns=[ColumnSpec(name=n, data_type="unknown" if metadata_only else "text", comment="Uploaded Excel header") for n in names])
                hidden_rows, hidden_columns = self._hidden_positions(path, ws, include_hidden, columns_only=metadata_only)
                visible = [i for i in range(len(names)) if i + 1 not in hidden_columns]
                physical_columns[table.qualified_name] = list(names)
                requested_columns = selected_columns.get(table.qualified_name)
                if requested_columns is not None and (not requested_columns or
                        not set(requested_columns) <= {names[i] for i in visible}):
                    raise LightGraphError("authorized_column_unavailable")
                kept = [i for i in visible if requested_columns is None or names[i] in requested_columns]
                table.columns = [table.columns[i] for i in kept]
                table._workbook_ordinals = {names[i]: i + 1 for i in kept}
                if not kept: continue
                tables.append(table)
                if metadata_only:
                    # Header-only sources must not look like empty instance tables.
                    # execute() explicitly rejects queries instead of returning zero rows.
                    rows[table.qualified_name] = ()
                    continue
                excluded = set(visible) - set(kept)
                sheet_rows = self._sheet_rows[table.qualified_name] = array("I")
                def iterate(sheet=ws, sr=source_rows, vr=value_rows, keep=kept, hidden=hidden_rows,
                            column_names=tuple(names), excluded_columns=excluded, positions=sheet_rows):
                    for index, (raw, cached) in enumerate(zip(sr, vr), header_row + 1):
                        if index - header_row > max_rows: raise LightGraphError("excel_row_limit")
                        if index in hidden: continue
                        if len(raw) > max_columns: raise LightGraphError("excel_column_limit")
                        converted = []
                        for i in keep:
                            cell = raw[i] if i < len(raw) else None
                            cache = cached[i] if i < len(cached) else None
                            value = _cell_value(cell, cache)
                            if cell is not None and cell.data_type == "f" and value is None:
                                self._pending_formula_columns.setdefault(f"public.{sheet.title}", set()).add(column_names[i])
                                self.issue_counts["formula_cache_missing"] = self.issue_counts.get("formula_cache_missing", 0) + 1
                                if len(self.issues) < 32:
                                    self.issues.append({"code": "formula_cache_missing", "sheet": sheet.title, "row": index, "column": i + 1})
                            if isinstance(value, (date, datetime, daytime)): value = value.isoformat()
                            if isinstance(value, int) and not isinstance(value, bool) and cell is not None and re.fullmatch("0+", cell.number_format or ""):
                                value = str(value).zfill(len(cell.number_format))
                            converted.append(value)
                        populated = any(v is not None for v in converted)
                        if not populated:
                            # Preserve the full import's row set. An explicit empty
                            # string counts as data; a formula without a cache does not.
                            populated = any(_cell_value(raw[i], cached[i] if i < len(cached) else None)
                                            is not None for i in excluded_columns if i < len(raw))
                        if populated:
                            record = tuple(converted)
                            if self.workbook_snapshot:
                                # Mirror the ordinary imported-row charge before yielding.
                                # At the existing limit, retain input data and discard the
                                # complete optional position index across every sheet.
                                required = self._input_bytes + row_bytes(record) + 64
                                self._position_bytes += positions.itemsize
                                if required + self._position_bytes > max_memory_bytes:
                                    for saved_positions in self._sheet_rows.values():
                                        del saved_positions[:]
                                    self._sheet_rows.clear()
                                    self._position_bytes = 0
                                    self.workbook_snapshot = None
                                else:
                                    positions.append(index)
                            yield record
                rows[table.qualified_name] = iterate()
            available_tables = {table.qualified_name for table in tables}
            if selected_tables is not None and selected_tables - available_tables:
                raise LightGraphError("authorized_table_unavailable_or_view")
            if set(selected_columns) - available_tables:
                raise LightGraphError("unauthorized_column_scope")
            if not tables: raise LightGraphError("excel_no_visible_tables")
            super().__init__(tables, rows, limits=limits, max_memory_bytes=max_memory_bytes, query_budget=query_budget)
            if not metadata_only:
                self._infer_loaded_types()
                after = path.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise LightGraphError("excel_input_changed")
            self.compiler = QueryCompiler(self._tables, dialect="sqlite", physical_columns=physical_columns)
        except LightGraphError:
            if hasattr(self, "_db"): self.close()
            raise
        except Exception:
            if hasattr(self, "_db"): self.close()
            raise LightGraphError("excel_parse_failed") from None
        finally:
            book.close()
            if values_book is not None:
                values_book.close()

    def _infer_loaded_types(self):
        """Classify all imported non-null cells using value-free SQL aggregates.

        SQLite's BLOB affinity preserves numeric cells versus textual codes. A
        mixed column remains projectable for discovery but cannot be filtered
        through typed predicates until its interpretation is resolved.
        """
        shapes = ("integer", "real", "text", "blob")
        for table in self._tables:
            for start in range(0, len(table.columns), 128):
                columns = table.columns[start:start + 128]
                projections = [f"MAX(CASE WHEN typeof({ident(column.name)}) = '{shape}' THEN 1 ELSE 0 END)"
                               for column in columns for shape in shapes]
                sql = f"SELECT {', '.join(projections)} FROM {ident(table.schema_name)}.{ident(table.name)}"
                cursor = BudgetedCursor(self._db.cursor(), self.query_budget, cancel=self._db.interrupt)
                began = time.monotonic()
                self._db.set_progress_handler(lambda: int(time.monotonic() > min(
                    self.query_budget.deadline, began + self.limits.timeout_ms / 1000)), 500)
                try:
                    cursor.execute(sql)
                    self.stats.queries += 1
                    counts = cursor.fetchone()
                    for i, column in enumerate(columns):
                        present = {shape for shape, count in zip(shapes, counts[i * 4:i * 4 + 4]) if count}
                        if not present or present == {"text"}: column.data_type = "text"
                        elif present == {"integer"}: column.data_type = "integer"
                        elif present <= {"integer", "real"}: column.data_type = "real"
                        else: column.data_type = "mixed"
                except sqlite3.Error:
                    self.query_budget.check_time()
                    raise LightGraphError("excel_type_inference_failed") from None
                finally:
                    cursor.close(); self._db.set_progress_handler(None, 0)
                    self.stats.elapsed_seconds += time.monotonic() - began

    def project(self, table, columns, predicate=None, distinct=True, limit=None):
        if self.metadata_only:
            raise LightGraphError("metadata_only_query_forbidden")
        required=set(columns)
        def predicate_columns(value):
            if isinstance(value, ScopeConjunction):
                return predicate_columns(value.user_predicate) | predicate_columns(value.partition_filter)
            if not isinstance(value,dict): return set()
            logical = set(value) & {"all", "any", "and", "not"}
            if logical:
                result = set()
                for op in logical:
                    children = [value[op]] if op == "not" else value[op]
                    if isinstance(children, list):
                        for child in children: result.update(predicate_columns(child))
                return result
            if "column" in value: return {value["column"]}
            return set(value)
        required.update(predicate_columns(predicate))
        if required & self._pending_formula_columns.get(table.qualified_name,set()):
            # A missing cache is unknown, never a real NULL. Publishing its
            # incomplete projection could wrongly retract previously known facts.
            raise LightGraphError("formula_cache_missing_in_projection")
        yield from super().project(table,columns,predicate,distinct,limit)

    def _check_formula_query(self, query):
        """Missing formula caches remain unknown through every SQL entry point.

        The normal catalog guard validates first. Removing unavailable columns
        from a second catalog then catches their use in SELECT, filters, aliases,
        nested queries and joins without guessing references from SQL text.
        """
        if self.metadata_only:
            raise LightGraphError("metadata_only_query_forbidden")
        if self._pending_formula_columns:
            self.compiler.guard.validate(query.sql, query.parameters)
            tables = self.describe()
            for table in tables:
                missing = self._pending_formula_columns.get(table.qualified_name, set())
                table.columns = [column for column in table.columns if column.name not in missing]
            try:
                SQLGuard(tables, dialect="sqlite", physical_columns=self.compiler.guard.physical_columns).validate(
                    query.sql, query.parameters)
            except LightGraphError as exc:
                if exc.code == "unauthorized_column":
                    raise LightGraphError("formula_cache_missing_in_projection") from None
                raise

    def execute(self, query, *, row_limit=None):
        self._check_formula_query(query)
        yield from super().execute(query, row_limit=row_limit)

    def _authorize(self, action, arg1, arg2, db, trigger):
        if self._witness_table:
            if (action == sqlite3.SQLITE_READ and (db, arg1) == self._witness_table
                    and (arg2 or '').casefold() == 'rowid'):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and arg2 == 'lg_capture_row':
                return sqlite3.SQLITE_OK
        return super()._authorize(action, arg1, arg2, db, trigger)

    def sheet_row_snapshot(self, table):
        # SQLite's implicit row identifier is addressable through any alias the
        # imported table has not shadowed. A source cell must never serve as a locator.
        names = {c.casefold() for c in self.compiler.tables[table.qualified_name].column_names}
        if {'rowid', '_rowid_', 'oid'} <= names:
            return None
        return self.workbook_snapshot

    @contextmanager
    def sheet_row_projection(self, table, columns, *, predicate, limit, witness_bytes):
        """Keep DISTINCT representatives and return-column budgets identical to normal projection.

        The private observer sees each qualifying source row while SQLite scans its immutable
        worksheet. Grouping and output still belong to the ordinary DISTINCT operator. The
        caller reads the locator only after exhausting the complete cursor, since duplicates
        may occur after the last newly observed tuple.
        """
        snapshot = self.sheet_row_snapshot(table)
        query = self.compiler.projection(table, columns, predicate=predicate, limit=limit)
        self._check_formula_query(query)
        if (not snapshot or len(columns) + 1 > self._db.getlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG)
                or witness_bytes <= 0):
            yield self.project(table, columns, predicate=predicate, limit=limit), lambda row: None
            return
        params = {}
        fields = self.compiler.columns(table, columns)
        where = self.compiler.predicate(table, predicate, params)
        names = {c.casefold() for c in self.compiler.tables[table.qualified_name].column_names}
        alias = next(a for a in ('rowid', '_rowid_', 'oid') if a not in names)
        sql = (f"SELECT DISTINCT {', '.join(fields)} FROM {self.compiler.table(table)} "
               f"WHERE CASE WHEN {where} THEN lg_capture_row({ident(alias)}, {', '.join(fields)}) ELSE 0 END "
               "LIMIT :cap")
        params['cap'] = limit + 1
        positions = self._sheet_rows[table.qualified_name]
        members = {}
        used_bytes = 0

        def capture(sqlite_id, *values):
            nonlocal used_bytes
            if used_bytes > witness_bytes:
                return 1
            # Python tuple equality matches SQLite DISTINCT for these BLOB-affinity
            # file values, including integer 1 and real 1.0. Preserve types separately.
            float_mask = sum(1 << i for i, v in enumerate(values) if isinstance(v, float))
            if values not in members:
                used_bytes += row_bytes(values) + 256
                members[values] = (float_mask, array('I'))
            group = members[values]
            if group is not None:
                if group[0] == float_mask:
                    group[1].append(positions[sqlite_id - 1])
                    used_bytes += group[1].itemsize
                else:
                    # One SQL representative covers several concrete source types.
                    members[values] = None
            if used_bytes > witness_bytes:
                members.clear()
            return 1

        def witness(row):
            group = members.get(tuple(row[c] for c in columns))
            if group is None:
                return None
            ranges = []
            for index in sorted(group[1]):
                if ranges and ranges[-1][1] + 1 == index:
                    ranges[-1][1] = index
                else:
                    ranges.append([index, index])
            return {'kind': 'excel_rows_v1', 'snapshot': snapshot, 'rows': ranges}

        with self._lock:
            if self._closed:
                raise LightGraphError('source_closed')
            self._db.create_function('lg_capture_row', -1, capture)
            self._witness_table = (table.schema_name, table.name)
            stream = self._read_cursor(sql, params, (), limit)
            try:
                yield stream, witness
            finally:
                stream.close()
                self._witness_table = None
                self._db.create_function('lg_capture_row', -1, None)

    @staticmethod
    def _hidden_positions(path, ws, include_hidden, *, columns_only=False):
        if include_hidden: return set(), set()
        hidden_rows, hidden_columns = set(), set()
        # read_only worksheets omit dimension metadata. Inspect XML without
        # materializing the sheet, using the internal path supplied by openpyxl.
        with zipfile.ZipFile(path) as archive:
            with archive.open(ws._worksheet_path) as stream:
                for event, elem in ET.iterparse(stream, events=("start", "end")):
                    tag = elem.tag.rsplit("}",1)[-1]
                    # Column dimensions precede sheetData in worksheet XML. A
                    # metadata-only load has no need to walk instance row flags.
                    if columns_only and event == "start" and tag == "sheetData":
                        break
                    if event != "end":
                        continue
                    if tag == "row" and elem.attrib.get("hidden") in ("1", "true"):
                        hidden_rows.add(int(elem.attrib["r"]))
                    elif tag == "col" and elem.attrib.get("hidden") in ("1", "true"):
                        hidden_columns.update(range(int(elem.attrib["min"]), int(elem.attrib["max"]) + 1))
                    elem.clear()
        return hidden_rows, hidden_columns
    def capabilities(self):
        result = super().capabilities(); result.product = "excel_memory"
        return result

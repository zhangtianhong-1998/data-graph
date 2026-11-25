"""Resolve document bindings against the project's exact source/table declarations."""
from functools import lru_cache
from pathlib import Path

from ..errors import LightGraphError


@lru_cache(maxsize=32)
def _workbook_tables(path, revision, schema, header_row, include_hidden, allowed, columns, memory_limit):
    # The revision separates reuploads and file edits. Only names survive this call.
    from ..sources.excel import ExcelSource
    with ExcelSource(path, schema_name=schema, metadata_only=True, header_row=header_row, include_hidden=include_hidden,
                     allowed_tables=list(allowed) or None, allowed_columns=dict(columns) or None,
                     max_memory_bytes=memory_limit) as workbook:
        return tuple(table.name for table in workbook.describe())


def project_tables(workspace, pid, config) -> set[tuple[str, str, str]]:
    """Return authorized names without opening a DB or reading spreadsheet data rows."""
    tables = set()
    for source in config.get("sources", []):
        allowed = set(source.get("allowed_tables") or [])
        sid = source["id"]
        if source["kind"] == "csv_file":
            schema = source.get("schema_name", "public")
            name = source.get("table_name") or Path(source["path"]).stem
            if not allowed or f"{schema}.{name}" in allowed:
                tables.add((sid, schema, name))
        elif source["kind"] == "excel_workbook":
            path = workspace._path(pid, source["path"])
            schema = source.get("schema_name", "public")
            try:
                file_stat = path.stat()
                columns = tuple(sorted((key, tuple(names)) for key, names in (source.get("allowed_columns") or {}).items()))
                names = _workbook_tables(path, (file_stat.st_ino, file_stat.st_ctime_ns, file_stat.st_mtime_ns, file_stat.st_size),
                                         schema, source.get("header_row", 1), source.get("include_hidden", False),
                                         tuple(sorted(allowed)), columns, config["max_working_bytes"])
            except (OSError, LightGraphError):
                continue
            tables.update((sid, schema, name) for name in names)
        else:
            for qualified in allowed:
                schema, sep, name = qualified.partition(".")
                if sep and schema and name:
                    tables.add((sid, schema, name))
    return tables


def table_lookup(keys):
    """Keep every physical match, including collisions of short or qualified spellings."""
    lookup = {}
    for sid, schema, name in sorted(keys):
        qualified = f"{sid}.{schema}.{name}"
        for form in {name, f"{schema}.{name}", qualified}:
            lookup.setdefault(form, []).append(qualified)
    return lookup


def validate_assignment(workspace, pid, config, document):
    """Validate a newly assigned document before admitting it to extraction."""
    from ..contracts import TableSpec
    from ..documents.core import DocumentCorpus
    from .deletion import validate_rebinding
    scopes = [document.scope, *(s.scope for s in document.sections if s.scope)]
    if any(scope.columns for scope in scopes):
        validate_rebinding(workspace, pid, document)
    elif any(scope.source_ids or scope.tables for scope in scopes):
        tables = [TableSpec(source_id=sid, schema=schema, name=name, columns=[])
                  for sid, schema, name in project_tables(workspace, pid, config)]
        DocumentCorpus([document]).bind_catalog(tables)

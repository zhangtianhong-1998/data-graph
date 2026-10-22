"""Configuration-stage previews contain schema declarations, never sample rows."""
from __future__ import annotations

import yaml

from ..catalog import CATALOG_VERSION, DeclaredCatalog, SourceDescription
from ..contracts import TableSpec, digest
from ..errors import LightGraphError
from .execution import read_metadata


def _yaml_files(tables: list[dict], sources: list[dict] | None = None) -> list[dict]:
    """Split a catalog for inspection without inventing unobserved constraints."""
    def identity(table):
        return {key: table[key] for key in ("source_id", "schema", "name")}

    declarations, constraints, foreign_keys = [], [], []
    for table in tables:
        base = identity(table)
        declarations.append({**base, "comment": table.get("comment", ""),
                             "columns": table["columns"]})
        constraints.append({**base,
            "primary_key": [column["name"] for column in table["columns"] if column.get("primary_key")],
            "unique_keys": table.get("unique_keys", []),
        })
        for key in table.get("foreign_keys", []):
            foreign_keys.append({"source": base, **key})
    values = [
        ("tables.yaml", {"coverage": "declarations_only", "tables": declarations}),
        ("constraints.yaml", {"coverage": "declared_primary_and_unique_only",
                              "check_constraints": "unknown", "defaults": "unknown",
                              "sources": sources or [], "tables": constraints}),
        ("foreign_keys.yaml", {"coverage": "declared_only", "foreign_keys": foreign_keys}),
    ]
    return [{"name": name, "content": yaml.safe_dump(value, allow_unicode=True, sort_keys=False)}
            for name, value in values]


def preview_file_metadata(workspace, pid: str, fid: str) -> dict:
    """Parse only this uploaded file, even when another database is unavailable.

    The caller holds the project lock and forbids edits during an extraction.
    File adapters may inspect uploaded cells in memory to infer column types;
    only the resulting declarations are saved and returned here.
    """
    file_path = workspace.file_path(pid, fid)
    config = workspace.resolved_config(pid)
    matching = [source for source in config.sources if source.path == str(file_path)
                and source.kind in {"excel_workbook", "csv_file"}]
    if len(matching) != 1:
        raise LightGraphError("file_metadata_source_missing")
    # Use the same source-bound cache as database onboarding. Refreshing the
    # browser can then retain these declarations without reopening any source.
    view = read_metadata(workspace, pid, refresh=True, source_ids=[matching[0].id])
    tables = [{key: value for key, value in table.items() if key != "id"}
              for table in view["tables"] if table["source_id"] == matching[0].id]
    sources = [source for source in view["sources"] if source["source_id"] == matching[0].id]
    catalog = DeclaredCatalog(
        tables=[TableSpec.model_validate(table) for table in tables],
        sources=[SourceDescription.model_validate(source) for source in sources],
        revision=digest(CATALOG_VERSION, tables, sources))
    content = yaml.safe_dump(catalog.model_dump(mode="json", by_alias=True),
                             allow_unicode=True, sort_keys=False)
    # This file is only the requested input's preview. The full project cache
    # separately reports unread sources and never calls partial metadata complete.
    target = workspace._path(pid, "metadata")
    target.mkdir(exist_ok=True)
    workspace._atomic(workspace._path(pid, f"metadata/{fid}.yaml"), content.encode("utf-8"))
    return {"tables": tables, "yaml": content, "files": _yaml_files(tables, sources),
            "coverage": "declarations_only"}


def metadata_yaml(workspace, pid: str, *, source_id: str | None = None,
                  schema: str | None = None, table: str | None = None) -> dict:
    """Read only a current cached catalog; exporting YAML never opens a database."""
    view = read_metadata(workspace, pid)
    if view.get("requires_refresh") and not view.get("tables"):
        return {"files": [], "requires_refresh": True}
    tables = [item for item in view["tables"]
              if (source_id is None or item["source_id"] == source_id)
              and (schema is None or item["schema"] == schema)
              and (table is None or item["name"] == table)]
    source_ids = {item["source_id"] for item in tables}
    sources = [item for item in view.get("sources", []) if item["source_id"] in source_ids]
    return {"files": _yaml_files(tables, sources) if tables else [],
            "requires_refresh": bool(view.get("requires_refresh")),
            "missing_sources": view.get("missing_sources", [])}

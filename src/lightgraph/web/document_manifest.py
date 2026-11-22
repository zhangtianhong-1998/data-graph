"""Import project documents from a client-supplied manifest, without resolving client paths on the server."""
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from uuid import uuid4

from ..contracts import TableSpec
from ..documents.core import DocumentCorpus, load_document
from ..errors import LightGraphError
from .workspace import FileRecord, MAX_TEXT_BYTES, _bound_markdown, _filename, _frontmatter, _PURPOSES, safe_yaml


class DocumentManifestError(LightGraphError):
    def __init__(self, field, hint):
        super().__init__("invalid_document_manifest")
        self.diagnostics = [{"path": field, "type": "manifest", "hint": hint}]


@dataclass(frozen=True)
class Entry:
    field: str
    path: str
    purpose: str
    tables: list[str]


def parse_manifest(data: bytes) -> list[Entry]:
    try:
        raw = safe_yaml(data.decode("utf-8-sig"))
    except (UnicodeError, LightGraphError):
        raise DocumentManifestError("documents", "请使用 UTF-8 YAML，检查缩进、重复字段和别名。") from None
    if not isinstance(raw, dict) or set(raw) != {"documents"} or not isinstance(raw["documents"], list) or not raw["documents"]:
        raise DocumentManifestError("documents", "请在 documents 下列出至少一份文档。")
    entries = []
    seen = set()
    for i, item in enumerate(raw["documents"]):
        field = f"documents[{i}]"
        if not isinstance(item, dict) or set(item) - {"path", "type", "tables"}:
            raise DocumentManifestError(field, "文档条目支持 path、type、tables 三个字段。")
        path = item.get("path")
        try:
            if not isinstance(path, str) or len(path) > 4096:
                raise ValueError
            for part in path.split("/"):
                _filename(part)
            if PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}:
                raise ValueError
        except (LightGraphError, ValueError):
            raise DocumentManifestError(field + ".path", "请填写以 / 分隔的 Markdown 相对路径，例如 docs/guide.md。") from None
        if path in seen:
            raise DocumentManifestError(field + ".path", "清单中重复指定了这个文档路径。")
        seen.add(path)
        purpose = item.get("type", "data_description")
        if not isinstance(purpose, str) or purpose not in _PURPOSES:
            raise DocumentManifestError(field + ".type", "支持 data_description、analysis_knowledge、mixed 和 unresolved。")
        tables = item.get("tables", [])
        if not isinstance(tables, list) or ("tables" not in item and purpose != "analysis_knowledge"):
            raise DocumentManifestError(field + ".tables", "请列出关联表名。全局使用请填写 tables: []，分析知识也可以省略此字段。")
        for j, selector in enumerate(tables):
            if not isinstance(selector, str) or not selector.strip():
                raise DocumentManifestError(f"{field}.tables[{j}]", "表名必须为非空字符串。")
        entries.append(Entry(field, path, purpose, tables))
    return entries


def _existing(entries, project):
    by_name = {}
    by_path = {}
    for record in project["files"]:
        if record["kind"] == "markdown":
            by_name.setdefault(record["name"], []).append(record)
            for path in {record["relative_path"], record.get("manifest_path")} - {None}:
                by_path.setdefault(path, []).append(record)
    existing = {}
    used_ids = set()
    for entry in entries:
        # Preserve directory identity. A bare name may also refer to a pre-manifest upload.
        matches = by_path.get(entry.path, [])
        if not matches and "/" not in entry.path:
            matches = by_name.get(entry.path, [])
        if len(matches) > 1:
            raise DocumentManifestError(entry.field + ".path", "项目中存在同名文档，请使用卡片上的项目文件路径。")
        if matches:
            record = matches[0]
            if record["file_id"] in used_ids:
                raise DocumentManifestError(entry.field + ".path", "此项目文档已被清单中的其他条目引用。")
            existing[entry.path] = record
            used_ids.add(record["file_id"])
    return existing


def manifest_paths(entries, project):
    existing = _existing(entries, project)
    paths = [entry.path for entry in entries]
    return {"paths": paths, "missing_paths": [path for path in paths if path not in existing]}


def _resolve_tables(entry, lookup):
    result = []
    for j, table in enumerate(entry.tables):
        field = f"{entry.field}.tables[{j}]"
        matches = lookup.get(table, [])
        if not matches:
            raise DocumentManifestError(field, "当前项目没有这张表，请核对数据来源、模式和完整表名。")
        if len(matches) > 1:
            raise DocumentManifestError(field, "此表名无法唯一定位，请使用 来源ID.模式.表名。")
        if matches[0] in result:
            raise DocumentManifestError(field, "此文档已关联同一张表。")
        result.append(matches[0])
    return result


def _bound_content(entry, tables, name, raw, catalog):
    try:
        text = raw.decode("utf-8-sig")
        original = load_document(name, text=text)
        scope = original.scope.model_dump(mode="json")
        # Table selection replaces any previous source selector. Column and section restrictions remain.
        scope.update(source_ids=[], tables=tables)
        updated = _bound_markdown(name, text, entry.purpose, scope)
        if scope["columns"] or _frontmatter(text)[0].get("sections"):
            document = load_document(name, text=updated.decode())
            DocumentCorpus([document]).bind_catalog(catalog())
    except UnicodeError:
        raise DocumentManifestError(entry.field + ".path", "Markdown 文件必须使用 UTF-8 编码。") from None
    except LightGraphError as exc:
        if exc.code == "document_too_large":
            raise LightGraphError("upload_too_large") from None
        raise DocumentManifestError(entry.field, f"请核对 Markdown 文档及其列和章节范围（{exc.code}）。") from None
    return updated, scope


def import_manifest(workspace, pid, entries, uploads):
    """Caller holds the project lock. Validate all bindings before replacing any registered file."""
    # A small manifest may rebind many MB of documents already in the project. Keep rollback
    # bytes and prepared replacements out of memory once the size of one document is exceeded.
    with SpooledTemporaryFile(max_size=MAX_TEXT_BYTES) as journal:
        return _import_manifest(workspace, pid, entries, uploads, journal)


def _import_manifest(workspace, pid, entries, uploads, journal):
    from .document_tables import project_tables, table_lookup
    from .deletion import _cached

    project = workspace.get(pid)
    previous_project = deepcopy(project)
    config = workspace.get_config(pid)
    previous_config = deepcopy(config)
    by_path = _existing(entries, project)
    expected = {entry.path for entry in entries}
    if set(uploads) - expected:
        raise DocumentManifestError("documents", "上传的附件包含清单以外的路径。")
    keys = project_tables(workspace, pid, config)
    lookup = table_lookup(keys)

    @cache
    def catalog():
        declared, trusted = _cached(workspace, pid)
        cached = {(t.source_id, t.schema_name, t.name): t for t in declared.tables if t.source_id in trusted} if declared else {}
        return [cached.get(key) or TableSpec(source_id=key[0], schema=key[1], name=key[2], columns=[])
                for key in keys]

    records = []
    writes = {}
    for entry in entries:
        tables = _resolve_tables(entry, lookup)
        record = by_path.get(entry.path)
        name = PurePosixPath(entry.path).name
        old = None
        if record:
            path = workspace._path(pid, record["relative_path"])
            if not path.is_file():
                raise DocumentManifestError(entry.field + ".path", "已登记的项目文件缺失，请检查项目文件目录。")
            with path.open("rb") as stream:
                old = stream.read(MAX_TEXT_BYTES + 1)
            if len(old) > MAX_TEXT_BYTES:
                raise LightGraphError("upload_too_large")
        elif entry.path not in uploads:
            raise DocumentManifestError(entry.field + ".path", "未找到此路径的项目文档，请一并选择对应的本地文件。")
        else:
            fid = "f_" + uuid4().hex[:16]
            record = FileRecord(file_id=fid, name=name, kind="markdown", size=0,
                                relative_path=f"files/{fid}_{name}").model_dump(mode="json")
            path = workspace._path(pid, record["relative_path"])
            record["manifest_path"] = entry.path
            project["files"].append(record)
        # A manifest can change bindings on existing documents, not replace their Markdown bodies.
        updated, scope = _bound_content(entry, tables, path, old if old is not None else uploads[entry.path], catalog)
        if old is not None and entry.path in uploads:
            submitted, _ = _bound_content(entry, tables, path, uploads[entry.path], catalog)
            if updated != submitted:
                raise DocumentManifestError(entry.field + ".path", "此路径已登记且正文不同，请核对文件或只导入清单以更新关联。")
        if updated != old:
            offset = journal.tell()
            if old is not None:
                journal.write(old)
            writes[path] = (offset, len(old) if old is not None else None, journal.tell(), len(updated))
            journal.write(updated)
        record.update(size=len(updated), purpose=entry.purpose, scope=scope, binding_origin="explicit",
                      inactive_reason=None, inactive_targets=[])
        if record["relative_path"] not in config["documents"]:
            config["documents"].append(record["relative_path"])
        records.append(record)
    if writes or project != previous_project or config != previous_config:
        applied = []
        config_started = False
        try:
            for path, (_, _, offset, size) in writes.items():
                journal.seek(offset)
                workspace._atomic(path, journal.read(size))
                applied.append(path)
            clean = workspace._normalize_config(pid, config, project)
            config_started = True
            workspace._write_config(pid, clean)
            workspace._save(project)
        except Exception:
            for path in reversed(applied):
                offset, size, _, _ = writes[path]
                if size is None:
                    path.unlink(missing_ok=True)
                else:
                    journal.seek(offset)
                    workspace._atomic(path, journal.read(size))
            if config_started:
                workspace._write_config(pid, previous_config)
                workspace._save(previous_project)
            raise
    return {"files": records, "imported": len(records)}

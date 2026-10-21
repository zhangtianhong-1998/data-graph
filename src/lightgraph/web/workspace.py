"""Project-scoped uploads and configuration; database query rows never enter this store.

The caller supplies the workspace directory, normally ``repository / 'workspace'``.
Locks coordinate one service process. They are intentionally not advertised as a
distributed lock or an authorization boundary between users of a shared server.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import Field

from ..contracts import StrictModel
from ..documents.core import DocumentScope, load_document
from ..errors import LightGraphError
from ..runtime.config import AgentConfig, RunConfig


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_KINDS = {".xlsx": "excel", ".csv": "csv", ".md": "markdown",
          ".markdown": "markdown", ".yaml": "yaml", ".yml": "yaml"}
_PURPOSES = {"data_description", "analysis_knowledge", "mixed", "unresolved"}


class FileRecord(StrictModel):
    file_id: str
    name: str
    kind: Literal["excel", "csv", "markdown", "yaml"]
    relative_path: str
    size: int = Field(ge=0, le=MAX_UPLOAD_BYTES)
    purpose: str = "unresolved"
    scope: DocumentScope = Field(default_factory=DocumentScope)


class _Project(StrictModel):
    project_id: str
    name: str
    created_at: str
    updated_at: str
    files: list[FileRecord] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LightGraphError("invalid_project_or_file_id")
    return value


def _filename(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 180 or
            value in {".", ".."} or value != value.strip() or
            "/" in value or "\\" in value or ":" in value or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise LightGraphError("invalid_upload_filename")
    try:
        if len(value.encode("utf-8")) > 200:
            raise LightGraphError("invalid_upload_filename")
    except UnicodeError:
        raise LightGraphError("invalid_upload_filename") from None
    return value


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise LightGraphError("invalid_project_name")
    return value.strip()


def _no_symlinks(path: Path) -> None:
    """Reject symlinks before resolve/open, including parent directory links."""
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise LightGraphError("workspace_symlink_forbidden")


def safe_yaml(text: str):
    """No aliases, anchors, custom tags, duplicate keys, or Python constructors."""
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool)) or key in result:
                raise LightGraphError("invalid_yaml_mapping")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken,
                              yaml.tokens.TagToken)) for t in yaml.scan(text)):
            raise LightGraphError("yaml_alias_or_tag_forbidden")
        return yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):
        raise LightGraphError("invalid_yaml") from None


def _no_credentials(data) -> None:
    """Inspect credential-bearing config locations, never arbitrary rule attributes."""
    if isinstance(data, list):
        for item in data:
            _no_credentials(item)
        return
    if not isinstance(data, dict):
        return

    def check(value):
        if not isinstance(value, dict):
            return
        for key in ("api_key", "password", "token", "secret", "dsn"):
            if value.get(key) not in (None, ""):
                raise LightGraphError("use_credential_environment_variable")
        if isinstance(value.get("base_url"), str) and value["base_url"]:
            try:
                parsed = urlsplit(value["base_url"])
                if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                    raise LightGraphError("credential_bearing_url_forbidden")
            except ValueError:
                raise LightGraphError("invalid_model_url") from None
        for key in ("llm", "model", "credentials", "connection"):
            check(value.get(key))

    if set(data) & {"llm", "model", "sources", "base_url", "api_key_env", "api_key"}:
        check(data)
    for source in data.get("sources", []) if isinstance(data.get("sources", []), list) else []:
        check(source)


def _frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, min(len(lines), 129)) if lines[i].strip() == "---"), None)
    if end is None:
        raise LightGraphError("invalid_document_front_matter")
    meta = safe_yaml("".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        raise LightGraphError("invalid_document_front_matter")
    return meta, "".join(lines[end + 1:])


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        _no_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise LightGraphError("invalid_workspace_root")
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def lock(self, project_id: str):
        project_id = _identifier(project_id)
        with self._locks_guard:
            lock = self._locks.setdefault(project_id, threading.RLock())
        with lock:
            yield

    def project_path(self, project_id: str) -> Path:
        path = self.root / _identifier(project_id)
        _no_symlinks(path)
        if not path.is_dir() or not (path / "project.yaml").is_file():
            raise LightGraphError("project_not_found")
        _no_symlinks(path / "project.yaml")
        return path

    def _path(self, project_id: str, relative: str) -> Path:
        if not isinstance(relative, str) or "\\" in relative:
            raise LightGraphError("workspace_path_forbidden")
        rel = PurePosixPath(relative)
        if rel.is_absolute() or not rel.parts or any(p in {".", ".."} for p in rel.parts):
            raise LightGraphError("workspace_path_forbidden")
        path = self.project_path(project_id).joinpath(*rel.parts)
        _no_symlinks(path)
        return path

    def _read(self, path: Path):
        _no_symlinks(path)
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                raise LightGraphError("workspace_metadata_too_large")
            return safe_yaml(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            raise LightGraphError("workspace_file_read_failed") from None

    def _atomic(self, path: Path, data: bytes) -> None:
        _no_symlinks(path)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".write_", dir=path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _no_symlinks(path)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _write_yaml(self, path: Path, value) -> None:
        text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise LightGraphError("workspace_metadata_too_large")
        self._atomic(path, text.encode())

    def _load(self, project_id: str) -> dict:
        path = self.project_path(project_id)
        try:
            project = _Project.model_validate(self._read(path / "project.yaml")).model_dump(mode="json")
        except LightGraphError:
            raise
        except Exception:
            raise LightGraphError("invalid_project_metadata") from None
        if project["project_id"] != project_id:
            raise LightGraphError("invalid_project_metadata")
        ids = set()
        for file in project["files"]:
            fid = _identifier(file["file_id"])
            filename = _filename(file["name"])
            if fid in ids or file["relative_path"] != f"files/{fid}_{filename}":
                raise LightGraphError("invalid_project_metadata")
            ids.add(fid)
            self._path(project_id, file["relative_path"])
        for folder in ("files", "output", "runs"):
            directory = self._path(project_id, folder)
            directory.mkdir(exist_ok=True)
        return project

    def _save(self, project: dict) -> dict:
        project["updated_at"] = _now()
        self._write_yaml(self.project_path(project["project_id"]) / "project.yaml", project)
        return deepcopy(project)

    def create(self, name: str, project_id: str | None = None) -> dict:
        name = _name(name)
        project_id = _identifier(project_id if project_id is not None else "p_" + uuid4().hex[:16])
        with self.lock(project_id):
            _no_symlinks(self.root)
            path = self.root / project_id
            _no_symlinks(path)
            try:
                path.mkdir()
            except FileExistsError:
                raise LightGraphError("project_already_exists") from None
            stamp = _now()
            project = _Project(project_id=project_id, name=name, created_at=stamp,
                               updated_at=stamp).model_dump(mode="json")
            self._write_yaml(path / "project.yaml", project)
            for folder in ("files", "output", "runs"):
                (path / folder).mkdir()
            config = RunConfig(sources=[], isolation_domain=project_id, output_dir="output",
                               goal={"purpose": ""}).model_dump(mode="json")
            self._write_yaml(path / "config.yaml", config)
            return project

    def list_projects(self) -> list[dict]:
        _no_symlinks(self.root)
        projects = []
        for path in sorted(self.root.iterdir()):
            if path.is_symlink():
                raise LightGraphError("workspace_symlink_forbidden")
            if path.is_dir() and _ID.fullmatch(path.name) and (path / "project.yaml").exists():
                projects.append(self.get(path.name))
        return sorted(projects, key=lambda p: p["updated_at"], reverse=True)

    def get(self, project_id: str) -> dict:
        with self.lock(project_id):
            return self._load(project_id)

    def rename(self, project_id: str, name: str) -> dict:
        with self.lock(project_id):
            project = self._load(project_id)
            project["name"] = _name(name)
            return self._save(project)

    def add_file(self, project_id: str, name: str, content: bytes) -> dict:
        name = _filename(name)
        kind = _KINDS.get(Path(name).suffix.lower())
        if kind is None:
            raise LightGraphError("unsupported_upload_type")
        if not isinstance(content, bytes):
            raise LightGraphError("upload_bytes_required")
        limit = MAX_TEXT_BYTES if kind in {"yaml", "markdown"} else MAX_UPLOAD_BYTES
        if len(content) > limit:
            raise LightGraphError("upload_too_large")
        meta = {}
        if kind in {"yaml", "markdown"}:
            try:
                text = content.decode("utf-8")
            except UnicodeError:
                raise LightGraphError("upload_utf8_required") from None
            if kind == "yaml":
                _no_credentials(safe_yaml(text))
            else:
                meta, _ = _frontmatter(text)
                load_document(name, text=text)
        with self.lock(project_id):
            project = self._load(project_id)
            config = self.get_config(project_id)
            original_config = deepcopy(config)
            fid = "f_" + uuid4().hex[:16]
            scope = meta.get("scope", {})
            if "applies_to" in meta:
                scope = {**scope, "tables": meta["applies_to"]}
            record = FileRecord(file_id=fid, name=name, kind=kind,
                                relative_path=f"files/{fid}_{name}", size=len(content),
                                purpose=meta.get("purpose", "unresolved"), scope=scope).model_dump(mode="json")
            path = self._path(project_id, record["relative_path"])
            self._atomic(path, content)
            project["files"].append(record)
            # Configuration and catalog updates share the project lock. In case
            # of validation failure no orphan upload remains registered.
            if kind in {"excel", "csv"}:
                source = {"id": "s_" + fid[2:], "kind": "excel_workbook" if kind == "excel" else "csv_file",
                          "path": record["relative_path"]}
                if kind == "csv":
                    source.update(encoding="utf-8", delimiter=",", header=True,
                                  null_values=[""], type_inference="text", table_name=Path(name).stem)
                config["sources"].append(source)
            elif kind == "markdown":
                config["documents"].append(record["relative_path"])
            try:
                clean = self._normalize_config(project_id, config, project)
                self._write_yaml(self._path(project_id, "config.yaml"), clean)
                self._save(project)
            except Exception:
                path.unlink(missing_ok=True)
                # Restore the previous valid config if registration failed after
                # its atomic file replacement; never leave a missing source path.
                self._write_yaml(self._path(project_id, "config.yaml"), original_config)
                raise
            return record

    def file_path(self, project_id: str, file_id: str) -> Path:
        _identifier(file_id)
        with self.lock(project_id):
            project = self._load(project_id)
            file = next((f for f in project["files"] if f["file_id"] == file_id), None)
            if file is None:
                raise LightGraphError("project_file_not_found")
            path = self._path(project_id, file["relative_path"])
            if not path.is_file():
                raise LightGraphError("project_file_not_found")
            return path

    def text_content(self, project_id: str, file_id: str) -> str:
        """Revalidate editable YAML on every preview, including manual disk edits.

        Upload-time checks alone are insufficient: a user may edit the project
        directory while the service remains running. Refuse credential-bearing
        configuration rather than returning a partly redacted editable document.
        """
        with self.lock(project_id):
            path = self.file_path(project_id, file_id)
            if path.suffix.lower() not in {".md", ".markdown", ".yaml", ".yml"}:
                raise LightGraphError("text_preview_unavailable")
            try:
                if path.stat().st_size > MAX_TEXT_BYTES:
                    raise LightGraphError("file_too_large")
                # The bounded binary read also covers a concurrent manual grow.
                with path.open("rb") as stream:
                    content = stream.read(MAX_TEXT_BYTES + 1)
                if len(content) > MAX_TEXT_BYTES:
                    raise LightGraphError("file_too_large")
                text = content.decode("utf-8")
            except UnicodeError:
                raise LightGraphError("text_encoding_invalid") from None
            except OSError:
                raise LightGraphError("workspace_file_read_failed") from None
            if path.suffix.lower() in {".yaml", ".yml"}:
                _no_credentials(safe_yaml(text))
            return text

    def document_binding(self, project_id: str, file_id: str, purpose: str, scope: dict) -> dict:
        if purpose not in _PURPOSES:
            raise LightGraphError("invalid_document_purpose")
        try:
            binding = DocumentScope.model_validate(scope).model_dump(mode="json")
        except Exception:
            raise LightGraphError("invalid_document_scope") from None
        with self.lock(project_id):
            project = self._load(project_id)
            file = next((f for f in project["files"] if f["file_id"] == file_id), None)
            if not file or file["kind"] != "markdown":
                raise LightGraphError("markdown_file_required")
            path = self.file_path(project_id, file_id)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise LightGraphError("workspace_file_read_failed") from None
            meta, body = _frontmatter(text)
            meta.pop("applies_to", None)
            meta.update(purpose=purpose, scope=binding)
            updated = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body
            # Validate the full section-scope inheritance before changing the file.
            load_document(path, text=updated)
            if len(updated.encode()) > MAX_TEXT_BYTES:
                raise LightGraphError("upload_too_large")
            self._atomic(path, updated.encode())
            file.update(purpose=purpose, scope=binding, size=len(updated.encode()))
            self._save(project)
            return deepcopy(file)

    def _normalize_config(self, project_id: str, value: dict, project: dict | None = None) -> dict:
        if not isinstance(value, dict):
            raise LightGraphError("invalid_project_config")
        _no_credentials(value)
        data = deepcopy(value)
        project = project if project is not None else self._load(project_id)
        data["output_dir"] = "output"
        data["isolation_domain"] = project_id

        def registered(raw, allowed):
            if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
                raise LightGraphError("config_path_must_be_project_file")
            rel = PurePosixPath(raw)
            if rel.is_absolute() or ".." in rel.parts or raw.startswith("./"):
                raise LightGraphError("config_path_must_be_project_file")
            matches = [f for f in project["files"] if raw in {f["relative_path"], f["name"]}]
            if len(matches) != 1 or matches[0]["kind"] not in allowed:
                raise LightGraphError("config_path_must_be_unambiguous_project_file")
            path = self._path(project_id, matches[0]["relative_path"])
            if not path.is_file():
                raise LightGraphError("project_file_not_found")
            return matches[0]["relative_path"]

        try:
            for source in data.get("sources", []):
                if source.get("path"):
                    allowed = {"excel"} if source.get("kind") == "excel_workbook" else {"csv"}
                    if source.get("kind") in {"gauss_database", "mysql_database"}:
                        raise LightGraphError("database_source_cannot_have_file_path")
                    source["path"] = registered(source["path"], allowed)
                if source.get("capability_manifest"):
                    source["capability_manifest"] = registered(source["capability_manifest"], {"yaml"})
            for field, kind in (("documents", "markdown"), ("rules", "yaml")):
                data[field] = [registered(raw, {kind}) for raw in data.get(field, [])]
            if data.get("agent_config"):
                data["agent_config"] = registered(data["agent_config"], {"yaml"})
                agent = self._read(self._path(project_id, data["agent_config"]))
                _no_credentials(agent)
                AgentConfig.model_validate(agent)
            return RunConfig.model_validate(data).model_dump(mode="json")
        except LightGraphError:
            raise
        except Exception:
            raise LightGraphError("invalid_project_config") from None

    def get_config(self, project_id: str) -> dict:
        with self.lock(project_id):
            return self._normalize_config(project_id, self._read(self._path(project_id, "config.yaml")))

    def save_config(self, project_id: str, value: dict) -> dict:
        with self.lock(project_id):
            project = self._load(project_id)
            clean = self._normalize_config(project_id, value, project)
            self._write_yaml(self._path(project_id, "config.yaml"), clean)
            self._save(project)
            return clean

    def resolved_config(self, project_id: str) -> RunConfig:
        with self.lock(project_id):
            data = self.get_config(project_id)
            try:
                return RunConfig.model_validate(data).resolve_paths(self.project_path(project_id))
            except Exception:
                raise LightGraphError("invalid_project_config") from None

    def import_config(self, project_id: str, file_id: str) -> dict:
        with self.lock(project_id):
            project = self._load(project_id)
            file = next((f for f in project["files"] if f["file_id"] == file_id), None)
            if not file or file["kind"] != "yaml":
                raise LightGraphError("yaml_file_required")
            return self.save_config(project_id, self._read(self.file_path(project_id, file_id)))

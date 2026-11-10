"""Recoverable model views of tool results, without repeating source queries.

Only the already-authorized JSON result is retained, never call arguments,
configuration or connection credentials. Files survive worker shutdown for
operator inspection; references are capabilities of the issuing tool session.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid

from lightgraph.discovery.token_estimation import (
    TOKEN_ESTIMATE_METHOD, estimate_tokens, prompt_token_limit,
)
from lightgraph.errors import LightGraphError
from .paths import io_path, resolve_path


FORMAT_VERSION = 1
DEFAULT_RESULT_TOKENS = 4096
_REF = re.compile(r"result:([0-9a-f]{64})\Z")
# These small control values must survive projection, including a failed
# operation's status. Nested coverage uses the same ordering before previews.
_CONTROL = frozenset({"status", "code", "cause_code", "passed", "complete",
    "result_complete", "truncated", "retryable", "queries_stopped",
    "finalization_model_calls", "source_blocked", "new_snapshot_recovery_available",
    "evidence_ref", "validation_id", "draft_id", "revision", "state",
    "validation_material", "draft_material", "failure_class", "recovery"})
_PRIORITY = _CONTROL | {"query_budget", "coverage", "errors", "diagnostics",
    "evidence_refs", "document_refs", "next_action", "source_circuits",
    "diagnostic_count", "diagnostic_codes", "diagnostics_truncated"}


def serialize(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def token_size(value) -> int:
    """Include escaped content and FastMCP's pretty-printed JSON whitespace."""
    return estimate_tokens(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def result_token_limit(model) -> int:
    limits = [DEFAULT_RESULT_TOKENS]
    for limit in (prompt_token_limit(model), getattr(model, "working_context_tokens", None)):
        if limit is not None:
            limits.append(max(0, limit // 8))
    return min(limits)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pointer(value, section):
    if not isinstance(section, str) or (section and not section.startswith("/")):
        raise LightGraphError("invalid_result_section")
    if not section:
        return value
    for token in section[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise LightGraphError("invalid_result_section")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(value):
            value = value[int(token)]
        else:
            raise LightGraphError("unknown_result_section")
    return value


def _preview(value, width, text_limit, depth=0):
    if isinstance(value, str) and len(value) > text_limit:
        return {"text_preview": value[:text_limit], "characters": len(value), "presentation_truncated": True}
    if isinstance(value, list):
        return [_preview(item, width, text_limit, depth + 1) for item in value[:width]] if depth < 5 else []
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: (key not in _PRIORITY, key))
        selected = [key for key in keys if key in _CONTROL]
        selected += [key for key in keys if key not in selected][:width if depth < 5 else 0]
        return {key: (value[key] if key in _CONTROL and not isinstance(value[key], (dict, list))
                     and (not isinstance(value[key], str) or len(value[key]) <= 256)
                     else _preview(value[key], width, text_limit, depth + 1))
                for key in selected}
    return value


def _source_complete(result):
    coverage = result.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    if result.get("truncated") is True or coverage.get("truncated") is True:
        return False
    for candidate in (result.get("complete"), coverage.get("result_complete"), coverage.get("complete")):
        if isinstance(candidate, bool):
            return candidate
    return None


class ResultMaterialStore:
    """One session's immutable results; file names and pointers never accept paths."""

    def __init__(self, workspace, context, access, token_limit=DEFAULT_RESULT_TOKENS):
        self.root = resolve_path(workspace) / "result-materials" / uuid.uuid4().hex
        self.context, self.access = context, access
        self.token_limit = token_limit
        self._issued = set()
        self._directory_fd = None
        self._directory_ready = False
        self._closed = False
        self._root_identity = None
        self._use_dir_fd = (all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW"))
            and all(operation in os.supports_dir_fd for operation in (os.open, os.mkdir, os.unlink)))
        self._page_boundaries = {}

    def close(self):
        # Retention is an operator decision, not a side effect of closing tools.
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None
        self._issued.clear()
        self._page_boundaries.clear()
        self._directory_ready = False
        self._closed = True

    def _directory(self):
        if self._closed:
            raise OSError("result_material_session_closed")
        if not self._directory_ready:
            base = self.root.parent
            io_path(base).mkdir(mode=0o700, exist_ok=True)
            self._regular_path(base, directory=True)
            if self._use_dir_fd:
                base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.mkdir(self.root.name, mode=0o700, dir_fd=base_fd)
                    self._directory_fd = os.open(self.root.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
                finally:
                    os.close(base_fd)
            else:
                io_path(self.root).mkdir(mode=0o700)
            info = self._regular_path(self.root, directory=True)
            self._root_identity = (info.st_dev, info.st_ino)
            self._directory_ready = True
        return self._directory_fd

    @staticmethod
    def _regular_path(path, *, directory=False):
        info = io_path(path).lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise OSError("result_material_unsafe_path")
        return info

    def _file(self, identifier, *, write=False):
        """POSIX descriptor confinement, or checked Windows long-path IO."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if write else os.O_RDONLY
        if self._use_dir_fd:
            return os.open(identifier + ".json", flags | os.O_NOFOLLOW,
                           0o600, dir_fd=self._directory_fd)
        self._regular_path(self.root.parent, directory=True)
        root = self._regular_path(self.root, directory=True)
        if (root.st_dev, root.st_ino) != self._root_identity:
            raise OSError("result_material_directory_changed")
        path = io_path(self.root / (identifier + ".json"))
        before = None if write else self._regular_path(path)
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
        after = os.fstat(fd)
        if before and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(fd)
            raise OSError("result_material_file_changed")
        return fd

    def _save(self, result, tool):
        content = serialize(result)
        revision = _hash(content)
        identifier = _hash(serialize([self.root.name, self.context, self.access, tool, revision]))
        if identifier not in self._issued:
            payload = serialize({"format_version": FORMAT_VERSION, "session": self.root.name,
                "context": self.context, "access": self.access, "tool": tool,
                "material_revision": revision, "result": result})
            self._directory()
            fd = self._file(identifier, write=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                if self._use_dir_fd:
                    os.unlink(identifier + ".json", dir_fd=self._directory_fd)
                else:
                    io_path(self.root / (identifier + ".json")).unlink(missing_ok=True)
                raise
            self._issued.add(identifier)
        return "result:" + identifier, revision

    def _load(self, name, access):
        match = _REF.fullmatch(name) if isinstance(name, str) else None
        if not match or match[1] not in self._issued or not self._directory_ready:
            raise LightGraphError("unknown_result_material")
        if access != self.access:
            raise LightGraphError("result_material_access_changed")
        try:
            fd = self._file(match[1])
            with os.fdopen(fd, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError):
            raise LightGraphError("result_material_unavailable") from None
        if (not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION
                or payload.get("session") != self.root.name or payload.get("context") != self.context
                or payload.get("access") != self.access
                or payload.get("material_revision") != _hash(serialize(payload.get("result")))
                or match[1] != _hash(serialize([self.root.name, self.context, self.access,
                    payload.get("tool"), payload.get("material_revision")]))):
            raise LightGraphError("result_material_integrity_failed")
        return payload

    def bound(self, result, tool):
        if token_size(result) <= self.token_limit:
            return result
        try:
            ref, revision = self._save(result, tool)
            stored = {"result_ref": ref, "material_revision": revision}
        except (OSError, ValueError, TypeError):
            # A save/publication already succeeded. Never report it as an
            # unexecuted tool failure or invite an automatic duplicate mutation.
            stored = {"presentation_error": "result_material_write_failed"}
        meta = {"format_version": FORMAT_VERSION, "summary_only": True,
            "presentation_truncated": True, "presentation_complete": False,
            "source_complete": _source_complete(result),
            "stored_complete_tool_result": "result_ref" in stored,
            "original_estimated_tokens": token_size(result),
            "estimated_token_limit": self.token_limit, "estimate_method": TOKEN_ESTIMATE_METHOD,
            "retrieval": "Read result_ref with read_material; select section by JSON pointer (for example /data/rows/0). Read only needed sections. Stored results do not query the source. Issued evidence and source completeness do not mean omitted content has been read by the model."}
        if "result_ref" not in stored:
            meta["retrieval"] = ("The full tool result could not be retained. The operation status above is unchanged. "
                "Use existing draft/validation materials if available; do not repeat a mutation because its presentation failed.")
        sections = [{"section": "/" + key.replace("~", "~0").replace("/", "~1"),
                     "type": "array" if isinstance(value, list) else "object", "items": len(value)}
                    for key, value in result.items() if isinstance(value, (dict, list))]
        for width, text_limit in ((8, 512), (4, 256), (2, 128), (1, 64), (0, 0)):
            view = _preview(result, width, text_limit)
            if isinstance(result.get("coverage"), dict):
                view["coverage"] = _preview(result["coverage"], width, text_limit)
            # The current global budget is small and is not a data preview.
            if "query_budget" in result:
                view["query_budget"] = result["query_budget"]
            view.update(stored, result_presentation={**meta, "sections": sections[:width]})
            if token_size(view) <= self.token_limit:
                return view
        # No invented minimum: explicitly expose an impossible configured
        # budget, retaining operation status/control fields and recovery ref.
        view["result_presentation"]["presentation_error"] = "result_budget_too_small"
        view["result_presentation"]["minimum_estimated_tokens"] = token_size(view)
        return view

    def read(self, name, page, section, *, access, query_budget):
        """Page exact JSON text for a selected subtree, even one oversized cell.

        Offsets refer to JSON characters, not rows. Concatenation under one
        material_revision and section_digest reconstructs the selected value.
        """
        if type(page) is not int or page < 0:
            raise LightGraphError("invalid_material_page")
        payload = self._load(name, access)
        selected = _pointer(payload["result"], section)
        content = serialize(selected)
        base = {"status": "ok", "format": "json", "format_version": FORMAT_VERSION,
            "material": name, "result_ref": name, "material_revision": payload["material_revision"],
            "section": section, "section_digest": _hash(content), "characters": len(content),
            "requires_database": False, "query_budget": query_budget,
            "source_status": payload["result"].get("status"),
            "source_complete": _source_complete(payload["result"]),
            "estimate_method": TOKEN_ESTIMATE_METHOD, "estimated_token_limit": self.token_limit,
            "next_action": "Concatenate content only for matching material_revision, section and section_digest. Fragments are not new evidence; read only the sections needed."}
        for key in ("code", "evidence_ref"):
            if key in payload["result"]:
                base["source_" + key] = payload["result"][key]
        # Reserve worst-case decimal widths so every page including its wrapper
        # fits. Replacing these values with actual indexes can only reduce cost.
        worst = {**base, "page": len(content), "pages": len(content) + 1,
            "next_page": len(content), "character_start": len(content),
            "character_end": len(content), "content_is_fragment": True,
            "presentation_truncated": True, "content": ""}
        # Remaining seconds change on every call. Use a stable conservative
        # budget envelope so character boundaries never drift across pages.
        worst["query_budget"] = {key: 10 ** max(24, len(str(value))) for key, value in query_budget.items()}
        if token_size(worst) >= self.token_limit:
            return {**base, "presentation_error": "result_budget_too_small", "content": "",
                "next_page": None, "content_is_fragment": True, "presentation_truncated": True,
                "minimum_estimated_tokens": token_size(worst)}
        # A fixed character count is not a token boundary: binary search the
        # serialized wrapper for each fragment, including escaped quotes.
        cache_key = (name, section, self.token_limit)
        boundaries = self._page_boundaries.get(cache_key, [0])
        while boundaries[-1] < len(content):
            start = boundaries[-1]
            low, high = start + 1, min(len(content), start + self.token_limit * 4)
            while low <= high:
                middle = (low + high) // 2
                if token_size({**worst, "content": content[start:middle]}) <= self.token_limit:
                    low = middle + 1
                else:
                    high = middle - 1
            if high == start:
                raise LightGraphError("result_budget_too_small")
            boundaries.append(high)
        self._page_boundaries[cache_key] = boundaries
        pages = len(boundaries) - 1
        if page >= pages:
            raise LightGraphError("invalid_material_page")
        start, end = boundaries[page:page + 2]
        return {**base, "page": page, "pages": pages, "content": content[start:end],
            "character_start": start, "character_end": end, "content_is_fragment": pages > 1,
            "presentation_truncated": pages > 1, "next_page": page + 1 if page + 1 < pages else None}

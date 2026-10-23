"""Bounded process-memory credentials, scoped to one browser project session.

Only presence flags leave this store. Source revisions are random identifiers,
not hashes of passwords; endpoint changes cannot silently reuse a model key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time

from ..errors import LightGraphError


@dataclass(repr=False)
class _Session:
    project_id: str
    expires_at: float
    sources: dict = field(default_factory=dict)
    model: tuple | None = None


class SessionCredentials:
    def __init__(self, *, ttl_seconds=3600, max_sessions=256, clock=time.monotonic):
        self.ttl_seconds, self.max_sessions, self.clock = ttl_seconds, max_sessions, clock
        self._sessions = {}
        self._lock = threading.RLock()

    def _purge(self):
        now = self.clock()
        for token in list(self._sessions):
            if self._sessions[token].expires_at <= now:
                del self._sessions[token]

    def create(self, pid):
        with self._lock:
            self._purge()
            if len(self._sessions) >= self.max_sessions:
                raise LightGraphError("credential_session_capacity_reached")
            token = secrets.token_urlsafe(32)
            self._sessions[token] = _Session(pid, self.clock() + self.ttl_seconds)
            return token

    def _get(self, pid, token):
        self._purge()
        item = self._sessions.get(token) if isinstance(token, str) else None
        if item is None or item.project_id != pid:
            raise LightGraphError("credential_session_invalid")
        return item

    def require(self, pid, token):
        with self._lock:
            self._get(pid, token)

    @staticmethod
    def _model_binding(config):
        model = config.resolved_model
        return (model.base_url, model.model)

    def set_model(self, pid, token, config, api_key):
        with self._lock:
            item = self._get(pid, token)
            if not all(self._model_binding(config)):
                raise LightGraphError("model_endpoint_required")
            item.model = (self._model_binding(config), api_key)

    def clear_model(self, pid, token):
        with self._lock:
            self._get(pid, token).model = None

    def set_source(self, pid, token, source, dsn):
        with self._lock:
            self._get(pid, token).sources[source.id] = (source.kind, source.credential_revision, dsn)

    def clear_source(self, pid, token, source_id):
        with self._lock:
            self._get(pid, token).sources.pop(source_id, None)

    def clear_source_sessions(self, pid, source_id):
        """Forgetting a saved connection also clears its active session copies."""
        with self._lock:
            for item in self._sessions.values():
                if item.project_id == pid:
                    item.sources.pop(source_id, None)

    def resolve(self, pid, token, config):
        """Return a private copy only to trusted metadata/worker dispatch code."""
        with self._lock:
            item = self._get(pid, token)
            active = {s.id: s for s in config.sources}
            for sid in list(item.sources):
                source = active.get(sid)
                kind, revision, _ = item.sources[sid]
                if source is None or source.credential_mode != "session" or (source.kind, source.credential_revision) != (kind, revision):
                    del item.sources[sid]
            if item.model and item.model[0] != self._model_binding(config):
                item.model = None
            return ({sid: values[2] for sid, values in item.sources.items()},
                    item.model[1] if item.model else None)

    def clear(self):
        with self._lock:
            self._sessions.clear()

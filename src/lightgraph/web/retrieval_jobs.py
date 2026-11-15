"""Short HTTP requests for user-started vector builds; no implicit query rebuilds."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
from uuid import uuid4

from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text


def _now():
    return datetime.now(timezone.utc).isoformat()


class RetrievalJobs:
    """Single-service jobs persist value-free status across refreshes and restarts.

    Index publication remains atomic in sync_graph_to_retrieval. A failed build
    never replaces the previous usable index. Credentials live only in callbacks.
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vectorize')
        self._lock = threading.RLock()
        self._active = {}

    def _path(self, pid):
        path = self.workspace.project_path(pid) / 'vectorization-job.json'
        if path.is_symlink():
            raise LightGraphError('workspace_path_invalid')
        return path

    def status(self, pid):
        with self._lock:
            if pid in self._active:
                return self._active[pid][0].copy()
            path = self._path(pid)
            if not path.exists():
                return {'status': 'idle'}
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if value['status'] in ('queued', 'running'):
                    value.update(status='interrupted', error_code='retrieval_job_interrupted')
                return value
            except (OSError, ValueError, KeyError, TypeError):
                raise LightGraphError('retrieval_job_invalid') from None

    def active(self, pid):
        with self._lock:
            return pid in self._active

    def start(self, pid, revision, work):
        with self._lock:
            if pid in self._active:
                job, previous_revision, future = self._active[pid]
                if revision != previous_revision:
                    raise LightGraphError('retrieval_job_active')
                return job.copy(), future
            if len(self._active) >= 2:
                raise LightGraphError('retrieval_jobs_busy')
            job = {'job_id': uuid4().hex, 'status': 'running', 'started_at': _now()}
            path = self._path(pid)
            atomic_text(path, json.dumps(job))
            future = self._pool.submit(self._run, pid, path, job, work)
            self._active[pid] = (job, revision, future)
            return job.copy(), future

    def _run(self, pid, path, job, work):
        final = dict(job)
        try:
            result = work()
            final['status'] = 'completed'
            return result
        except Exception as exc:
            # Never persist driver messages, request bodies or credentials.
            final.update(status='failed', error_code=exc.code if isinstance(exc, LightGraphError) else 'retrieval_build_failed')
            raise LightGraphError(final['error_code']) from None
        finally:
            final['finished_at'] = _now()
            with self._lock:
                try:
                    atomic_text(path, json.dumps(final))
                finally:
                    self._active.pop(pid, None)

    def shutdown(self):
        self._pool.shutdown(wait=True)

"""Schedule full-graph statistics after publication, independently of embeddings."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading

from lightgraph.construction.analytics_store import artifact_path, current_version, persist_analysis
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from . import construction_graph, graph_history


class AnalyticsJobs:
    def __init__(self, workspace):
        self.workspace = workspace
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='graph-analytics')
        self._lock = threading.RLock()
        self._pending = set()

    def _job_path(self, pid, key):
        directory = self.workspace.project_path(pid) / 'analytics-jobs'
        path = directory / (key + '.json')
        if directory.is_symlink() or path.is_symlink():
            raise LightGraphError('workspace_path_invalid')
        return path

    def _view(self, pid, config, version, selection):
        version = version or current_version(config)
        if version is None:
            return None
        entry = graph_history.resolve_version(self.workspace, pid, config, version)
        if entry.row['graph_kind'] != 'project_full':
            raise LightGraphError('analytics_project_graph_required')
        effective = graph_history._authorize_entry(self.workspace, entry, config)
        path = artifact_path(entry.root, effective, version, selection)
        return entry, effective, path

    def status(self, workspace, pid, *, config, version=None, selection=None, limit=20, offset=0):
        view = self._view(pid, config, version, selection)
        if not view:
            return {'status': 'no_graph', 'version_id': None}
        entry, _, path = view
        job_path = self._job_path(pid, path.stem)
        try:
            job = json.loads(job_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            job = None
        if job:
            return {**job, 'previous_result_available': path.is_file()}
        from lightgraph.construction.analytics_store import read_analysis
        result = read_analysis(entry.root, config, entry.row['version_id'], selection)
        if result is None:
            return {'version_id': entry.row['version_id'], 'status': 'not_built'}
        for key in ('nodes', 'communities'):
            result[key] = result[key][offset:offset + limit]
        return result

    def start(self, workspace, pid, *, config, version=None, selection=None, force=False):
        # Register work under the same project lock that guards deletion.
        # Always acquire the workspace lock before the job registry lock.
        with workspace.lock(pid):
            latest = workspace.result_config(pid)
            if version is not None:
                latest.output_dir = config.output_dir
            if latest != config:
                raise LightGraphError('graph_read_conflict')
            view = self._view(pid, config, version, selection)
            if not view:
                return {'status': 'no_graph', 'version_id': None}
            entry, _, path = view
            version = entry.row['version_id']
            job_path = self._job_path(pid, path.stem)
            job = {'status': 'queued', 'version_id': version, 'business_context': selection}
            with self._lock:
                if path.is_file() and not force:
                    return self.status(workspace, pid, config=config, version=version, selection=selection, limit=0)
                if job_path not in self._pending:
                    job_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_text(job_path, json.dumps(job))
                    self._pending.add(job_path)
                    try:
                        self._pool.submit(self._run, pid, path, job_path, job)
                    except RuntimeError:
                        self._pending.discard(job_path)
                        atomic_text(job_path, json.dumps({**job, 'status': 'failed', 'error_code': 'graph_analytics_failed'}))
                        raise LightGraphError('graph_analytics_failed') from None
            return job

    def _run(self, pid, path, job_path, job):
        try:
            atomic_text(job_path, json.dumps({**job, 'status': 'running'}))
            config = self.workspace.result_config(pid)
            entry, effective, expected = self._view(pid, config, job['version_id'], job['business_context'])
            if path != expected:
                raise LightGraphError('graph_read_conflict')
            graph, _ = construction_graph.load_prepared(effective, root=entry.root, head=entry.head,
                                                       authorization_root=entry.auth_root)
            persist_analysis(graph, effective, job['business_context'], path)
            job_path.unlink(missing_ok=True)
        except Exception as exc:
            code = exc.code if isinstance(exc, LightGraphError) else 'graph_analytics_failed'
            atomic_text(job_path, json.dumps({**job, 'status': 'failed', 'error_code': code}))
        finally:
            with self._lock:
                self._pending.discard(job_path)

    def published(self, pid, config):
        try:
            version = current_version(config)
            if version:
                graph_history.read_consistently(self.workspace, pid, self.start, version=version)
        except Exception:
            # Publication was committed before this hook. The caller may also
            # request the same version explicitly to retry scheduling.
            logging.getLogger(__name__).warning('Unable to enqueue graph analytics for %s', pid)

    def recover(self):
        for project in self.workspace.list_projects(summary=True):
            if project.get("status") == "invalid_project_metadata":
                continue
            pid = project['project_id']
            directory = self.workspace.project_path(pid) / 'analytics-jobs'
            if directory.is_symlink():
                continue
            for path in directory.glob('*.json'):
                if path.is_symlink():
                    continue
                try:
                    job = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if not isinstance(job, dict) or job.get('status') not in {'queued', 'running'}:
                    continue
                try:
                    config = self.workspace.result_config(pid)
                    view = self._view(pid, config, job['version_id'], job['business_context'])
                    if view and view[-1].stem == path.stem:
                        self.start(self.workspace, pid, config=config, version=job['version_id'],
                                   selection=job['business_context'], force=True)
                        continue
                    reason = 'graph_read_conflict'
                except (LightGraphError, OSError, ValueError, KeyError, TypeError) as exc:
                    reason = exc.code if isinstance(exc, LightGraphError) else 'graph_analytics_failed'
                try:
                    atomic_text(path, json.dumps({**job, 'status': 'failed', 'error_code': reason}))
                except OSError:
                    logging.getLogger(__name__).warning('Could not settle interrupted graph analytics for %s', pid)

    def active(self, pid):
        directory = self.workspace.project_path(pid) / 'analytics-jobs'
        with self._lock:
            return any(path.parent == directory for path in self._pending)

    def shutdown(self):
        self._pool.shutdown(wait=True)

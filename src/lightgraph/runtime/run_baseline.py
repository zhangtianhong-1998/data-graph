"""Read-only, credential-free evidence for reproducing a local graph run."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .config import load_config
from .source_identity import environment_source_bindings


class _Files:
    def __init__(self):
        self.stamps = {}
        self.trees = {}
        self.absent = set()

    @staticmethod
    def stamp(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def read(self, path):
        import hashlib
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise LightGraphError('baseline_file_unavailable')
        before = self.stamp(path)
        value = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                value.update(block)
        if before != self.stamp(path):
            raise LightGraphError('baseline_input_changed')
        self.stamps[path] = before
        return {'sha256': value.hexdigest(), 'bytes': before[2]}

    def verify(self):
        if any(path.is_symlink() or self.stamp(path) != before for path, before in self.stamps.items()):
            raise LightGraphError('baseline_input_changed')
        if any(path.exists() or path.is_symlink() for path in self.absent):
            raise LightGraphError('baseline_input_changed')
        if any(tuple(_tree_paths(root, source=source)) != paths for (root, source), paths in self.trees.items()):
            raise LightGraphError('baseline_input_changed')


def _tree_paths(root, *, source=False):
    if not root.is_dir():
        return
    excluded = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache'}
    if source:
        excluded |= {'dist', 'build', '.next', 'coverage'}
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if name not in excluded and not name.startswith('.')
                           and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if name.startswith('.') or path.is_symlink() or path.suffix in {'.pyc', '.pyo'}:
                continue
            if source and path.suffix.lower() not in {
                    '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.json', '.yaml', '.yml',
                    '.md', '.toml', '.lock', '.txt', '.html', '.css', '.c', '.h', '.sql'}:
                continue
            yield path


def _tree(root, files, *, source=False):
    paths = tuple(_tree_paths(root, source=source))
    files.trees[(root, source)] = paths
    return {path.relative_to(root).as_posix(): files.read(path) for path in paths}


def _local_command(arguments, cwd):
    try:
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _source(repo, files):
    manifest = {}
    for name in ('src/lightgraph', 'src/workbench/src', 'scripts', 'vendor/deepseek-harness'):
        manifest.update({name + '/' + path: value for path, value in _tree(repo / name, files, source=True).items()})
    for name in ('pyproject.toml', 'requirements.lock', 'requirements-test.lock',
                 'src/workbench/package.json', 'src/workbench/package-lock.json',
                 'src/workbench/vite.config.ts', 'src/workbench/tsconfig.json'):
        path = repo / name
        if path.is_file():
            manifest[name] = files.read(path)
    return {'root': str(repo), 'git_commit': _local_command(['git', 'rev-parse', 'HEAD'], repo),
            'working_tree_sha256': digest(manifest), 'files': manifest}


def _observer(files):
    modules = {}
    for name in ('lightgraph', 'lightgraph.web.app', 'lightgraph.web.execution',
                 'lightgraph.construction.runner', 'lightgraph.construction.executor'):
        loaded = sys.modules.get(name)
        spec = getattr(loaded, '__spec__', None) if loaded else importlib.util.find_spec(name)
        origin = getattr(spec, 'origin', None)
        if origin and Path(origin).is_file():
            modules[name] = {'path': str(Path(origin).resolve()), 'loaded_in_observer': loaded is not None,
                             'file_at_capture': files.read(origin)}
    return {'pid': os.getpid(), 'python': sys.version, 'executable': sys.executable,
            'modules': modules,
            'scope': 'calling_process_only', 'remote_service_verified': False}


def _owned(root, relative):
    if not isinstance(relative, str):
        raise LightGraphError('baseline_artifact_invalid')
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise LightGraphError('baseline_artifact_invalid')
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise LightGraphError('baseline_artifact_invalid')
    return path


def _artifacts(config, files):
    from lightgraph.construction.workspace import read_internal_yaml
    from lightgraph.construction.checkpoints import verify_checkpoint_files
    output = Path(config.output_dir)
    root = output / 'construction'
    result = {'output_dir': str(output), 'publication': None, 'checkpoints': []}
    manifest_path = root / 'manifest.yaml'
    if manifest_path.exists():
        manifest_file = files.read(manifest_path)
        manifest = read_internal_yaml(manifest_path)
        head = manifest.get('current_graph')
        publication = {'manifest': manifest_file, 'head': None}
        if head:
            publication['head'] = {name: head[name] for name in (
                'version_id', 'graph_digest', 'publication_digest', 'path', 'access_fingerprint',
                'result_policy_id', 'run_id', 'graph_kind') if name in head}
            graph = _owned(root, head['path'])
            publication['files'] = (_tree(graph, files) if graph.is_dir() else {graph.name: files.read(graph)})
            if review := head.get('identity_review'):
                ledger = _owned(root, review['ledger_path'])
                payload = read_internal_yaml(ledger)
                if digest(payload) != review['ledger_digest']:
                    raise LightGraphError('baseline_identity_ledger_invalid')
                publication['identity_review'] = {
                    'base_version_id': review['base_version_id'], 'ledger': files.read(ledger),
                    'ledger_digest': review['ledger_digest'], 'revision': payload.get('revision'),
                    'decision_status_counts': dict(Counter(d.get('status') for d in payload.get('decisions', [])))}
        result['publication'] = publication
    else:
        files.absent.add(manifest_path)
    checkpoint_root = output / 'checkpoints'
    if checkpoint_root.is_symlink():
        raise LightGraphError('baseline_artifact_invalid')
    if checkpoint_root.exists():
        files.trees[(checkpoint_root, False)] = tuple(_tree_paths(checkpoint_root))
        for marker in sorted(checkpoint_root.rglob('checkpoint.json')):
            marker_file = files.read(marker)
            record = json.loads(marker.read_text('utf-8'))
            graph = verify_checkpoint_files(output, record)
            result['checkpoints'].append({
                **{name: record[name] for name in ('run_id', 'node_id', 'artifact_id', 'graph_kind',
                    'graph_digest', 'evidence_digest', 'input_digest', 'access_fingerprint', 'artifact_status') if name in record},
                'marker': marker_file, 'files': _tree(graph.parent, files)})
    else:
        files.absent.add(checkpoint_root)
    return result


def capture_baseline(config_path, *, config=None, repo=None, frontend=None, run_id=None):
    """Capture hashes only; the caller may pass its exact resolved runtime config.

    This records the calling Python process, not a separately running service.
    Database snapshots require an external witness and are explicitly unverified.
    """
    files = _Files()
    config_path = Path(config_path).resolve()
    repo = Path(repo).resolve() if repo is not None else Path(__file__).resolve().parents[3]
    frontend = Path(frontend).resolve() if frontend is not None else repo / 'src/workbench/dist'
    configuration_files = {str(config_path): files.read(config_path)}
    origin = 'provided_runtime_config'
    if config is None:
        project = config_path.parent
        if config_path.name == 'config.yaml' and (project / 'project.yaml').is_file():
            from lightgraph.web.workspace import Workspace
            for name in ('project.yaml', 'output-head.yaml'):
                if (project / name).exists():
                    configuration_files[str(project / name)] = files.read(project / name)
            config = Workspace(project.parent).resolved_config(project.name)
            origin = 'workbench_config'
        else:
            config = load_config(config_path)
            origin = 'config_file'
    if config.agent_config:
        configuration_files[str(config.agent_config)] = files.read(config.agent_config)
    effective = config.model_dump(mode='json', by_alias=True)
    model = config.resolved_model.model_dump(mode='json')
    bindings = environment_source_bindings(config.sources)
    input_paths = set(config.documents + config.rules)
    for source in config.sources:
        input_paths.update(path for path in (source.path, source.capability_manifest) if path)
    for reference in config.inherited_graphs:
        if reference.snapshot_path:
            snapshot = Path(reference.snapshot_path)
            if snapshot.is_dir():
                input_paths.update(str(snapshot / name) for name in ('metadata.yaml', 'nodes.parquet', 'edges.parquet'))
            else:
                input_paths.add(str(snapshot))
    from lightgraph.construction.access import access_fingerprint
    from lightgraph.construction.snapshot_registry import result_access_policy
    installed = sorted((distribution.metadata['Name'], distribution.version)
                       for distribution in importlib.metadata.distributions() if distribution.metadata['Name'])
    frontend_files = _tree(frontend, files)
    result = {
        'format_version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
        'source': _source(repo, files), 'observer_process': _observer(files),
        'dependencies': {'python_packages': installed, 'node_version': _local_command(['node', '--version'], repo)},
        'frontend': {'path': str(frontend), 'present': frontend.is_dir(), 'files': frontend_files,
                     'sha256': digest(frontend_files)},
        'configuration': {'origin': origin,
                          'files': configuration_files, 'effective_sha256': digest(effective),
                          'effective_model_sha256': digest(model),
                          'model_origin': 'agent_config_file' if config.agent_config else 'inline_config',
                          'environment_connection_bindings': bindings,
                          'resolved_document_tables_sha256': digest(config._document_table_bindings)},
        'permissions': {'access_fingerprint': access_fingerprint(config),
                        'result_policy_sha256': digest(result_access_policy(config)),
                        'scope_selection_sha256': digest(sorted(config.scopes))},
        'inputs': {str(Path(path).resolve()): files.read(path) for path in sorted(input_paths)},
        'database_sources': [{'source_id': source.id, 'kind': source.kind, 'snapshot_verified': False}
                             for source in config.sources if source.kind.endswith('_database')],
        'artifacts': _artifacts(config, files), 'live_database_reads': 0,
    }
    result['observer_process']['repository_import_match'] = all(
        Path(item['path']).is_relative_to(repo) for item in result['observer_process']['modules'].values())
    files.verify()
    result['baseline_id'] = digest({key: value for key, value in result.items()
                                   if key not in {'captured_at', 'observer_process'}})
    return result

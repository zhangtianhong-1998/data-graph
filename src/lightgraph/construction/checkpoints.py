"""Committed, immutable workflow-node artifacts. Loading never opens a live source."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from lightgraph.runtime.locking import FileLock
from .artifact_io import artifact_step
from .graph_storage import read_graph_record, write_graph_record, _graph_digest
from .paths import io_path


class WorkflowCheckpointError(LightGraphError):
    """Safe error identifying the configured node that cannot be reused."""
    def __init__(self, code, node_id):
        super().__init__(code)
        self.node_id = node_id


def usable_result(result):
    """Partial workers may publish only a verified conflict-isolated subset."""
    if result.get('error_code') not in {None, 'run_partial'}:
        return False
    if result.get('status') in {'completed', 'completed_with_gaps'}:
        return True
    if result.get('status') != 'partial':
        return False
    issues = result.get('issue_codes')
    if issues is None:
        issues = [item.get('code') for item in (result.get('report') or {}).get('issues', [])]
    return bool(issues) and set(issues) <= {'published_definition_conflict', 'construction_conflicts_isolated'}


def require_partial_isolation(result, graph):
    if result.get('status') != 'partial':
        return
    isolation = graph.get('conflict_isolation') or {}
    removed = set(isolation.get('node_ids', []))
    if (isolation.get('policy') != 'isolate_label_conflicts_v1' or not removed
            or isolation.get('coverage') != 'incomplete' or not isolation.get('candidate_retained')
            or len(removed) != isolation.get('nodes') or any(n['id'] in removed for n in graph.get('nodes', []))
            or any(e['source'] in removed or e['target'] in removed for e in graph.get('edges', []))):
        raise LightGraphError('workflow_partial_artifact_unverified')


def _directory(output, run_id, node_id, graph_kind):
    if (not isinstance(run_id, str) or not re.fullmatch(r'run_[0-9a-f]{24}', run_id)
            or not isinstance(node_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', node_id)):
        raise LightGraphError('workflow_checkpoint_invalid')
    current = Path(output)
    if graph_kind not in {'node_delta', 'node_base', 'node_full'}:
        raise LightGraphError('workflow_checkpoint_invalid')
    parts = ['checkpoints', run_id, node_id]
    if graph_kind == 'node_base':
        parts.append('base')
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise LightGraphError('workflow_checkpoint_invalid')
    if any((current / name).is_symlink() for name in ('graph.graph', 'checkpoint.json', 'evidence.json')):
        raise LightGraphError('workflow_checkpoint_invalid')
    return current


def commit_checkpoint(output_dir, run_id, node_id, contributor, *, input_digest,
                      upstream_artifacts=(), status='completed', graph_kind='node_full', result_policy_id=None, source_attempt=None, recomposed_from=None):
    from .snapshot_registry import inherited_grants
    directory = artifact_step('checkpoint_directory', _directory, output_dir, run_id, node_id, graph_kind)
    artifact_step('checkpoint_directory', directory.mkdir, parents=True, exist_ok=True)
    graph = contributor['graph']
    evidence = contributor.get('evidence_records') or {}
    checksum = artifact_step('checkpoint_digest', _graph_digest, graph)
    evidence_checksum = artifact_step('checkpoint_digest', digest, evidence)
    record = {
        'format_version': 1, 'run_id': run_id, 'node_id': node_id, 'source_attempt': source_attempt or run_id,
        'artifact_id': 'artifact_' + digest(run_id, node_id, graph_kind, checksum, evidence_checksum)[:32],
        'relative_path': (directory.relative_to(output_dir) / 'graph.graph').as_posix(),
        'graph_digest': checksum, 'evidence_digest': evidence_checksum, 'has_evidence': bool(evidence),
        'input_digest': input_digest, 'upstream_artifacts': list(upstream_artifacts),
        'status': status, 'artifact_status': 'committed', 'graph_kind': graph_kind,
        'contributor_kind': contributor.get('kind', 'unit'),
        'access_fingerprint': graph.get('access_fingerprint'), 'result_policy_id': result_policy_id,
        'inherited_grants': artifact_step('checkpoint_grants', inherited_grants, Path(output_dir) / 'construction'),
        'coverage': (graph.get('conflict_isolation') or {}).get('coverage', 'unknown'),
        'read_window_digest': digest(graph['read_window']) if graph.get('read_window') else None,
        'source_key_semantics': graph.get('source_key_semantics', 'unknown'),
    }
    if recomposed_from is not None:
        record['recomposed_from'] = recomposed_from
    lock = FileLock(directory / '.writer.lock', busy_code='workflow_checkpoint_busy',
                    invalid_code='workflow_checkpoint_lock_invalid')
    artifact_step('checkpoint_lock', lock.acquire)
    try:
        return _commit(output_dir, directory, record, graph, evidence)
    finally:
        lock.close()


def _write_graph(directory, graph, checksum):
    directory = io_path(directory)
    destination = directory / 'graph.graph'
    if destination.exists():
        # A prior attempt installed the graph but failed before the marker.
        if _graph_digest(read_graph_record(destination)) != checksum:
            raise LightGraphError('workflow_checkpoint_immutable')
        return
    with TemporaryDirectory(prefix='.checkpoint-', dir=directory, ignore_cleanup_errors=True) as scratch:
        staged = Path(scratch) / 'graph.graph'
        write_graph_record(staged, graph)
        # Sharing violations on the destination must not re-encode Parquet.
        artifact_step('checkpoint_graph_install', staged.rename, destination)


def _commit(output_dir, directory, record, graph, evidence):
    commit = directory / 'checkpoint.json'
    if commit.exists():
        existing = artifact_step('checkpoint_marker_read', lambda: json.loads(commit.read_text(encoding='utf-8')))
        if not isinstance(existing, dict) or {k: v for k, v in existing.items() if k != 'created_at'} != record:
            raise LightGraphError('workflow_checkpoint_immutable')
        artifact_step('checkpoint_verify', load_checkpoint, output_dir, existing)
        return existing
    # Evidence is separate from Parquet metadata so a large evidence map cannot
    # hit the workspace YAML limit. The final JSON marker commits both files.
    artifact_step('checkpoint_graph_write', _write_graph, directory, graph, record['graph_digest'])
    if evidence:
        artifact_step('checkpoint_evidence_write', atomic_text, directory / 'evidence.json', json.dumps(evidence, ensure_ascii=False))
    record['created_at'] = datetime.now(timezone.utc).isoformat()
    artifact_step('checkpoint_marker_write', atomic_text, commit, json.dumps(record, ensure_ascii=False, sort_keys=True))
    return record


def recover_committed_checkpoint(output_dir, run_id, node_id):
    """Recover a commit whose process exited before updating workflow-state."""
    path = _directory(output_dir, run_id, node_id, 'node_full') / 'checkpoint.json'
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text('utf-8'))
        if record['run_id'] != run_id or record['node_id'] != node_id:
            raise ValueError
        return record
    except (ValueError, OSError, KeyError, TypeError):
        raise LightGraphError('workflow_checkpoint_invalid') from None


def verify_checkpoint_files(output_dir, record) -> Path:
    """Verify the commit marker and required file locations without decoding the graph."""
    try:
        directory = _directory(output_dir, record['run_id'], record['node_id'], record['graph_kind'])
        path = directory / 'graph.graph'
        committed = json.loads((directory / 'checkpoint.json').read_text(encoding='utf-8'))
        if (committed != record or record['artifact_status'] != 'committed'
                or record['relative_path'] != path.relative_to(output_dir).as_posix()):
            raise ValueError
        files = [path / name for name in ('metadata.yaml', 'nodes.parquet', 'edges.parquet')]
        if record['has_evidence']:
            files.append(directory / 'evidence.json')
        if (not isinstance(record['graph_digest'], str)
                or any(file.is_symlink() or not file.is_file() for file in files)):
            raise ValueError
        return path
    except (LightGraphError, KeyError, TypeError, ValueError, OSError):
        raise LightGraphError('workflow_checkpoint_invalid') from None


def load_checkpoint(output_dir, record):
    path = verify_checkpoint_files(output_dir, record)
    try:
        graph = read_graph_record(path, expected_digest=record['graph_digest'])
        evidence = json.loads((path.parent / 'evidence.json').read_text('utf-8')) if record['has_evidence'] else {}
        if (digest(evidence) != record['evidence_digest']
                or record['access_fingerprint'] != graph.get('access_fingerprint')):
            raise ValueError
        return {'contributor_id': record['node_id'], 'kind': record['contributor_kind'],
                'graph': graph, 'evidence_records': evidence}
    except (LightGraphError, KeyError, TypeError, ValueError, OSError):
        raise LightGraphError('workflow_checkpoint_invalid') from None

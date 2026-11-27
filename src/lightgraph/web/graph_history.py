"""Read committed graph versions without changing the project's output pointer."""
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from pydantic import ValidationError

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.construction.paths import io_path
from lightgraph.construction.snapshot_registry import (snapshot_id, authorize_snapshot, migrate_access_context,
                                                       inherited_grants, authorize_inherited_grants)
from lightgraph.construction.workspace import read_internal_yaml
from lightgraph.runtime.config import RunConfig
from . import construction_graph


@dataclass
class Entry:
    root: Path
    auth_root: Path
    head: dict
    row: dict
    worker_root: Path | None = None


def _within(path, project):
    path.relative_to(project)
    current = path
    while current != project:
        if io_path(current).is_symlink():
            raise LightGraphError('workspace_path_invalid')
        current = current.parent
    return path


def _effective_config(config, entry):
    # A worker has at most the grants of its project. Rebuild its table and
    # source restrictions without opening its inputs or a model configuration.
    root = entry.worker_root
    if root is not None and config.workflow:
        from lightgraph.construction.workflow import build_unit_config, build_merge_node_config
        node_id = root.parent.name
        unit = next((u for u in config.workflow.units if u.unit_id == node_id), None)
        if unit is not None:
            return build_unit_config(config, unit, unit_output_dir=root.parent)
        node = next((n for n in config.workflow.merge_nodes if n.node_id == node_id), None)
        if node is not None:
            upstream = root.parent / 'upstream.graph'
            if not upstream.exists() and root.parent.parent.parent.name == 'attempts':
                base = root.parents[3] / 'checkpoints' / root.parents[1].name / node_id / 'base' / 'graph.graph'
                if base.is_dir():
                    upstream = base
            return build_merge_node_config(config, node, upstream_snapshot_path=upstream,
                                           upstream_summary='', node_output_dir=root.parent)
    return config.model_copy(update={'output_dir': str(entry.root.parent)})


def _manifest_entries(project, root, active_root, run_id, worker):
    path = _within(root / 'manifest.yaml', project)
    if not io_path(path).is_file():
        return
    manifest = read_internal_yaml(path)
    if not isinstance(manifest, dict) or manifest.get('target') != 'graph' or manifest.get('format_version') != 1:
        return
    current = manifest.get('current_graph')
    if not isinstance(current, dict):
        current = None
    history = manifest.get('graph_history', [])
    if not isinstance(history, list):
        return
    seen = set()
    for head in [current, *reversed(history)]:
        if not isinstance(head, dict) or not isinstance(head.get('path'), str):
            continue
        version = snapshot_id(head, root=root)
        if version in seen:
            continue
        seen.add(version)
        is_pointer = bool(current and version == snapshot_id(current, root=root))
        old_delta = ('inherited_summary' not in head and not is_pointer
                     and head.get('storage_layout') not in {'sharded_v1', 'parquet_v1'}
                     and (root / 'inherited' / 'summary.yaml').is_file())
        if old_delta:
            # An older legacy publication has no pinned base. Only the committed
            # delta is historically attributable to it.
            head = {**head, 'graph_kind': 'node_delta'}
        row = {'version_id': version, 'created_at': head.get('created_at'), 'run_id': run_id if worker else head.get('run_id'),
               'node_id': root.parent.name if worker else None,
               'graph_kind': 'node_delta' if old_delta else ('node_full' if worker else 'project_full'),
               'coverage': head.get('coverage'), 'is_current': root == active_root and is_pointer}
        yield Entry(root, root, head, row, root if worker else None)


def _checkpoint_entry(project, output, path):
    _within(path, project)
    path_io = io_path(path)
    if not path_io.is_file() or path_io.stat().st_size > 1024 * 1024:
        return None
    rec = json.loads(path_io.read_text(encoding='utf-8'))
    if not isinstance(rec, dict) or rec.get('format_version') != 1 or rec.get('artifact_status') != 'committed':
        return None
    parts = path.relative_to(output / 'checkpoints').parts
    rid, node = parts[:2]
    kind = rec.get('graph_kind')
    graph_path = path.parent / 'graph.graph'
    if (not re.fullmatch(r'run_[0-9a-f]{24}', rid) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', node)
            or kind not in {'node_full', 'node_base', 'node_delta'}
            or (kind == 'node_base') != (len(parts) == 4 and parts[2] == 'base')
            or rec.get('run_id') != rid or rec.get('node_id') != node
            or rec.get('relative_path') != graph_path.relative_to(output).as_posix()
            or not re.fullmatch(r'[a-f0-9]{64}', str(rec.get('graph_digest')))):
        return None
    head = {'path': 'graph.graph', 'context_fingerprint': rec.get('artifact_id'), 'graph_kind': kind,
            'access_fingerprint': rec.get('access_fingerprint'), 'result_policy_id': rec.get('result_policy_id'),
            'graph_digest': rec['graph_digest'], **({'inherited_grants': rec['inherited_grants']} if 'inherited_grants' in rec else {})}
    row = {'version_id': snapshot_id(head, root=path.parent), 'created_at': rec.get('created_at'), 'run_id': rid,
           'node_id': node, 'graph_kind': kind, 'coverage': rec.get('coverage'), 'is_current': False}
    worker_root = output / 'attempts' / rid / node / 'construction' if kind == 'node_delta' else None
    return Entry(path.parent, output / 'construction', head, row, worker_root)


def _entries(workspace, pid, config):
    project = workspace.project_path(pid)
    output = project / 'output'
    active_root = Path(config.output_dir) / 'construction'
    for directory in [output, *sorted((output / 'generations').glob('run_*'))]:
        try:
            _within(directory, project)
        except LightGraphError:
            continue
        generation = directory.name if directory != output else None
        roots = [(directory / 'construction', generation, False)]
        roots.extend((root, root.parents[1].name, True) for root in directory.glob('attempts/run_*/*/construction'))
        roots.extend((root, generation, True) for root in directory.glob('units/*/construction'))
        for root, run_id, worker in roots:
            try:
                yield from _manifest_entries(project, root, active_root, run_id, worker)
            except (LightGraphError, OSError):
                # One damaged historical manifest cannot suppress other runs.
                continue
        for pattern in ('checkpoints/run_*/*/checkpoint.json', 'checkpoints/run_*/*/base/checkpoint.json'):
            for path in directory.glob(pattern):
                try:
                    entry = _checkpoint_entry(project, directory, path)
                except (LightGraphError, OSError, ValueError, UnicodeError):
                    continue
                if entry is not None:
                    yield entry


def _upstream_grant(entry, effective):
    root = entry.worker_root
    if root is None or not effective.inherited_graphs or root.parents[2].name != 'attempts':
        return None
    node_id, run_id = root.parents[0].name, root.parents[1].name
    ref = effective.inherited_graphs[0]
    if not ref.snapshot_path or ref.inherit_id != ('upstream_' + node_id if len(node_id) <= 55
                                                   else 'upstream_' + digest(node_id)[:32]):
        return None
    output = root.parents[3]
    # A merge worker may be restricted to one table while its frozen base
    # includes all upstream tables. Check the parent grant as well.
    commit = output / 'checkpoints' / run_id / node_id / 'base' / 'checkpoint.json'
    if commit.is_file():
        base = _checkpoint_entry(output.parent, output, commit)
        if base is None:
            raise LightGraphError('workflow_checkpoint_invalid')
        return base.auth_root, base.head
    summary = entry.head.get('inherited_summary')
    if summary is None:
        path = construction_graph._record_path(root, 'inherited/summary.yaml')
        summary = read_internal_yaml(path) if path.is_file() else {}
    source_access = next((item.get('source_access') for item in summary.get('inherited', [])
                          if isinstance(item, dict) and item.get('inherit_id') == ref.inherit_id), None)
    return output / 'construction', {'access_fingerprint': source_access}


def _check_inherited_projects(workspace, entry, config, upstream=None):
    references = [r for r in config.inherited_graphs if r.source_project_id]
    if not references:
        return
    # Merge workers inherit an internal base which may itself contain imported
    # data. Use its committed project-level grants when available.
    root, head = upstream if upstream is not None else (entry.auth_root, entry.head)
    grants = head.get('inherited_grants')
    if grants is None:
        grants = inherited_grants(root, summary=head.get('inherited_summary'))
    authorize_inherited_grants(references, grants, workspace.result_config)


def _authorize_entry(workspace, entry, config):
    effective = _effective_config(config, entry)
    upstream = _upstream_grant(entry, effective)
    if upstream is not None:
        authorize_snapshot(*upstream, config)
    _check_inherited_projects(workspace, entry, config, upstream)
    authorize_snapshot(entry.auth_root, entry.head, effective)
    return effective


def list_versions(workspace, pid, *, config):
    versions = []
    for entry in _entries(workspace, pid, config):
        row = dict(entry.row)
        try:
            _authorize_entry(workspace, entry, config)
            if not construction_graph._record_path(entry.root, entry.head['path']).exists():
                raise LightGraphError('graph_workspace_record_unavailable')
            row['readable'] = True
        except LightGraphError as exc:
            row.update(readable=False, error_code=exc.code)
        versions.append(row)
    versions.sort(key=lambda row: (row.get('created_at') or '', row['version_id']), reverse=True)
    return {'versions': versions, 'current_version_id': next((v['version_id'] for v in versions if v['is_current']), None)}


def resolve_version(workspace, pid, config, version):
    if re.fullmatch(r'v_[0-9a-f]{32}', version or ''):
        for entry in _entries(workspace, pid, config):
            if entry.row['version_id'] == version:
                return entry
    raise LightGraphError('graph_version_not_found')


def _prepared(workspace, pid, config, version):
    entry = resolve_version(workspace, pid, config, version)
    effective = _authorize_entry(workspace, entry, config)
    graph, generation = construction_graph.load_prepared(
        effective, root=entry.root, head=entry.head, authorization_root=entry.auth_root)
    return effective, graph, generation


def read_export(workspace, pid, *, config, format_name, version=None, business_context=None):
    from lightgraph.construction.graph_export import serialize_graph
    if version is not None:
        config, graph, _ = _prepared(workspace, pid, config, version)
    else:
        if not construction_graph.available(config):
            raise LightGraphError('graph_version_not_found')
        graph = construction_graph.load_graph(config)
    selected = construction_graph.select_business_context(graph, config, business_context)
    return {'version_id': graph.get('version_id'), 'payload': serialize_graph(selected, format_name),
            'nodes': len(selected['nodes']), 'edges': len(selected['edges'])}


def read_contexts(workspace, pid, *, config, version=None, **options):
    if version is not None:
        effective, graph, _ = _prepared(workspace, pid, config, version)
        return construction_graph.context_options(effective, prepared_graph=graph, **options)
    if not construction_graph.available(config):
        return {'contexts': [], 'total_contexts': 0, 'support_counts': {}, 'next_offset': None}
    return construction_graph.context_options(config, **options)


def read_page(workspace, pid, *, config, version, **options):
    if options.get('layer', 'business') != 'business':
        raise LightGraphError('graph_layer_invalid')
    effective, graph, _ = _prepared(workspace, pid, config, version)
    return construction_graph.graph_page(effective, prepared_graph=graph, **options)


def read_details(workspace, pid, *, config, version, **options):
    entry = resolve_version(workspace, pid, config, version)
    effective = _authorize_entry(workspace, entry, config)
    graph, _ = construction_graph.load_prepared(
        effective, root=entry.root, head=entry.head, authorization_root=entry.auth_root)
    use_catalog = bool(entry.row.get('is_current') or entry.worker_root is not None)
    return construction_graph.graph_details(
        effective, prepared_graph=graph, include_catalog_metadata=use_catalog, **options)


def read_batch(workspace, pid, *, config, version, **options):
    if options.get('layer', 'business') != 'business':
        raise LightGraphError('graph_layer_invalid')
    effective, graph, generation = _prepared(workspace, pid, config, version)
    from .graph_batches import read_graph_batch
    return read_graph_batch(workspace, pid, config=effective,
                            prepared_view=({**graph, 'display_schema': construction_graph.vocabulary(graph)}, generation), **options)


def read_consistently(workspace, pid, reader, **options):
    """Freeze current result permissions, then recheck before disclosing a page."""
    with workspace.lock(pid):
        project = workspace.get(pid)
        config = workspace.result_config(pid)
    result = reader(workspace, pid, config=config, **options)
    # A source project can revoke its grant while a large result is being read.
    if config.inherited_graphs and result.get('version_id'):
        entry = resolve_version(workspace, pid, config, result['version_id'])
        _authorize_entry(workspace, entry, config)
    with workspace.lock(pid):
        current = workspace.get(pid)
        latest = workspace.result_config(pid)
        if options.get('version'):
            # Switching active generations cannot change a selected snapshot.
            latest.output_dir = config.output_dir
        if (current['created_at'] != project['created_at'] or current['config_revision'] != project['config_revision']
                or latest != config):
            raise LightGraphError('graph_read_conflict')
    return result


def _previous_location(effective, project, previous_root):
    if not isinstance(previous_root, str) or len(previous_root) > 4096:
        raise LightGraphError('invalid_previous_project_root')
    previous = PureWindowsPath(previous_root)
    if not previous.is_absolute():
        previous = PurePosixPath(previous_root)
    if not previous.is_absolute() or '..' in previous.parts:
        raise LightGraphError('invalid_previous_project_root')
    historical = effective.model_copy(deep=True)
    for item, attr in [(s, 'path') for s in historical.sources] + [(r, 'snapshot_path') for r in historical.inherited_graphs]:
        value = getattr(item, attr)
        if value:
            try:
                relative = Path(value).relative_to(project)
            except ValueError:
                raise LightGraphError('graph_access_migration_evidence_mismatch') from None
            setattr(item, attr, str(previous.joinpath(*relative.parts)))
    return historical


def migrate_version(workspace, pid, version, evidence):
    """Verify the previous absolute paths by recomputing the original access hash."""
    config = workspace.result_config(pid)
    entry = resolve_version(workspace, pid, config, version)
    effective = _effective_config(config, entry)
    if set(evidence) == {'historical_config'}:
        try:
            historical = RunConfig.model_validate(evidence['historical_config'])
        except ValidationError:
            raise LightGraphError('invalid_historical_config') from None
    elif set(evidence) == {'previous_project_root'}:
        historical = _previous_location(effective, workspace.project_path(pid), evidence['previous_project_root'])
    else:
        raise LightGraphError('graph_access_migration_evidence_required')
    upstream = _upstream_grant(entry, effective)
    if upstream is not None:
        if 'previous_project_root' in evidence:
            prior_parent = _previous_location(config, workspace.project_path(pid), evidence['previous_project_root'])
            migrate_access_context(*upstream, prior_parent, config)
        else:
            authorize_snapshot(*upstream, config)
    migrate_access_context(entry.auth_root, entry.head, historical, effective)
    return {'version_id': version, 'readable': True}

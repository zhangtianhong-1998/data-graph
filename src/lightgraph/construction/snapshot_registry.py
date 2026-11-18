"""Committed publication heads and portable, credential-free result authorization.

Execution fingerprints deliberately retain their existing strict semantics. These
records only authorize reading immutable results; they never validate a rerun.
"""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
import re

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint
from .workspace import read_internal_yaml, write_yaml


# Root publication runs in the workflow coordinator's dedicated thread.
publication_run_id = ContextVar('graph_publication_run_id', default=None)

def result_access_policy(config):
    def selected(values):
        return sorted(set(values))
    return {
        'version': 1, 'isolation_domain': config.isolation_domain,
        'document_scopes': selected(config.document_scopes),
        'scope_bindings': sorted([b.model_dump(mode='json', by_alias=True) for b in config.scope_bindings], key=digest),
        'goal': {name: selected(getattr(config.goal, name)) for name in (
            'source_scope', 'excluded_columns', 'evidence_only_columns', 'entity_kinds', 'relation_kinds')},
        'sources': sorted([{
            'id': s.id, 'kind': s.kind, **s.file_scope_revision,
            'source_file': PureWindowsPath(s.path).name if s.path else None,
            'credential_revision': s.credential_revision,
            'credential_mode': s.credential_mode, 'dsn_env': s.dsn_env, 'allow_fact_data': s.allow_fact_data,
            'allowed_tables': selected(s.allowed_tables),
            'allowed_columns': None if s.allowed_columns is None else {
                table: selected(columns) for table, columns in sorted(s.allowed_columns.items())},
        } for s in config.sources], key=lambda s: s['id']),
        'inherited_graphs': sorted([{k: v for k, v in r.model_dump(mode='json').items()
                                    if k != 'snapshot_path'} | {'snapshot_file': PureWindowsPath(r.snapshot_path).name if r.snapshot_path else None}
                                    for r in config.inherited_graphs], key=digest),
    }


def _context_path(root, fingerprint, policy_id):
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint) or (
            policy_id is not None and (not isinstance(policy_id, str) or not re.fullmatch(r'[a-f0-9]{64}', policy_id))):
        raise LightGraphError('graph_access_context_changed')
    directory = Path(root) / 'access-contexts'
    name = (digest(fingerprint, policy_id)[:32] if policy_id else fingerprint) + '.yaml'
    path = directory / name
    if directory.is_symlink() or path.is_symlink() or Path(root).is_symlink():
        raise LightGraphError('workspace_path_invalid')
    return path


def register_access_context(root, config, *, legacy=False):
    """Bind the original strict fingerprint to its portable result permissions."""
    fingerprint = access_fingerprint(config)
    policy = result_access_policy(config)
    policy_id = digest(policy)
    path = _context_path(root, fingerprint, None if legacy else policy_id)
    record = {'format_version': 1, 'access_fingerprint': fingerprint,
              'result_access_policy': policy,
              'inheritance_digest': digest(sorted((r.model_dump(mode='json') for r in config.inherited_graphs),
                                                  key=lambda ref: ref['inherit_id']))}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read_internal_yaml(path) != record:
            raise LightGraphError('graph_access_context_changed')
    else:
        write_yaml(path, record)
    return policy_id


def authorize_snapshot(root, head, config):
    recorded = head.get('access_fingerprint')
    policy_id = head.get('result_policy_id')
    path = _context_path(root, recorded, policy_id)
    if policy_id is None and any(s.allow_fact_data is False for s in config.sources):
        # The original access hash predates per-source fact-data authorization.
        # A legacy result cannot prove it contains catalog-only observations.
        raise LightGraphError('graph_access_context_changed')
    if not path.is_file():
        if policy_id is None and recorded == access_fingerprint(config):
            return None
        raise LightGraphError('graph_access_context_changed')
    record = read_internal_yaml(path)
    policy = result_access_policy(config)
    if (not isinstance(record, dict) or record.get('format_version') != 1
            or record.get('access_fingerprint') != recorded or record.get('result_access_policy') != policy
            or policy_id is not None and policy_id != digest(policy)):
        raise LightGraphError('graph_access_context_changed')
    return record


def snapshot_id(head, *, root):
    # Copies in different generations are distinct selectable versions. Keep the
    # location project-relative so a drive or workspace move preserves the ID.
    root = Path(root)
    output = next((p for p in root.parents if p.name == 'output' and (p.parent / 'project.yaml').is_file()), None)
    location = root.relative_to(output).as_posix() if output is not None else None
    if head.get('publication_digest'):
        return 'v_' + digest(location, head['publication_digest'])[:32]
    # Preserve existing bookmarks for legacy heads without a publication witness.
    return 'v_' + digest(location, head.get('path'), head.get('context_fingerprint'), head.get('access_fingerprint'))[:32]


def inherited_grants(root, *, summary=None):
    """Small authorization witnesses for external projects whose results were frozen."""
    if summary is None:
        path = Path(root) / 'inherited' / 'summary.yaml'
        if path.is_symlink() or path.parent.is_symlink():
            raise LightGraphError('workspace_path_invalid')
        if not path.is_file():
            return []
        summary = read_internal_yaml(path)
    if not isinstance(summary, dict) or not isinstance(summary.get('inherited'), list):
        raise LightGraphError('invalid_inherited_graph_snapshot')
    grants = []
    for item in summary['inherited']:
        if not isinstance(item, dict) or not isinstance(item.get('upstream_grants', []), list):
            raise LightGraphError('invalid_inherited_graph_snapshot')
        if item.get('source_project_id'):
            grants.append({k: item.get(k) for k in ('source_project_id', 'source_access', 'source_policy_id', 'allowed_scopes')})
        grants.extend(item.get('upstream_grants', []))
    return grants


def authorize_inherited_grants(references, grants, resolve_config):
    """Recheck the direct and transitive project grants behind frozen source data."""
    required = {r.source_project_id for r in references if r.source_project_id}
    if not isinstance(grants, list) or any(
            not isinstance(g, dict) or not isinstance(g.get('source_project_id'), str)
            or not isinstance(g.get('allowed_scopes'), list)
            or any(not isinstance(s, str) for s in g['allowed_scopes']) for g in grants):
        raise LightGraphError('inherited_graph_scope_not_authorized')
    granted_projects = {g.get('source_project_id') for g in grants}
    if not required <= granted_projects:
        raise LightGraphError('inherited_graph_scope_not_authorized')
    for grant in grants:
        try:
            source = resolve_config(grant['source_project_id'])
        except LightGraphError:
            raise LightGraphError('inherited_graph_scope_not_authorized') from None
        # Before transitive witnesses were recorded, a legacy chain may only
        # identify its immediate parent. It cannot prove the remaining grants.
        if any(ref.source_project_id not in granted_projects for ref in source.inherited_graphs if ref.source_project_id):
            raise LightGraphError('inherited_graph_scope_not_authorized')
        visible = set(grant['allowed_scopes']) <= set(source.scopes)
        matches = (grant['source_policy_id'] == digest(result_access_policy(source)) if grant.get('source_policy_id')
                   else grant.get('source_access') == access_fingerprint(source))
        if not visible or not matches:
            raise LightGraphError('inherited_graph_scope_not_authorized')


def record_graph_head(manifest, head, *, root, graph_kind=None, coverage=None):
    """Commit the exact head and its frozen base alongside the manifest pointer."""
    root = Path(root)
    kind = graph_kind or head.get('graph_kind', 'workspace_publication')
    if publication_run_id.get():
        head.setdefault('run_id', publication_run_id.get())
    head.setdefault('created_at', datetime.now(timezone.utc).isoformat())
    head['graph_kind'] = kind
    if coverage is not None:
        head['coverage'] = coverage
    metadata = root / head['path'] / 'metadata.yaml'
    if metadata.is_file() and not metadata.is_symlink():
        head['graph_digest'] = read_internal_yaml(metadata)['graph_digest']
    # A project graph already contains its base; keep its witness, while a
    # workspace delta needs the actual summary to recompose the same version.
    inherited_dir = root / 'inherited'
    path = inherited_dir / 'summary.yaml'
    if inherited_dir.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    summary = read_internal_yaml(path) if path.is_file() else None
    if kind == 'project_full':
        head.pop('inherited_summary', None)
    else:
        head['inherited_summary'] = summary
    head['inherited_digest'] = digest(summary) if summary is not None else None
    head['inherited_grants'] = inherited_grants(root, summary=summary) if summary is not None else []
    # A content-addressed store may reuse one file for several publications.
    # Bind the commit (run, access, coverage and frozen inputs) before deriving
    # its relocatable external ID. Legacy heads enter history by verbatim copy.
    head['publication_digest'] = digest({k: v for k, v in head.items() if k not in {'version_id', 'publication_digest'}})
    head['version_id'] = snapshot_id(head, root=root)
    history = manifest.setdefault('graph_history', [])
    if not any(snapshot_id(item, root=root) == head['version_id'] for item in history):
        history.append(deepcopy(head))
    return head


def migrate_access_context(root, head, historical_config, current_config):
    """Accept historical config evidence only when its exact original hash matches."""
    if head.get('result_policy_id') is None and any(s.allow_fact_data is False for s in current_config.sources):
        raise LightGraphError('graph_access_context_changed')
    if access_fingerprint(historical_config) != head.get('access_fingerprint'):
        raise LightGraphError('graph_access_migration_evidence_mismatch')
    if result_access_policy(historical_config) != result_access_policy(current_config):
        raise LightGraphError('graph_access_context_changed')
    if head.get('result_policy_id') is not None and head['result_policy_id'] != digest(result_access_policy(historical_config)):
        raise LightGraphError('graph_access_context_changed')
    register_access_context(root, historical_config, legacy=head.get('result_policy_id') is None)

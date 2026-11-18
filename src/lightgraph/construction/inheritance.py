"""Cross-project graph inheritance via explicit publication authorization and digest locking."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import uuid
from typing import Any

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint, filter_graph_scopes
from .workspace import read_internal_yaml, write_yaml
from .graph_storage import read_graph_record, write_graph_record


def _namespace_ref(prefix: str, ref: str) -> str:
    text = str(ref)
    return f'{prefix}:{text}'


def _namespace_graph_evidence(graph: dict[str, Any], *, inherit_id: str,
                              source_project_id: str | None, source_digest: str) -> dict[str, Any]:
    prefix = f'base:{inherit_id}'
    result = deepcopy(graph)
    marker = {
        'inherit_id': inherit_id,
        'source_project_id': source_project_id,
        'source_graph_digest': source_digest,
    }
    result['evidence_refs'] = [_namespace_ref(prefix, ref) for ref in result.get('evidence_refs', [])]
    for item in result.get('nodes', []) + result.get('edges', []):
        for prov in item.get('provenance', []):
            if isinstance(prov, dict) and prov.get('evidence_refs'):
                prov['evidence_refs'] = [_namespace_ref(prefix, r) for r in prov['evidence_refs']]
        for support in item.get('supports', []):
            if not isinstance(support, dict):
                continue
            support['inherited_from'] = deepcopy(marker)
            for origin in support.get('execution_origins', []):
                if origin.get('evidence_refs'):
                    origin['evidence_refs'] = [_namespace_ref(prefix, r) for r in origin['evidence_refs']]
            prov = support.get('provenance')
            if isinstance(prov, dict) and prov.get('evidence_refs'):
                prov['evidence_refs'] = [_namespace_ref(prefix, r) for r in prov['evidence_refs']]
            if support.get('evidence_refs'):
                support['evidence_refs'] = [_namespace_ref(prefix, r) for r in support['evidence_refs']]
            endpoint = (prov or {}).get('metadata_endpoint') if isinstance(prov, dict) else None
            if isinstance(endpoint, dict) and isinstance(endpoint.get('column'), dict):
                refs = endpoint['column'].get('evidence_refs')
                if refs:
                    endpoint['column']['evidence_refs'] = [_namespace_ref(prefix, r) for r in refs]
    return result


def load_inherited_source_graph(ref, *, workspace=None, grant_evidence=None) -> tuple[dict[str, Any], str]:
    """Read an authorized, scope-filtered published graph without relying on isolation_domain."""
    if ref.source_project_id:
        if workspace is None:
            raise LightGraphError('inherited_graph_workspace_required')
        source_config = workspace.resolved_config(ref.source_project_id).model_copy(deep=True)
        if not set(ref.allowed_scopes) <= set(source_config.scopes):
            raise LightGraphError('inherited_graph_scope_not_authorized')
        source_config.scopes = list(ref.allowed_scopes)
        from lightgraph.web.construction_graph import load_graph
        graph = load_graph(source_config)
        if grant_evidence is not None:
            from .snapshot_registry import inherited_grants, result_access_policy
            grant_evidence['source_policy_id'] = digest(result_access_policy(source_config))
            if source_config.inherited_graphs:
                from lightgraph.web.graph_history import _authorize_entry, resolve_version
                source_entry = resolve_version(workspace, ref.source_project_id, source_config, graph['version_id'])
                _authorize_entry(workspace, source_entry, source_config)
                grants = source_entry.head.get('inherited_grants')
                grant_evidence['upstream_grants'] = (grants if grants is not None else inherited_grants(source_entry.auth_root))
    else:
        path = Path(ref.snapshot_path)
        if not (path.is_file() or path.is_dir()) or path.is_symlink():
            raise LightGraphError('inherited_graph_snapshot_not_found')
        raw = read_graph_record(path) if path.is_dir() else read_internal_yaml(path)
        if not isinstance(raw, dict) or raw.get('status') != 'published' or raw.get('target') != 'graph':
            raise LightGraphError('invalid_inherited_graph_snapshot')
        graph = filter_graph_scopes(raw, ref.allowed_scopes)
    if graph.get('status') != 'published' or graph.get('target') != 'graph':
        raise LightGraphError('inherited_publication_required')
    actual_digest = digest({
        'nodes': graph.get('nodes', []),
        'edges': graph.get('edges', []),
        'definitions': graph.get('definitions', {}),
    })
    if ref.expected_graph_digest and actual_digest != ref.expected_graph_digest:
        raise LightGraphError('inherited_graph_digest_mismatch')
    return graph, actual_digest


def freeze_inherited_graphs(config, construction_dir: str | Path, *, workspace=None) -> dict[str, Any]:
    """Freeze authorized inherited graphs into local construction/inherited/ storage."""
    root = Path(construction_dir)
    inherited_dir = root / 'inherited'
    inherited_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    contributors = []
    for ref in config.inherited_graphs:
        if not set(ref.allowed_scopes) <= set(config.scopes):
            raise LightGraphError('inherited_graph_scope_not_authorized')
        grant_evidence = {}
        graph, actual_digest = load_inherited_source_graph(ref, workspace=workspace, grant_evidence=grant_evidence)
        tagged = _namespace_graph_evidence(graph, inherit_id=ref.inherit_id,
                                           source_project_id=ref.source_project_id, source_digest=actual_digest)
        frozen_digest = digest(tagged)
        # Short independent generations avoid MAX_PATH and let interrupted
        # writes remain unreferenced without blocking a retry of the same graph.
        snapshot_file = uuid.uuid4().hex + '.graph'
        snapshot_path = inherited_dir / snapshot_file
        write_graph_record(snapshot_path, tagged)
        summaries.append({
            'inherit_id': ref.inherit_id, 'source_project_id': ref.source_project_id,
            'source_access': graph.get('access_fingerprint'), **grant_evidence,
            'source_graph_digest': actual_digest, 'allowed_scopes': list(ref.allowed_scopes),
            'frozen_digest': frozen_digest, 'snapshot_file': snapshot_file,
            'nodes': len(tagged['nodes']), 'edges': len(tagged['edges']),
            'entity_types': [{'type': e['type'], 'key_names': list(e.get('key_names', []))}
                             for e in tagged.get('definitions', {}).get('entities', [])],
        })
        contributors.append({'kind': 'base', 'contributor_id': ref.inherit_id, 'graph': tagged})
    base_graph = _combine_frozen(contributors, config.fusion_policy)
    summary = {'format_version': 2, 'configuration_digest': _configuration_digest(config),
               'fusion_policy': config.fusion_policy.model_dump(mode='json'),
               'inherited_count': len(summaries), 'inherited': summaries,
               'entity_types': sorted({t['type'] for t in (base_graph or {}).get('definitions', {}).get('entities', [])})}
    # Publish the pointer only after every snapshot and their combined identity space are valid.
    write_yaml(inherited_dir / 'summary.yaml', summary)
    return {'summary': summary, 'base_graph': base_graph}


def _configuration_digest(config):
    return digest(sorted((r.model_dump(mode='json') for r in config.inherited_graphs),
                         key=lambda ref: ref['inherit_id']))


def _combine_frozen(contributors, fusion_policy=None):
    from .merger import merge_construction_graphs
    if not contributors:
        return None
    return merge_construction_graphs(contributors, merge_policy={'fusion_policy': fusion_policy})


def frozen_contributors(construction_dir, *, config=None, summary=None, authorized_digest=None):
    inherited_dir = Path(construction_dir) / 'inherited'
    summary_path = inherited_dir / 'summary.yaml'
    empty = {'format_version': 2, 'inherited_count': 0, 'inherited': [], 'entity_types': []}
    if config is not None and not config.inherited_graphs:
        return [], empty
    if inherited_dir.is_symlink() or summary_path.is_symlink():
        raise LightGraphError('invalid_inherited_graph_snapshot')
    if summary is None and not summary_path.is_file():
        if config is not None and config.inherited_graphs:
            raise LightGraphError('inherited_graph_snapshot_not_found')
        return [], empty
    if summary is None:
        summary = read_internal_yaml(summary_path)
    if not isinstance(summary, dict) or not isinstance(summary.get('inherited'), list):
        raise LightGraphError('invalid_inherited_graph_snapshot')
    if config is not None:
        if (summary.get('configuration_digest') != (authorized_digest or _configuration_digest(config))
                or any(not set(ref.allowed_scopes) <= set(config.scopes) for ref in config.inherited_graphs)):
            raise LightGraphError('inherited_graph_authorization_changed')
    contributors = []
    for item in summary['inherited']:
        iid = item.get('inherit_id')
        if not isinstance(iid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', iid):
            raise LightGraphError('invalid_inherited_graph_snapshot')
        filename = item.get('snapshot_file', f'{iid}.yaml')
        if not isinstance(filename, str) or Path(filename).name != filename or (inherited_dir / filename).is_symlink():
            raise LightGraphError('invalid_inherited_graph_snapshot')
        path = (inherited_dir / filename).resolve()
        if path.parent != inherited_dir.resolve() or not (path.is_file() or path.is_dir()):
            raise LightGraphError('inherited_graph_snapshot_not_found')
        if path.is_dir():
            graph = read_graph_record(path)
        else:
            # Historical YAML snapshots retain their wrapper and long filename.
            payload = read_internal_yaml(path)
            graph = payload.get('graph') if isinstance(payload, dict) and payload.get('inherit_id') == iid else None
        if not isinstance(graph, dict):
            raise LightGraphError('invalid_inherited_graph_snapshot')
        if item.get('frozen_digest') and digest(graph) != item['frozen_digest']:
            raise LightGraphError('inherited_graph_digest_mismatch')
        contributors.append({'kind': 'base', 'contributor_id': iid, 'graph': graph})
    return contributors, summary


def load_frozen_base_graph(construction_dir: str | Path, *, config=None, summary=None, authorized_digest=None):
    """Read only the immutable snapshots authorized by the active configuration."""
    contributors, summary = frozen_contributors(construction_dir, config=config, summary=summary, authorized_digest=authorized_digest)
    return _combine_frozen(contributors, summary.get('fusion_policy')), summary


def compose_published_graph(graph, base_graph, access):
    """A unit publishes a delta; a sharded project already contains its base."""
    if not base_graph or graph.get('storage_layout') in {'sharded_v1', 'parquet_v1'}:
        return graph
    from .merger import merge_construction_graphs
    return merge_construction_graphs([
        {'contributor_id': 'base', 'kind': 'base', 'graph': base_graph},
        {'contributor_id': 'delta', 'kind': 'unit', 'graph': graph},
    ], target_access_fingerprint=access, merge_policy={'fusion_policy': graph.get('fusion_policy')})


def authorize_frozen_inheritance(config, workspace):
    """Retry uses the pinned publication and rechecks current source visibility, without reading old facts."""
    from .snapshot_registry import inherited_grants, authorize_inherited_grants
    root = Path(config.output_dir) / 'construction'
    _, summary = frozen_contributors(root, config=config)
    grants = inherited_grants(root, summary=summary)
    if grants or any(ref.source_project_id for ref in config.inherited_graphs):
        if workspace is None:
            raise LightGraphError('inherited_graph_workspace_required')
        authorize_inherited_grants(config.inherited_graphs, grants, workspace.resolved_config)
        # Reuse for execution retains the original strict source fingerprint.
        for grant in grants:
            if access_fingerprint(workspace.resolved_config(grant['source_project_id'])) != grant['source_access']:
                raise LightGraphError('inherited_graph_scope_not_authorized')

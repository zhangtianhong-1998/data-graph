"""Cross-project graph inheritance via explicit publication authorization and digest locking."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint, filter_graph_scopes
from .workspace import safe_load_yaml, write_yaml


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


def load_inherited_source_graph(ref, *, workspace=None) -> tuple[dict[str, Any], str]:
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
    else:
        path = Path(ref.snapshot_path)
        if not path.is_file() or path.is_symlink():
            raise LightGraphError('inherited_graph_snapshot_not_found')
        raw = safe_load_yaml(path.read_text(encoding='utf-8'), max_bytes=128 * 1024 * 1024)
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
        graph, actual_digest = load_inherited_source_graph(ref, workspace=workspace)
        tagged = _namespace_graph_evidence(graph, inherit_id=ref.inherit_id,
                                           source_project_id=ref.source_project_id, source_digest=actual_digest)
        frozen_digest = digest(tagged)
        snapshot_file = f'{ref.inherit_id}-{frozen_digest}.yaml'
        write_yaml(inherited_dir / snapshot_file, {
            'format_version': 1, 'inherit_id': ref.inherit_id, 'source_graph_digest': actual_digest,
            'source_project_id': ref.source_project_id, 'allowed_scopes': ref.allowed_scopes, 'graph': tagged,
        })
        summaries.append({
            'inherit_id': ref.inherit_id, 'source_project_id': ref.source_project_id,
            'source_access': graph.get('access_fingerprint'),
            'source_graph_digest': actual_digest, 'allowed_scopes': list(ref.allowed_scopes),
            'frozen_digest': frozen_digest, 'snapshot_file': snapshot_file,
            'nodes': len(tagged['nodes']), 'edges': len(tagged['edges']),
            'entity_types': [{'type': e['type'], 'key_names': list(e.get('key_names', []))}
                             for e in tagged.get('definitions', {}).get('entities', [])],
        })
        contributors.append({'kind': 'base', 'contributor_id': ref.inherit_id, 'graph': tagged})
    base_graph = _combine_frozen(contributors)
    summary = {'format_version': 2, 'configuration_digest': _configuration_digest(config),
               'inherited_count': len(summaries), 'inherited': summaries,
               'entity_types': sorted({t['type'] for s in summaries for t in s['entity_types']})}
    # Publish the pointer only after every snapshot and their combined identity space are valid.
    write_yaml(inherited_dir / 'summary.yaml', summary)
    return {'summary': summary, 'base_graph': base_graph}


def _configuration_digest(config):
    return digest(sorted((r.model_dump(mode='json') for r in config.inherited_graphs),
                         key=lambda ref: ref['inherit_id']))


def _combine_frozen(contributors):
    from .merger import merge_construction_graphs
    if not contributors:
        return None
    return merge_construction_graphs(contributors)


def frozen_contributors(construction_dir, *, config=None):
    inherited_dir = Path(construction_dir) / 'inherited'
    summary_path = inherited_dir / 'summary.yaml'
    empty = {'format_version': 2, 'inherited_count': 0, 'inherited': [], 'entity_types': []}
    if config is not None and not config.inherited_graphs:
        return [], empty
    if not summary_path.is_file():
        if config is not None and config.inherited_graphs:
            raise LightGraphError('inherited_graph_snapshot_not_found')
        return [], empty
    summary = safe_load_yaml(summary_path.read_text(encoding='utf-8'))
    if not isinstance(summary, dict) or not isinstance(summary.get('inherited'), list):
        raise LightGraphError('invalid_inherited_graph_snapshot')
    if config is not None:
        if (summary.get('configuration_digest') != _configuration_digest(config)
                or any(not set(ref.allowed_scopes) <= set(config.scopes) for ref in config.inherited_graphs)):
            raise LightGraphError('inherited_graph_authorization_changed')
    contributors = []
    for item in summary['inherited']:
        iid = item.get('inherit_id')
        if not isinstance(iid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', iid):
            raise LightGraphError('invalid_inherited_graph_snapshot')
        filename = item.get('snapshot_file', f'{iid}.yaml')
        path = (inherited_dir / filename).resolve()
        if path.parent != inherited_dir.resolve() or not path.is_file():
            raise LightGraphError('inherited_graph_snapshot_not_found')
        payload = safe_load_yaml(path.read_text(encoding='utf-8'), max_bytes=128 * 1024 * 1024)
        graph = payload.get('graph') if isinstance(payload, dict) else None
        if not isinstance(graph, dict) or payload.get('inherit_id') != iid:
            raise LightGraphError('invalid_inherited_graph_snapshot')
        if item.get('frozen_digest') and digest(graph) != item['frozen_digest']:
            raise LightGraphError('inherited_graph_digest_mismatch')
        contributors.append({'kind': 'base', 'contributor_id': iid, 'graph': graph})
    return contributors, summary


def load_frozen_base_graph(construction_dir: str | Path, *, config=None):
    """Read only the immutable snapshots authorized by the active configuration."""
    contributors, summary = frozen_contributors(construction_dir, config=config)
    return _combine_frozen(contributors), summary


def compose_published_graph(graph, base_graph, access):
    """A unit publishes a delta; a sharded project already contains its base."""
    if not base_graph or graph.get('storage_layout') == 'sharded_v1':
        return graph
    from .merger import merge_construction_graphs
    return merge_construction_graphs([
        {'contributor_id': 'base', 'kind': 'base', 'graph': base_graph},
        {'contributor_id': 'delta', 'kind': 'unit', 'graph': graph},
    ], target_access_fingerprint=access)


def authorize_frozen_inheritance(config, workspace):
    """Retry uses the pinned publication and rechecks current source visibility, without reading old facts."""
    _, summary = frozen_contributors(Path(config.output_dir) / 'construction', config=config)
    entries = {item['inherit_id']: item for item in summary['inherited']}
    for ref in config.inherited_graphs:
        if ref.source_project_id:
            source = workspace.resolved_config(ref.source_project_id)
            if (not set(ref.allowed_scopes) <= set(source.scopes)
                    or access_fingerprint(source) != entries[ref.inherit_id].get('source_access')):
                raise LightGraphError('inherited_graph_scope_not_authorized')

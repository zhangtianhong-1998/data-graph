"""Read checked structure revisions independently of instance execution/publication."""
from pathlib import Path

from pydantic import ValidationError

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint, ensure_supported_access
from .models import GraphDraft
from .structure_export import _ReadOnlyWorkspace
from .workspace import safe_load_yaml


def load_structure_preview(config, *, draft_id=None, revision=None):
    """Expose only authorized, digest-checked historical plans; never open sources.

    A later draft or failed instance check does not erase an earlier structure
    check. Historical previews retain that distinction and cannot publish facts.
    """
    if (draft_id is not None and (not isinstance(draft_id, str) or not draft_id)
            or revision is not None and (type(revision) is not int or revision < 1 or not draft_id)):
        raise LightGraphError('invalid_structure_selection')
    ensure_supported_access(config)
    result = {'target': 'graph_structure', 'status': 'empty', 'plans': [], 'selected': None,
              'data_validation': 'not_run', 'instance_enumeration': 'not_run'}
    root = Path(config.output_dir) / 'construction'
    path = root / 'manifest.yaml'
    if root.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    if not path.exists():
        if draft_id is not None:
            raise LightGraphError('structure_preview_not_found')
        return result
    try:
        manifest = safe_load_yaml(path.read_text(encoding='utf-8'))
    except OSError:
        raise LightGraphError('graph_workspace_record_unavailable') from None
    if (not isinstance(manifest, dict) or manifest.get('format_version') != 1
            or manifest.get('target') != 'graph' or not isinstance(manifest.get('drafts'), dict)
            or not isinstance(manifest.get('evidence'), dict)
            or not isinstance(manifest.get('structure_checks', {}), dict)):
        raise LightGraphError('invalid_graph_workspace_manifest')
    expected_access = access_fingerprint(config)
    workspace = _ReadOnlyWorkspace(root, 'preview', manifest)
    plans, denied = {}, []
    for name, checks in manifest.get('structure_checks', {}).items():
        if not isinstance(checks, dict) or not isinstance(checks.get('history'), list):
            raise LightGraphError('invalid_graph_workspace_record')
        for entry in checks['history']:
            if not isinstance(entry, dict):
                raise LightGraphError('invalid_graph_workspace_record')
            artifact = workspace._read_record(entry.get('path'))
            if not isinstance(artifact, dict) or digest(artifact) != entry.get('digest'):
                raise LightGraphError('validated_structure_mismatch')
            try:
                draft = GraphDraft.model_validate(artifact.get('draft'))
            except ValidationError:
                raise LightGraphError('invalid_graph_workspace_record') from None
            if (artifact.get('format_version') != 1 or artifact.get('target') != 'graph_structure'
                    or artifact.get('validation_level') != 'structure'
                    or artifact.get('data_validation') != 'not_run'
                    or artifact.get('instance_enumeration') != 'not_run'
                    or type(artifact.get('passed')) is not bool
                    or type(artifact.get('revision')) is not int or artifact['revision'] < 1
                    or not isinstance(artifact.get('context_fingerprint'), str)
                    or not artifact['context_fingerprint']
                    or not isinstance(artifact.get('validation_id'), str)
                    or not isinstance(artifact.get('checked_at'), str)
                    or draft.draft_id != name or artifact.get('draft_id') != name
                    or digest(artifact['draft']) != artifact.get('draft_fingerprint')):
                raise LightGraphError('invalid_graph_workspace_record')
            if artifact.get('access_fingerprint') != expected_access:
                # History may span authorization changes. Do not reveal the
                # names, counts or bindings of versions no longer authorized.
                denied.append((name, artifact['revision']))
                continue
            workspace.context_fingerprint = artifact['context_fingerprint']
            current = workspace.get_draft(name)
            same_revision = (current.revision == artifact['revision']
                             and current.fingerprint == artifact['draft_fingerprint']
                             and current.context_fingerprint == artifact['context_fingerprint'])
            metadata = {'draft_id': name, 'revision': artifact['revision'],
                        'validation_id': artifact['validation_id'], 'checked_at': artifact['checked_at'],
                        'passed': artifact['passed'], 'is_current_revision': same_revision,
                        'historical': not same_revision,
                        'entity_type_count': len(draft.entities), 'relation_type_count': len(draft.relations),
                        'mapping_count': sum(len(item.mappings) for item in draft.entities + draft.relations)}
            # History is append-only. A revision's most recent structure check
            # is the selectable version, even when that check did not pass.
            plans[(name, artifact['revision'])] = (metadata, artifact)
    ordered = sorted(plans.values(), key=lambda item: (
        item[0]['checked_at'], item[0]['draft_id'], item[0]['revision']), reverse=True)
    matches = [item for item in ordered if (draft_id is None or item[0]['draft_id'] == draft_id)
               and (revision is None or item[0]['revision'] == revision)]
    if not matches:
        if any((draft_id is None or name == draft_id) and (revision is None or rev == revision)
               for name, rev in denied):
            raise LightGraphError('graph_access_context_changed')
        if draft_id is not None:
            raise LightGraphError('structure_preview_not_found')
        return result
    metadata, artifact = matches[0]
    return {**result, 'status': 'available', 'plans': [item[0] for item in ordered],
            'selected': {**artifact, **metadata, 'preview_only': True}}

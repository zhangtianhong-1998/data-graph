"""Read published structure plans without opening sources or modifying a workspace."""
from copy import deepcopy
from pathlib import Path

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint, ensure_supported_access
from .structure import structure_ready
from .workspace import GraphWorkspace, safe_load_yaml


class _ReadOnlyWorkspace(GraphWorkspace):
    """Use the workspace's guarded record readers with one fixed manifest view."""

    def __init__(self, root, context, manifest):
        self.root = root
        self.context_fingerprint = context
        self.manifest_path = root / 'manifest.yaml'
        self._manifest_view = manifest

    def _manifest(self):
        return deepcopy(self._manifest_view)

    def _save_manifest(self, manifest):
        raise LightGraphError('workspace_read_only')

    def _append_record(self, folder, value):
        raise LightGraphError('workspace_read_only')


def load_published_structures(config):
    """Export current, authorized plans, retaining the exact checked YAML content.

    The recorded context is compared across the publication, check and current
    draft. Reconstructing a new runtime context would open sources and does not
    belong to offline export. Structure checks never attest to instance data.
    """
    ensure_supported_access(config)
    root = Path(config.output_dir) / 'construction'
    path = root / 'manifest.yaml'
    if root.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    if not path.exists():
        raise LightGraphError('structure_publication_required')
    try:
        manifest = safe_load_yaml(path.read_text(encoding='utf-8'))
    except OSError:
        raise LightGraphError('graph_workspace_record_unavailable') from None
    if (not isinstance(manifest, dict) or manifest.get('format_version') != 1
            or manifest.get('target') != 'graph' or not isinstance(manifest.get('drafts'), dict)
            or not isinstance(manifest.get('evidence'), dict)):
        raise LightGraphError('invalid_graph_workspace_manifest')
    publications = manifest.get('structure_publications', {})
    if not isinstance(publications, dict):
        raise LightGraphError('invalid_graph_workspace_manifest')
    if not publications:
        raise LightGraphError('structure_publication_required')
    expected_access = access_fingerprint(config)
    plans = []
    for draft_id, publication in sorted(publications.items()):
        if (not isinstance(publication, dict) or publication.get('draft_id') != draft_id
                or not isinstance(publication.get('context_fingerprint'), str)
                or not publication['context_fingerprint']):
            raise LightGraphError('invalid_graph_workspace_record')
        workspace = _ReadOnlyWorkspace(root, publication['context_fingerprint'], manifest)
        artifact = workspace._read_record(publication.get('path'))
        if not isinstance(artifact, dict):
            raise LightGraphError('invalid_graph_workspace_record')
        if artifact.get('access_fingerprint') != expected_access:
            raise LightGraphError('graph_access_context_changed')
        record = workspace.get_draft(draft_id)
        if not structure_ready(workspace, record):
            raise LightGraphError('structure_publication_required')
        if (artifact.get('target') != 'graph_structure' or artifact.get('status') != 'published'
                or artifact.get('validation_level') != 'structure'
                or artifact.get('data_validation') != 'not_run'
                or artifact.get('instance_enumeration') != 'not_run'
                or digest(artifact.get('draft')) != record.fingerprint):
            raise LightGraphError('validated_structure_mismatch')
        plans.append(artifact)
    return {'format_version': 1, 'target': 'graph_structure', 'status': 'published',
            'semantic_origin': 'agent_authored', 'data_validation': 'not_run',
            'instance_enumeration': 'not_run', 'access_fingerprint': expected_access,
            'plans': plans}

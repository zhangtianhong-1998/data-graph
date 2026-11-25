"""File and authorization context for a workflow run from an independent CLI YAML."""
from pathlib import Path
from threading import RLock

from .errors import LightGraphError
from .runtime.config import load_config


class StandaloneWorkflow:
    project_id = 'standalone'

    def __init__(self, path, config):
        self.path = Path(path)
        self.config = config
        self.root = Path(config.output_dir)
        self._lock = RLock()
        if any(ref.source_project_id for ref in config.inherited_graphs):
            raise LightGraphError('inherited_graph_workspace_required')
        # An alternate YAML must not publish into a workbench project or its
        # generations without the service lease and the registered grants.
        if any((directory / 'project.yaml').is_file() and (directory / 'config.yaml').is_file()
               for directory in (self.root, *self.root.parents)):
            raise LightGraphError('workflow_requires_project_workspace')
        self.root.mkdir(parents=True, exist_ok=True)

    def project_path(self, pid):
        if pid != self.project_id:
            raise LightGraphError('inherited_graph_workspace_required')
        return self.root

    def lock(self, pid):
        self.project_path(pid)
        return self._lock

    def resolved_config(self, pid):
        self.project_path(pid)
        if load_config(self.path) != self.config:
            raise LightGraphError('input_dependency_changed')
        return self.config

    def config_revision(self, pid):
        self.resolved_config(pid)
        # Independent YAMLs have no project revision counter. The comparison
        # above revalidates the effective configuration on each publication.
        return 0

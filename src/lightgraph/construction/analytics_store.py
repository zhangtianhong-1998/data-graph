"""Versioned offline results for the same authorized graph view as the reader."""
import json
from pathlib import Path

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from .access import filter_graph_scopes
from .graph_analytics import ALGORITHM_VERSION, compute_graph_analytics
from .snapshot_registry import result_access_policy, snapshot_id
from .workspace import read_internal_yaml


def current_version(config):
    root = Path(config.output_dir) / 'construction'
    path = root / 'manifest.yaml'
    if root.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    head = read_internal_yaml(path).get('current_graph') if path.is_file() else None
    return snapshot_id(head, root=root) if head else None


def artifact_path(root, config, version, selection):
    key = digest(ALGORITHM_VERSION, version, result_access_policy(config), sorted(set(config.scopes)), selection)
    directory = Path(root) / 'analytics'
    path = directory / (key + '.json')
    if directory.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    return path


def read_analysis(root, config, version, selection):
    path = artifact_path(root, config, version, selection)
    if not path.is_file():
        return None
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        if (result['version_id'] == version and result['algorithm_version'] == ALGORITHM_VERSION
                and result['business_context'] == selection):
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise LightGraphError('analytics_record_invalid')


def persist_analysis(graph, config, selection, path):
    if selection is not None:
        graph = filter_graph_scopes(graph, config.scopes, business_context=selection)
    result = compute_graph_analytics(graph)
    names = {n['id']: n.get('display_label') or n.get('label') for n in graph['nodes']}
    for node in result['nodes']:
        node['display_name'] = names.get(node['id'])
    result['business_context'] = selection
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(path, json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    return result


def analyze_current(config, expected_version=None):
    """CLI completion hook; the immutable result is also readable in the workbench."""
    from lightgraph.web.construction_graph import load_graph
    version = current_version(config)
    if expected_version is not None and (version or '') != expected_version:
        raise LightGraphError('graph_read_conflict')
    if not version:
        return {'status': 'no_graph'}
    graph = load_graph(config)
    if expected_version is not None and graph.get('version_id') != expected_version:
        raise LightGraphError('graph_read_conflict')
    version = graph['version_id']
    path = artifact_path(Path(config.output_dir) / 'construction', config, version, None)
    result = read_analysis(path.parent.parent, config, version, None) or persist_analysis(graph, config, None, path)
    return {key: result[key] for key in ('status', 'version_id', 'counts', 'algorithm_version')}

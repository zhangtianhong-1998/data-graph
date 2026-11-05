"""Complete authorized graph transport, paginated independently by nodes/edges.

Node pages cannot carry all cross-page edges. The second pass therefore pages
the entire edge set of the same authorized generation, without a display cap.
No source database is opened by this reader.
"""
from collections import Counter
from pathlib import Path

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.graph_neighborhood import neighborhood_paths


def _authorized_view(workspace, pid, config, layer, include_candidates):
    from . import construction_graph
    from .execution import read_metadata, _bounded_view
    if layer == 'metadata':
        view = read_metadata(workspace, pid, config=config, full_graph=True)
        return view, digest(view)
    if construction_graph.available(config):
        graph, generation = construction_graph.load_prepared(config)
        return {**graph, 'display_schema': construction_graph.vocabulary(graph)}, generation
    path = Path(config.output_dir) / 'graph.sqlite'
    if path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    if not path.exists():
        return {'nodes': [], 'edges': []}, digest('empty', str(path))
    from lightgraph.store import GraphStore
    from .graph_display import display_schema
    with GraphStore(path) as store:
        authorized = store.view(set(config.scopes), config.isolation_domain,
                                include_candidates=include_candidates)
        view = _bounded_view(authorized, layer, bounded=False)
        view['display_schema'] = display_schema(store, authorized)
    return view, digest(view)


def read_graph_batch(workspace, pid, *, config, layer='business', part='nodes', offset=0,
                     limit=1000, generation='', include_candidates=False,
                     focus='', hops=2, traversal='chains'):
    if (layer not in ('business', 'metadata') or part not in ('nodes', 'edges')
            or offset < 0 or not 1 <= limit <= 1000 or hops not in (1, 2)
            or traversal not in ('all', 'chains')):
        raise LightGraphError('graph_page_invalid')
    graph, version = _authorized_view(workspace, pid, config, layer, include_candidates)
    # Include access/selection even for legacy and empty views. A cursor from
    # another project, authorization or neighborhood must never be reusable.
    version = digest(version, str(Path(config.output_dir).resolve()), config.model_dump(mode='json'),
                     layer, include_candidates, focus, hops, traversal)
    if generation and generation != version:
        raise LightGraphError('graph_generation_changed')
    nodes, edges = graph['nodes'], graph['edges']
    total_nodes, total_edges = len(nodes), len(edges)
    if focus:
        by_id = {node['id']: node for node in nodes}
        if focus not in by_id:
            raise LightGraphError('graph_item_not_found')
        order, _ = neighborhood_paths(by_id, edges, focus, hops, traversal)
        selected = set(order)
        nodes = [by_id[node] for node in order]
        edges = [edge for edge in edges if edge['source'] in selected and edge['target'] in selected]
    collection = nodes if part == 'nodes' else edges
    page = collection[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(collection) else None
    from .construction_graph import _listed
    return {'nodes': [_listed(item) for item in page] if part == 'nodes' else [],
            'edges': [_listed(item) for item in page] if part == 'edges' else [],
            'part': part, 'offset': offset, 'next_offset': next_offset, 'page_size': limit,
            'generation': version, 'complete_batches': True, 'layer': layer,
            'total_nodes': total_nodes, 'total_edges': total_edges,
            'matched_nodes': len(nodes), 'matched_edges': len(edges),
            'display_schema': graph.get('display_schema', {}),
            'kind_counts': dict(Counter(n.get('kind', n.get('layer', '节点')) for n in nodes)),
            'focus_node_id': focus or None, 'focus_hops': hops, 'focus_traversal': traversal,
            'focus_neighbor_count': max(0, len(nodes) - 1) if focus else 0,
            'view_scope': 'neighborhood' if focus else 'full_graph',
            'include_candidates': include_candidates,
            'truncated': True, 'edges_truncated': False}

"""Workbench views over the current, atomically published YAML graph version."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from lightgraph.contracts import digest, canonical
from lightgraph.construction.access import access_fingerprint, ensure_supported_access, filter_graph_scopes
from lightgraph.construction.workspace import GraphWorkspace, safe_load_yaml
from lightgraph.errors import LightGraphError
from lightgraph.graph_neighborhood import neighborhood_paths
from lightgraph.web.graph_view_cache import cached_generation, load_graph_view, remember_graph, view_token


def available(config):
    root = Path(config.output_dir) / 'construction'
    manifest = root / 'manifest.yaml'
    if not manifest.exists():
        return False
    if root.is_symlink() or manifest.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    value = safe_load_yaml(manifest.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('format_version') != 1 or value.get('target') != 'graph':
        raise LightGraphError('invalid_graph_workspace_manifest')
    return bool(value.get('current_graph'))


def _scope_key(config):
    return sorted(set(config.scopes))


def _record_path(root: Path, relative):
    if not isinstance(relative, str) or not relative:
        raise LightGraphError('invalid_graph_workspace_record')
    path = (root / relative).resolve()
    base = root.resolve()
    if not path.is_relative_to(base) or path == base:
        raise LightGraphError('invalid_graph_workspace_record')
    return path


def _view_key(kind, path: Path, stat, access, scopes):
    # Nanosecond timestamps do not survive a JSON number round trip.
    return {'version': 1, 'kind': kind, 'path': str(path), 'size': stat.st_size,
            'mtime_ns': str(stat.st_mtime_ns), 'access': access, 'scopes': scopes}


def load_prepared(config):
    """Return the authorized display graph and the token that identifies it."""
    ensure_supported_access(config)
    root = Path(config.output_dir) / 'construction'
    manifest_path = root / 'manifest.yaml'
    if root.is_symlink() or manifest_path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    manifest = safe_load_yaml(manifest_path.read_text(encoding='utf-8'))
    head = manifest.get('current_graph')
    expected_access = access_fingerprint(config)
    scopes = _scope_key(config)
    cache_dir = root / 'view-cache'
    if not head:
        key = _view_key('empty', manifest_path.resolve(), manifest_path.stat(), expected_access, scopes)
        return load_graph_view(cache_dir, key, lambda: (
            {'nodes': [], 'edges': [], 'definitions': {}, 'status': 'empty'}, view_token(key)))
    if head.get('access_fingerprint') != expected_access:
        raise LightGraphError('graph_access_context_changed')
    published = _record_path(root, head.get('path'))
    try:
        stat = published.stat()
    except OSError:
        raise LightGraphError('graph_workspace_record_unavailable') from None
    key = _view_key('published', published, stat, expected_access, scopes)

    def build():
        workspace = GraphWorkspace(root, head['context_fingerprint'])
        graph = workspace.read_published_graph()
        if graph.get('access_fingerprint') != expected_access:
            raise LightGraphError('graph_access_context_changed')
        graph = filter_graph_scopes(graph, config.scopes)
        graph['nodes'] = [{**node, 'kind': node['type'], 'layer': 'business', 'status': 'accepted'}
                          for node in graph.get('nodes', [])]
        graph['edges'] = [{**edge, 'relation': edge['type'], 'status': 'accepted',
                           'qualifiers': edge.get('properties', {})} for edge in graph.get('edges', [])]
        return graph, view_token(key)

    return load_graph_view(cache_dir, key, build)


def load_graph(config):
    graph, generation = load_prepared(config)
    remember_graph(graph, generation)
    return graph


def vocabulary(graph):
    definitions = graph.get("definitions", {})
    return {"entity_types": [{"name": e["type"], "description": e.get("description", "")}
                             for e in definitions.get("entities", [])],
            "relations": [{"name": e["type"], "description": e.get("description", "")}
                          for e in definitions.get("relations", [])], "properties": []}


def _listed(item):
    """Pages identify and draw facts. Row evidence stays on the detail read."""
    return {key: value for key, value in item.items() if key not in {'supports', 'provenance'}}


def _search_text(node):
    """Search visible business values; never provenance IDs or hidden supports."""
    values = [node.get("display_label", ""), node.get("label", ""), node.get("kind", "")]
    values.extend(node.get("aliases", []))
    for name, value in node.get("identity", {}).items():
        values.extend((name, value))
    return " ".join(str(value) for value in values).casefold()


def _focused_page(nodes, edges, focus, hops, offset, limit, traversal='all'):
    """Page an authorized neighborhood with its center and connecting paths."""
    by_id = {node['id']: node for node in nodes}
    if focus not in by_id:
        raise LightGraphError('graph_item_not_found')
    order, parents = neighborhood_paths(by_id, edges, focus, hops, traversal)
    retained, cursor = {focus}, offset
    while cursor < len(order):
        path, current = set(), order[cursor]
        while current is not None and current not in retained:
            path.add(current)
            current = parents[current]
        if len(retained | path) > limit:
            break
        retained |= path
        cursor += 1
    # A request too small to fit a connecting path must not return a repeating
    # cursor or pretend the disconnected endpoint is a complete neighborhood.
    next_offset = cursor if offset < cursor < len(order) else None
    return [by_id[i] for i in order], [by_id[i] for i in order if i in retained], parents, next_offset


def graph_page(config, *, layer="business", offset=0, limit=240, q="", kind="", focus="", hops=1,
               include_candidates=False, traversal='all'):
    if traversal not in ('all', 'chains'):
        raise LightGraphError('graph_page_invalid')
    if layer not in {"business", "metadata", "provenance", "all"} or offset < 0 or not 1 <= limit <= 1000 or hops not in {1, 2}:
        raise LightGraphError("graph_selection_invalid")
    graph = load_graph(config)
    generation = cached_generation(graph) or digest(graph)
    nodes = graph["nodes"] if layer in {"business", "all"} else []
    edges = graph["edges"] if nodes else []
    counts = Counter(n["kind"] for n in nodes)
    selected, parents = nodes, {}
    if q and not focus:
        needle = q.casefold()
        selected = [n for n in selected if needle in _search_text(n)]
    if kind and not focus:
        selected = [n for n in selected if n["kind"] == kind]
    if focus:
        # Search chooses a center; its name/type cannot restrict its neighbors.
        selected, page, parents, next_offset = _focused_page(nodes, edges, focus, hops, offset, limit, traversal)
    else:
        page = selected[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(selected) else None
    ids = {n["id"] for n in page}
    page_edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    if focus:
        paths = {frozenset((child, parent)) for child, parent in parents.items() if child in ids and parent in ids}
        backbone, remaining, seen = [], [], set()
        for edge in page_edges:
            pair = frozenset((edge['source'], edge['target']))
            if pair in paths and pair not in seen:
                backbone.append(edge)
                seen.add(pair)
            else:
                remaining.append(edge)
        page_edges = backbone + sorted(remaining, key=lambda e: focus not in (e['source'], e['target']))
    local_edges = page_edges[:3000]
    boundary = sum((e["source"] in ids) != (e["target"] in ids) for e in edges)
    return {"nodes": [_listed(node) for node in page], "edges": [_listed(edge) for edge in local_edges], "layer": layer,
            "total_nodes": len(nodes), "total_edges": len(edges), "matched_nodes": len(selected),
            "offset": offset, "page_size": limit, "next_offset": next_offset,
            "truncated": len(page) < len(selected) or len(local_edges) < len(page_edges),
            "edges_truncated": len(local_edges) < len(page_edges), "matched_edges": len(page_edges),
            "boundary_edges": boundary,
            "generation": generation, "query_strategy": "published_yaml", "kinds": sorted(counts),
            "kind_counts": dict(counts), "focus_node_id": focus or None,
            "focus_neighbor_count": max(0, len(selected) - 1) if focus else 0, "focus_hops": hops,
            "focus_traversal": traversal,
            "focus_total_edges": sum(focus in (e['source'], e['target']) for e in edges) if focus else None,
            "focus_loaded_edges": sum(focus in (e['source'], e['target']) for e in local_edges) if focus else None,
            "view_scope": "neighborhood" if focus else "search_results" if q or kind else "graph_page",
            "focus_filters_ignored": bool(focus and (q or kind)),
            "neighborhood_truncated": bool(focus and (len(page) < len(selected) or len(local_edges) < len(page_edges))),
            "include_candidates": include_candidates, "candidate_view_available": False,
            "display_schema": vocabulary(graph), "semantic_origin": "agent_authored", "complete_batches": True}


def graph_details(config, *, entity_id=None, fact_id=None, offset=0, include_candidates=False):
    if bool(entity_id) == bool(fact_id) or offset < 0:
        raise LightGraphError("graph_selection_invalid")
    graph = load_graph(config)
    selected = []
    supports = []

    def add(identifier, kind, payload, provenance):
        if fact_id and identifier != fact_id:
            return
        selected.append({"id": identifier, "fact_kind": kind, "status": "accepted", "payload": payload})
        for index, source in enumerate(provenance):
            binding = source.get("source", {})
            location = ".".join(str(binding.get(key, "")) for key in ("source_id", "schema", "table") if binding.get(key))
            support_id = digest(identifier, index)
            origin = {'authorized_catalog': 'metadata', 'agent_declared_semantics': 'agent_declared'}.get(
                source.get('origin'), 'source_query')
            supports.append({"id": support_id, "support_id": support_id, "fact_id": identifier,
                             "status": "accepted", "origin": origin,
                             "rule_id": source.get("definition", ""),
                             "source_binding": location, "binding": source,
                             "scope_ref": source.get('scope_ref', 'public')})

    def property_origins(node, name, value):
        origins = []
        fallback_label = not any(support.get('label') is not None for support in node.get('supports', []))
        for support in node.get('supports', []):
            if name == 'label':
                matches = support.get('label') == value or fallback_label and node['label'] == value
            elif name == 'alias':
                matches = value in support.get('aliases', [])
            else:
                matches = canonical(value) in {canonical(item) for item in support.get('properties', {}).get(name, [])}
            if matches:
                origin = {**support.get('provenance', {}), 'scope_ref': support['scope_ref']}
                if support.get('row_context') is not None:
                    origin['row_context'] = support['row_context']
                if origin not in origins:
                    origins.append(origin)
        return origins

    def support_origins(item):
        if 'supports' not in item:
            return item.get('provenance', [])
        return [{**support.get('provenance', {}), 'scope_ref': support['scope_ref'],
                 **{key: support[key] for key in ('row_context', 'lookup_matches', 'required_scope_refs')
                    if support.get(key) is not None}}
                for support in item['supports']]

    if entity_id or fact_id:
        nodes = [n for n in graph["nodes"] if not entity_id or n["id"] == entity_id]
        if entity_id and not nodes:
            raise LightGraphError("graph_item_not_found")
        for node in nodes:
            identifier = node["id"]
            add(identifier, "NodeExistence", {"entity_id": identifier, "kind": node["kind"]}, support_origins(node))
            properties = {**node.get("properties", {}), "label": [node["label"]], "alias": node.get("aliases", [])}
            for name, values in properties.items():
                for value in values:
                    add(digest(identifier, name, value), "PropertyValue", {"entity_id": identifier,
                        "name": name, "value": value}, property_origins(node, name, value))
    for edge in graph["edges"]:
        if edge["id"] == fact_id or entity_id and entity_id in {edge["source"], edge["target"]}:
            add(edge["id"], "Edge", {"source": edge["source"], "target": edge["target"],
                "relation": edge["relation"], "qualifiers": edge.get("properties", {})}, support_origins(edge))
    if not selected:
        raise LightGraphError("graph_item_not_found")
    facts = selected[offset:offset + 50]
    fact_ids = {f["id"] for f in facts}
    endpoints = {f["payload"].get(k) for f in facts for k in ("source", "target", "entity_id")}
    retained = [s for s in supports if s["fact_id"] in fact_ids]
    # Describe the actual visible supports, not a newer unvalidated draft. Old
    # publications may lack mapping revision numbers; do not invent revision 1.
    mappings = {}
    descriptions = {item["type"]: item.get("description", "") for group in ("entities", "relations")
                    for item in graph.get("definitions", {}).get(group, [])}
    for support in retained[:500]:
        source = support["binding"]
        key = digest({name: value for name, value in source.items() if name != 'row_context'})
        mappings[key] = {"definition": source.get("definition", ""),
            "description": descriptions.get(source.get("definition"), ""),
            "source": source.get("source", {}), "mapping_index": source.get("mapping"),
            "identity_columns": source.get("key_columns", []),
            "reference_columns": {name: source[name] for name in ("source_key_columns", "target_key_columns") if name in source},
            "endpoint_lookups": {name: source[name] for name in ('source_lookup', 'target_lookup') if name in source},
            "source_columns": source.get("columns", []), "scope_columns": source.get("scope_columns", []),
            "label_column": source.get("label_column"), "scope_ref": support["scope_ref"],
            "constant_properties": source.get('constant_properties', {}),
            "context_columns": source.get('context_columns', []),
            **({"metadata_endpoint": source['metadata_endpoint']} if 'metadata_endpoint' in source else {}),
            **({"source_kind": source['source_kind'], "evidence_refs": source.get('evidence_refs', [])}
               if source.get('source_kind') == 'metadata_column' else {})}
    return {"facts": facts, "supports": retained[:500], "rules": list(mappings.values()),
            "nodes": [n for n in graph["nodes"] if n["id"] in endpoints], "display_schema": vocabulary(graph),
            "total_facts": len(selected), "total_supports": len(retained), "supports_truncated": len(retained) > 500,
            "total_incident_edges": sum(entity_id in (e['source'], e['target']) for e in graph['edges']) if entity_id else None,
            "next_offset": offset + len(facts) if offset + len(facts) < len(selected) else None,
            "include_candidates": include_candidates, "semantic_origin": "agent_authored"}

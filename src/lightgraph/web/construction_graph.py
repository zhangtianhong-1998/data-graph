"""Workbench views over the current, atomically published YAML graph version."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from lightgraph.contracts import digest, canonical
from lightgraph.construction.access import access_fingerprint, ensure_supported_access, filter_graph_scopes
from lightgraph.construction.workspace import GraphWorkspace, safe_load_yaml
from lightgraph.errors import LightGraphError


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


def load_graph(config):
    ensure_supported_access(config)
    root = Path(config.output_dir) / "construction"
    manifest = root / "manifest.yaml"
    if root.is_symlink() or manifest.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    value = safe_load_yaml(manifest.read_text(encoding="utf-8"))
    head = value.get("current_graph")
    if not head:
        return {"nodes": [], "edges": [], "definitions": {}, "status": "empty"}
    expected_access = access_fingerprint(config)
    if head.get('access_fingerprint') != expected_access:
        raise LightGraphError('graph_access_context_changed')
    workspace = GraphWorkspace(root, head["context_fingerprint"])
    graph = workspace.read_published_graph()
    if graph.get('access_fingerprint') != expected_access:
        raise LightGraphError('graph_access_context_changed')
    graph = filter_graph_scopes(graph, config.scopes)
    graph["nodes"] = [{**n, "kind": n["type"], "layer": "business", "status": "accepted"}
                      for n in graph.get("nodes", [])]
    graph["edges"] = [{**e, "relation": e["type"], "status": "accepted", "qualifiers": e.get("properties", {})}
                      for e in graph.get("edges", [])]
    return graph


def vocabulary(graph):
    definitions = graph.get("definitions", {})
    return {"entity_types": [{"name": e["type"], "description": e.get("description", "")}
                             for e in definitions.get("entities", [])],
            "relations": [{"name": e["type"], "description": e.get("description", "")}
                          for e in definitions.get("relations", [])], "properties": []}


def _search_text(node):
    """Search visible business values; never provenance IDs or hidden supports."""
    values = [node.get("display_label", ""), node.get("label", ""), node.get("kind", "")]
    values.extend(node.get("aliases", []))
    for name, value in node.get("identity", {}).items():
        values.extend((name, value))
    return " ".join(str(value) for value in values).casefold()


def graph_page(config, *, layer="business", offset=0, limit=240, q="", kind="", focus="", hops=1,
               include_candidates=False):
    if layer not in {"business", "metadata", "provenance", "all"} or offset < 0 or not 1 <= limit <= 1000 or hops not in {1, 2}:
        raise LightGraphError("graph_selection_invalid")
    graph = load_graph(config)
    nodes = graph["nodes"] if layer in {"business", "all"} else []
    edges = graph["edges"] if nodes else []
    counts = Counter(n["kind"] for n in nodes)
    selected = nodes
    if q:
        needle = q.casefold()
        selected = [n for n in selected if needle in _search_text(n)]
    if kind:
        selected = [n for n in selected if n["kind"] == kind]
    reached = {focus} if focus else set()
    if focus:
        if not any(n["id"] == focus for n in nodes):
            raise LightGraphError("graph_item_not_found")
        for _ in range(hops):
            adjacent = {endpoint for e in edges if e["source"] in reached or e["target"] in reached
                        for endpoint in (e["source"], e["target"])}
            reached |= adjacent
        selected = [n for n in selected if n["id"] in reached]
    page = selected[offset:offset + limit]
    ids = {n["id"] for n in page}
    page_edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    local_edges = page_edges[:3000]
    boundary = sum((e["source"] in ids) != (e["target"] in ids) for e in edges)
    return {"nodes": page, "edges": local_edges, "layer": layer,
            "total_nodes": len(nodes), "total_edges": len(edges), "matched_nodes": len(selected),
            "offset": offset, "page_size": limit, "next_offset": offset + len(page) if offset + len(page) < len(selected) else None,
            "truncated": len(page) < len(selected) or len(local_edges) < len(page_edges),
            "edges_truncated": len(local_edges) < len(page_edges), "matched_edges": len(page_edges),
            "boundary_edges": boundary,
            "generation": digest(graph), "query_strategy": "published_yaml", "kinds": sorted(counts),
            "kind_counts": dict(counts), "focus_node_id": focus or None,
            "focus_neighbor_count": max(0, len(reached) - 1), "focus_hops": hops,
            "neighborhood_truncated": bool(focus and (len(page) < len(selected) or len(local_edges) < len(page_edges))),
            "include_candidates": include_candidates, "candidate_view_available": False,
            "display_schema": vocabulary(graph), "semantic_origin": "agent_authored"}


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
            supports.append({"id": support_id, "support_id": support_id, "fact_id": identifier,
                             "status": "accepted", "origin": "source_query",
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
                if origin not in origins:
                    origins.append(origin)
        return origins

    if entity_id or fact_id:
        nodes = [n for n in graph["nodes"] if not entity_id or n["id"] == entity_id]
        if entity_id and not nodes:
            raise LightGraphError("graph_item_not_found")
        for node in nodes:
            identifier = node["id"]
            add(identifier, "NodeExistence", {"entity_id": identifier, "kind": node["kind"]}, node.get("provenance", []))
            properties = {**node.get("properties", {}), "label": [node["label"]], "alias": node.get("aliases", [])}
            for name, values in properties.items():
                for value in values:
                    add(digest(identifier, name, value), "PropertyValue", {"entity_id": identifier,
                        "name": name, "value": value}, property_origins(node, name, value))
    for edge in graph["edges"]:
        if edge["id"] == fact_id or entity_id and entity_id in {edge["source"], edge["target"]}:
            add(edge["id"], "Edge", {"source": edge["source"], "target": edge["target"],
                "relation": edge["relation"], "qualifiers": edge.get("properties", {})}, edge.get("provenance", []))
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
        key = digest(source)
        mappings[key] = {"definition": source.get("definition", ""),
            "description": descriptions.get(source.get("definition"), ""),
            "source": source.get("source", {}), "mapping_index": source.get("mapping"),
            "identity_columns": source.get("key_columns", []),
            "reference_columns": {name: source[name] for name in ("source_key_columns", "target_key_columns") if name in source},
            "source_columns": source.get("columns", []), "scope_columns": source.get("scope_columns", []),
            "label_column": source.get("label_column"), "scope_ref": support["scope_ref"]}
    return {"facts": facts, "supports": retained[:500], "rules": list(mappings.values()),
            "nodes": [n for n in graph["nodes"] if n["id"] in endpoints], "display_schema": vocabulary(graph),
            "total_facts": len(selected), "total_supports": len(retained), "supports_truncated": len(retained) > 500,
            "next_offset": offset + len(facts) if offset + len(facts) < len(selected) else None,
            "include_candidates": include_candidates, "semantic_origin": "agent_authored"}

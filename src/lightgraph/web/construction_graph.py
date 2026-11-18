"""Workbench views over the current, atomically published YAML graph version."""
from __future__ import annotations

from collections import Counter
from pathlib import Path, PureWindowsPath

from lightgraph.contracts import digest, canonical
from lightgraph.construction.access import access_fingerprint, ensure_supported_access, filter_graph_scopes
from lightgraph.construction.workspace import read_internal_yaml
from lightgraph.errors import LightGraphError
from lightgraph.graph_neighborhood import neighborhood_paths
from lightgraph.web.graph_view_cache import cached_generation, load_graph_view, remember_graph, view_token
from .graph_payload import listed, canvas_item, canvas_page, MAX_PAGE_BYTES


def available(config):
    root = Path(config.output_dir) / 'construction'
    manifest = root / 'manifest.yaml'
    if not manifest.exists():
        return False
    if root.is_symlink() or manifest.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    value = read_internal_yaml(manifest)
    if not isinstance(value, dict) or value.get('format_version') != 1 or value.get('target') != 'graph':
        raise LightGraphError('invalid_graph_workspace_manifest')
    return bool(value.get('current_graph'))


def _scope_key(config):
    return sorted(set(config.scopes))


def _record_path(root: Path, relative):
    if not isinstance(relative, str) or not relative:
        raise LightGraphError('invalid_graph_workspace_record')
    name = PureWindowsPath(relative)
    if name.anchor or not name.parts or '..' in name.parts:
        raise LightGraphError('invalid_graph_workspace_record')
    if root.is_symlink():
        raise LightGraphError('invalid_graph_workspace_record')
    path = root
    for part in name.parts:
        path = path / part
        if path.is_symlink():
            raise LightGraphError('invalid_graph_workspace_record')
    return path.resolve()


def _view_key(kind, path: Path, stat, access, scopes):
    # Nanosecond timestamps do not survive a JSON number round trip.
    return {'version': 4, 'kind': kind, 'path': str(path), 'size': stat.st_size,
            'mtime_ns': str(stat.st_mtime_ns), 'access': access, 'scopes': scopes}


def load_prepared(config, *, root=None, head=None, authorization_root=None):
    """Return the authorized display graph and the token that identifies it."""
    ensure_supported_access(config)
    selected_head = head is not None
    root = Path(root) if root is not None else Path(config.output_dir) / 'construction'
    manifest_path = root / 'manifest.yaml'
    if root.is_symlink() or manifest_path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    manifest = read_internal_yaml(manifest_path) if not selected_head else {}
    head = head if selected_head else manifest.get('current_graph')
    expected_access = access_fingerprint(config)
    scopes = _scope_key(config)
    cache_dir = root / 'view-cache'
    if not head:
        key = _view_key('empty', manifest_path.resolve(), manifest_path.stat(), expected_access, scopes)
        return load_graph_view(cache_dir, key, lambda: (
            {'nodes': [], 'edges': [], 'definitions': {}, 'status': 'empty'}, view_token(key)))
    from lightgraph.construction.snapshot_registry import authorize_snapshot, snapshot_id
    policy_record = authorize_snapshot(authorization_root or root, head, config)
    published = _record_path(root, head.get('path'))
    try:
        stat = published.stat()
    except OSError:
        raise LightGraphError('graph_workspace_record_unavailable') from None
    key = _view_key('published', published, stat, expected_access, scopes)
    key['snapshot'] = snapshot_id(head, root=root)
    key['policy'] = head.get('result_policy_id')
    key['graph_digest'] = head.get('graph_digest')
    if published.is_dir():
        try:
            names = ('metadata.yaml', 'nodes.parquet', 'edges.parquet')
            if any((published / name).is_symlink() for name in names):
                raise LightGraphError('invalid_graph_workspace_record')
            key['parquet_files'] = [
                [name, (published / name).stat().st_size, str((published / name).stat().st_mtime_ns)]
                for name in names
            ]
        except OSError:
            raise LightGraphError('graph_workspace_record_unavailable') from None

    # A checkpoint contains exactly the published component named in its
    # commit. A worker publication may additionally have a pinned inherited base.
    compose_base = bool(config.inherited_graphs and head.get('graph_kind') not in
                        {'project_full', 'node_full', 'node_delta', 'node_base'})
    pinned_summary = head.get('inherited_summary')
    if compose_base and 'inherited_summary' in head and not pinned_summary:
        compose_base = False
    if compose_base:
        if pinned_summary is None:
            path = _record_path(root, 'inherited/summary.yaml')
            if not path.is_file():
                raise LightGraphError('inherited_graph_snapshot_not_found')
            pinned_summary = read_internal_yaml(path)
        key['frozen_inheritance'] = view_token({'summary': pinned_summary})
        # Replacements or corruption of frozen constituents also invalidate a cached view.
        stamps = []
        for item in pinned_summary['inherited']:
            path = _record_path(root / 'inherited', item.get('snapshot_file') or (item['inherit_id'] + '.yaml'))
            files = [path / n for n in ('metadata.yaml', 'nodes.parquet', 'edges.parquet')] if path.is_dir() else [path]
            for file in files:
                if file.is_symlink() or not file.is_file():
                    raise LightGraphError('inherited_graph_snapshot_not_found')
                info = file.stat()
                stamps.append([file.name, info.st_size, str(info.st_mtime_ns)])
        key['inherited_files'] = stamps

    def build():
        if published.is_dir():
            from lightgraph.construction.graph_storage import read_graph_record
            graph = read_graph_record(published)
        else:
            graph = read_internal_yaml(published)
        if graph.get('storage_layout') == 'sharded_v1':
            from lightgraph.construction.merger import load_sharded_graph
            graph = load_sharded_graph(root, graph)
        graph.pop('_checkpoint_evidence_records', None)
        if graph.get('access_fingerprint') != head.get('access_fingerprint'):
            raise LightGraphError('graph_access_context_changed')
        if head.get('graph_digest') and published.is_dir():
            if read_internal_yaml(published / 'metadata.yaml').get('graph_digest') != head['graph_digest']:
                raise LightGraphError('graph_parquet_checksum_mismatch')
        if compose_base and graph.get('storage_layout') not in {'sharded_v1', 'parquet_v1'}:
            from lightgraph.construction.inheritance import compose_published_graph, load_frozen_base_graph
            base_graph, _ = load_frozen_base_graph(root, config=config, summary=pinned_summary,
                                                    authorized_digest=(policy_record or {}).get('inheritance_digest'))
            graph = compose_published_graph(graph, base_graph, expected_access)
        graph = filter_graph_scopes(graph, config.scopes)
        graph['version_id'] = snapshot_id(head, root=root)
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


def retrieval_status(config, *, embedding_api_key=None) -> dict:
    """Inspect whether the current published graph has been vectorized (without running embeddings)."""
    from lightgraph.retrieval import resolve_embedder
    from lightgraph.retrieval.sync import active_index
    from lightgraph.retrieval.vector_local import embedder_fingerprint

    root = Path(config.output_dir) / 'construction'
    manifest = root / 'manifest.yaml'
    if root.is_symlink() or manifest.is_symlink():
        raise LightGraphError('workspace_path_invalid')

    graph_nodes = 0
    graph_edges = 0
    has_graph = False
    if manifest.exists():
        graph, generation = load_prepared(config)
        graph_nodes = len(graph.get('nodes', []))
        graph_edges = len(graph.get('edges', []))
        has_graph = graph_nodes > 0 or graph_edges > 0
    else:
        ensure_supported_access(config)
        generation = digest('no-construction-publication', access_fingerprint(config), _scope_key(config))

    emb_cfg = getattr(config, 'embedding', None)
    configured_provider = getattr(emb_cfg, 'provider', 'local_hash')
    configured_model = getattr(emb_cfg, 'model', '') or ('local_hash' if configured_provider == 'local_hash' else '')
    configured_base_url = getattr(emb_cfg, 'base_url', '')

    config_error = None
    expected_tag = None
    try:
        embedder = resolve_embedder(config, api_key=embedding_api_key)
        expected_tag = embedder_fingerprint(embedder)
    except LightGraphError as exc:
        config_error = exc.code

    retrieval_dir = Path(config.output_dir) / 'retrieval'
    current_meta = None
    if (retrieval_dir / 'current.json').is_file():
        try:
            _, current_meta = active_index(retrieval_dir)
        except LightGraphError:
            current_meta = None

    if not has_graph:
        status = 'no_graph'
        stale_reason = None
    elif current_meta is None:
        status = 'stale' if (retrieval_dir / 'current.json').is_file() else 'not_vectorized'
        stale_reason = 'index_incompatible' if status == 'stale' else 'index_missing'
    elif current_meta.get('generation') != generation:
        status = 'stale'
        stale_reason = 'graph_updated'
    elif config_error:
        status = 'stale'
        stale_reason = 'embedding_config_invalid'
    elif expected_tag is not None and current_meta.get('embedder') != expected_tag:
        status = 'stale'
        stale_reason = 'embedder_changed'
    else:
        status = 'vectorized'
        stale_reason = None

    # A stale build can belong to an earlier, broader scope. Only disclose
    # its counts when it matches the current authorized graph.
    if status != 'vectorized':
        current_meta = None
    return {
        'status': status,
        'vectorized': status == 'vectorized',
        'stale_reason': stale_reason,
        'has_graph': has_graph,
        'graph_nodes': graph_nodes,
        'graph_edges': graph_edges,
        'configured_provider': configured_provider,
        'configured_model': configured_model,
        'configured_base_url': configured_base_url,
        'vector_dim': current_meta.get('vector_dim') if current_meta else None,
        'vector_cards': current_meta.get('vector_cards') if current_meta else None,
        'nodes_indexed': current_meta.get('nodes_indexed') if current_meta else None,
        'edges_indexed': current_meta.get('edges_indexed') if current_meta else None,
        'embedder_model': current_meta.get('embedder_model') if current_meta else None,
        'embedder_provider': current_meta.get('embedder_provider') if current_meta else None,
        'synced_at': current_meta.get('synced_at') if current_meta else None,
        'generation': generation,
        'config_error': config_error,
    }


def project_retriever(config, *, refresh=False, embedding_api_key=None):
    """Bind both indexes to the active publication and its current access scope."""
    from lightgraph.retrieval import SubgraphRetriever, resolve_embedder, sync_graph_to_retrieval
    root = Path(config.output_dir) / 'construction'
    manifest = root / 'manifest.yaml'
    if root.is_symlink() or manifest.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    if manifest.exists():
        graph, generation = load_prepared(config)
    else:
        ensure_supported_access(config)
        graph = {'nodes': [], 'edges': []}
        generation = digest('no-construction-publication', access_fingerprint(config), _scope_key(config))
    embedder = resolve_embedder(config, api_key=embedding_api_key)
    directory = Path(config.output_dir) / 'retrieval'
    if not refresh:
        # Queries never start a potentially expensive index build implicitly.
        return SubgraphRetriever(directory, embedder=embedder, expected_generation=generation), None
    if not graph.get('nodes'):
        raise LightGraphError('retrieval_graph_required')
    result = sync_graph_to_retrieval(graph, directory, generation=generation, embedder=embedder)
    return SubgraphRetriever(directory, embedder=embedder, expected_generation=generation), result


def vocabulary(graph):
    definitions = graph.get("definitions", {})
    return {"entity_types": [{"name": e["type"], **({'display_label': e['original_type']} if e.get('original_type') else {}), "description": e.get("description", "")}
                             for e in definitions.get("entities", [])],
            "relations": [{"name": e["type"], **({'display_label': e['original_type']} if e.get('original_type') else {}), "description": e.get("description", "")}
                          for e in definitions.get("relations", [])], "properties": []}


def _listed(item):
    """Pages identify and draw facts. Row evidence stays on the detail read."""
    return listed(item)


def select_business_context(graph, config, selection):
    """Filter supports before traversal, keeping business scope separate from access."""
    if selection is None:
        return graph
    result = filter_graph_scopes(graph, config.scopes, business_context=selection)
    result['nodes'] = [{**n, 'kind': n['type'], 'layer': 'business', 'status': n.get('status', 'accepted')}
                       for n in result.get('nodes', [])]
    result['edges'] = [{**e, 'relation': e['type'], 'status': e.get('status', 'accepted'),
                        'qualifiers': e.get('properties', {})} for e in result.get('edges', [])]
    return result


def context_options(config, *, prepared_graph=None, offset=0, limit=200):
    """Offer only observed, authorized business dimensions, never source credentials."""
    from lightgraph.construction.contexts import support_business_context
    if offset < 0 or not 1 <= limit <= 500:
        raise LightGraphError('graph_selection_invalid')
    graph = prepared_graph if prepared_graph is not None else load_graph(config)
    options = {}
    counts = Counter()
    for item in graph.get('nodes', []) + graph.get('edges', []):
        for support in item.get('supports', []):
            context = support_business_context(support)
            counts[context['status']] += 1
            if context['status'] == 'scoped':
                dimensions = context['dimensions']
                # Single dimensions let a project query span its months/versions.
                for value in [dimensions, *({key: val} for key, val in dimensions.items())]:
                    options[canonical(value)] = value
    ordered = [options[key] for key in sorted(options)]
    return {'version_id': graph.get('version_id'), 'contexts': ordered[offset:offset + limit],
            'total_contexts': len(ordered), 'support_counts': dict(counts),
            'next_offset': offset + limit if offset + limit < len(ordered) else None}


def _search_text(node):
    """Search visible business values; never provenance IDs or hidden supports."""
    values = [node.get("display_label", ""), node.get("label", ""), node.get("kind", "")]
    values.extend(node.get("aliases", []))
    values.extend(node.get("english_names", []))
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
    previews = {focus: canvas_item(by_id[focus])}
    sizes = {focus: len(canonical(previews[focus]).encode('utf-8')) + 1}
    used = sizes[focus]
    while cursor < len(order):
        path, current = set(), order[cursor]
        while current is not None and current not in retained:
            path.add(current)
            current = parents[current]
        if len(retained | path) > limit:
            break
        for node in path:
            if node not in sizes:
                previews[node] = canvas_item(by_id[node])
                sizes[node] = len(canonical(previews[node]).encode('utf-8')) + 1
        extra = sum(sizes[node] for node in path)
        if used + extra > MAX_PAGE_BYTES:
            break
        retained |= path
        used += extra
        cursor += 1
    # A request too small to fit a connecting path must not return a repeating
    # cursor or pretend the disconnected endpoint is a complete neighborhood.
    next_offset = cursor if offset < cursor < len(order) else None
    return [by_id[i] for i in order], [previews[i] for i in order if i in retained], parents, next_offset


def graph_page(config, *, layer="business", offset=0, limit=240, q="", kind="", focus="", hops=1,
               include_candidates=False, traversal='all', prepared_graph=None, business_context=None):
    if traversal not in ('all', 'chains'):
        raise LightGraphError('graph_page_invalid')
    if layer not in {"business", "metadata", "provenance", "all"} or offset < 0 or not 1 <= limit <= 1000 or hops not in {1, 2}:
        raise LightGraphError("graph_selection_invalid")
    graph = prepared_graph if prepared_graph is not None else load_graph(config)
    graph = select_business_context(graph, config, business_context)
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
        page, next_offset, _ = canvas_page(selected, offset, limit)
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
    local_edges, _, _ = canvas_page(page_edges, 0, 3000)
    boundary = sum((e["source"] in ids) != (e["target"] in ids) for e in edges)
    return {"nodes": [canvas_item(node) for node in page], "edges": local_edges, "layer": layer,
            "total_nodes": len(nodes), "total_edges": len(edges), "matched_nodes": len(selected),
            "offset": offset, "page_size": limit, "next_offset": next_offset,
            "truncated": len(page) < len(selected) or len(local_edges) < len(page_edges),
            "edges_truncated": len(local_edges) < len(page_edges), "matched_edges": len(page_edges),
            "boundary_edges": boundary,
            "generation": generation, "version_id": graph.get("version_id"), "query_strategy": "published_yaml", "kinds": sorted(counts),
            "kind_counts": dict(counts), "focus_node_id": focus or None,
            "focus_neighbor_count": max(0, len(selected) - 1) if focus else 0, "focus_hops": hops,
            "focus_traversal": traversal,
            "focus_total_edges": sum(focus in (e['source'], e['target']) for e in edges) if focus else None,
            "focus_loaded_edges": sum(focus in (e['source'], e['target']) for e in local_edges) if focus else None,
            "view_scope": "neighborhood" if focus else "search_results" if q or kind else "graph_page",
            "focus_filters_ignored": bool(focus and (q or kind)),
            "neighborhood_truncated": bool(focus and (len(page) < len(selected) or len(local_edges) < len(page_edges))),
            "business_context": business_context,
            "include_candidates": include_candidates, "candidate_view_available": True,
            "alignment_candidates": graph.get('candidate_identity_merges', [])[:200] if include_candidates else [],
            "definition_conflicts": graph.get('definition_conflicts', [])[:200] if include_candidates else [],
            "pending_alignment_count": len(graph.get('candidate_identity_merges', [])),
            "pending_definition_count": len(graph.get('definition_conflicts', [])),
            "unresolved_relations": [_listed(item) for item in graph.get('unresolved_relations', [])[:200]] if include_candidates else [],
            "pending_relation_count": len(graph.get('unresolved_relations', [])),
            "context_cycle_diagnostics": graph.get('context_cycle_diagnostics', [])[:200] if include_candidates else [],
            "display_schema": vocabulary(graph), "semantic_origin": "agent_authored", "complete_batches": True}


def _catalog_tables_for_config(config):
    output = Path(getattr(config, 'output_dir', '') or '')
    if not str(output):
        return []
    catalog_path = output / 'physical_catalog.yaml'
    if not catalog_path.is_file() or catalog_path.is_symlink():
        return []
    try:
        payload = read_internal_yaml(catalog_path)
        return list(payload.get('tables') or []) if isinstance(payload, dict) else []
    except Exception:
        return []


def graph_details(config, *, entity_id=None, fact_id=None, offset=0, include_candidates=False, prepared_graph=None,
                  business_context=None):
    from lightgraph.construction.contexts import business_context_identity
    if bool(entity_id) == bool(fact_id) or offset < 0:
        raise LightGraphError("graph_selection_invalid")
    graph = prepared_graph if prepared_graph is not None else load_graph(config)
    graph = select_business_context(graph, config, business_context)
    selected = []
    supports = []

    def add(identifier, kind, payload, provenance, *, status='accepted'):
        if fact_id and identifier != fact_id:
            return
        selected.append({"id": identifier, "fact_kind": kind, "status": status, "payload": payload})
        for index, source in enumerate(provenance):
            binding = source.get("source", {})
            location = ".".join(str(binding.get(key, "")) for key in ("source_id", "schema", "table") if binding.get(key))
            support_id = digest(identifier, index)
            origin = {'authorized_catalog': 'metadata', 'agent_declared_semantics': 'agent_declared'}.get(
                source.get('origin'), 'source_query')
            supports.append({"id": support_id, "support_id": support_id, "fact_id": identifier,
                             "status": status, "origin": origin,
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
                for key in ('row_context', 'business_context', 'observation_id', 'execution_origins', 'identity_origins'):
                    if key in support:
                        origin[key] = support[key]
                mapped = [obs for obs in support.get('property_observations', [])
                          if obs['property'] == name and canonical(obs['value']) == canonical(value)]
                if mapped:
                    origin['property_observations'] = mapped
                if origin not in origins:
                    origins.append(origin)
        return origins

    def support_origins(item):
        if 'supports' not in item:
            return item.get('provenance', [])
        return [{**support.get('provenance', {}), 'scope_ref': support['scope_ref'],
                 **{key: support[key] for key in ('row_context', 'business_context', 'lookup_matches', 'required_scope_refs', 'inherited_from',
                     'observation_id', 'execution_origins', 'endpoint_origins')
                    if support.get(key) is not None}}
                for support in item['supports']]

    if entity_id or fact_id:
        nodes = [n for n in graph["nodes"] if not entity_id or n["id"] == entity_id]
        if entity_id and not nodes:
            raise LightGraphError("graph_item_not_found")
        for node in nodes:
            identifier = node["id"]
            add(identifier, "NodeExistence", {"entity_id": identifier, "kind": node["kind"],
                **({'identity_context': node['identity_context']} if 'identity_context' in node else {})}, support_origins(node))
            properties = {**node.get("properties", {}), "label": node.get("label_variants") or [node["label"]], "alias": node.get("aliases", [])}
            for name, values in properties.items():
                for value in values:
                    origins = property_origins(node, name, value)
                    groups = {}
                    for origin in origins:
                        # Physical column bindings belong to the citation. Equal
                        # applicability and value form one fact across sources.
                        context = business_context_identity(origin) if 'business_context' in origin else None
                        groups.setdefault(canonical(context), (context, []))[1].append(origin)
                    for context, sources in groups.values() if groups else [(None, origins)]:
                        # The same value under M and K is two assertions. A source
                        # list alone must not erase the condition that made it true.
                        fact = digest(identifier, name, value, context) if context is not None else digest(identifier, name, value)
                        add(fact, "PropertyValue", {"entity_id": identifier,
                            "name": name, "value": value,
                            **({'business_context': context} if context is not None else {})}, sources)
    for edge in graph["edges"]:
        if edge["id"] == fact_id or entity_id and entity_id in {edge["source"], edge["target"]}:
            add(edge["id"], "Edge", {"source": edge["source"], "target": edge["target"],
                "relation": edge["relation"], "qualifiers": edge.get("properties", {}),
                **({'business_context': edge['business_context']} if 'business_context' in edge else {}),
                'observations': [{k: s[k] for k in ('business_context', 'properties', 'row_context', 'observation_id') if k in s}
                                 for s in edge.get('supports', [])]}, support_origins(edge))
    if include_candidates or fact_id:
        for pending in graph.get('unresolved_relations', []):
            if pending['id'] == fact_id or entity_id and entity_id in pending.get('node_ids', []):
                add(pending['id'], 'UnresolvedRelation', {
                    'reason': pending.get('reason'), 'candidate_endpoints': pending.get('candidate_endpoints', {}),
                    'observations': [{k: s[k] for k in ('business_context', 'properties', 'row_context', 'observation_id') if k in s}
                                     for s in pending.get('supports', [])]}, support_origins(pending), status='unresolved')
    if not selected:
        raise LightGraphError("graph_item_not_found")
    facts = selected[offset:offset + 50]
    fact_ids = {f["id"] for f in facts}
    endpoints = {f["payload"].get(k) for f in facts for k in ("source", "target", "entity_id")}
    endpoints.update(nid for f in facts for ids in f['payload'].get('candidate_endpoints', {}).values() for nid in ids)
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
            "label_column": source.get("label_column"),
            **({"english_name_column": source["english_name_column"]} if source.get("english_name_column") else {}),
            **({"description_column": source["description_column"]} if source.get("description_column") else {}),
            "scope_ref": support["scope_ref"],
            "constant_properties": source.get('constant_properties', {}),
            "context_columns": source.get('context_columns', []),
            **({"metadata_endpoint": source['metadata_endpoint']} if 'metadata_endpoint' in source else {}),
            **({"source_kind": source['source_kind'], "evidence_refs": source.get('evidence_refs', [])}
               if source.get('source_kind') == 'metadata_column' else {})}
    from lightgraph.construction.node_standardizer import standardize_node_details
    catalog_tables = _catalog_tables_for_config(config) if prepared_graph is None else []
    definitions = graph.get("definitions", {})
    standard_nodes = [
        standardize_node_details(node, definitions=definitions, catalog_tables=catalog_tables)
        for node in graph["nodes"] if node["id"] in endpoints
    ]
    return {"version_id": graph.get("version_id"), "business_context": business_context,
            "facts": facts, "supports": retained[:500], "rules": list(mappings.values()),
            "nodes": [_listed(n) for n in graph["nodes"] if n["id"] in endpoints],
            "standard_nodes": standard_nodes,
            "display_schema": vocabulary(graph),
            "total_facts": len(selected), "total_supports": len(retained), "supports_truncated": len(retained) > 500,
            "total_incident_edges": sum(entity_id in (e['source'], e['target']) for e in graph['edges']) if entity_id else None,
            "next_offset": offset + len(facts) if offset + len(facts) < len(selected) else None,
            "include_candidates": include_candidates, "semantic_origin": "agent_authored"}

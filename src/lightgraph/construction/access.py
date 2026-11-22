"""Program-owned result visibility, separate from source connection permissions."""
from copy import deepcopy
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from .display import derive_display_graph
from .contexts import business_context_matches, normalize_context_selection


def ensure_supported_access(config) -> None:
    """Validate explicit bindings without changing the legacy scope selection."""
    scope_binding_map(config.scope_bindings)


def scope_binding_map(bindings):
    result = {}
    for binding in bindings:
        value = binding.model_dump(mode='python', by_alias=True) if hasattr(binding, 'model_dump') else binding
        key = (value['source_id'], value.get('schema', 'public'), value['table'])
        columns = list(value.get('columns', []))
        if key in result and result[key] != columns:
            raise LightGraphError('scope_binding_conflict')
        result[key] = columns
    return result


def support_scope(row, columns):
    """Preserve the previous explicit namespace-to-scope serialization."""
    if not columns:
        return 'public'
    if len(columns) == 1:
        return str(row[columns[0]])
    return canonical({column: row[column] for column in columns})


def access_fingerprint(config) -> str:
    """Hash value-output permissions without retaining credential values.

    Read scopes select individual supports and can change without recomputation.
    Source authorization, isolation and the trusted scope mapping cannot.
    """
    def selection(values):
        return sorted(set(values))

    goal = config.goal
    sources = []
    for source in config.sources:
        sources.append({
            **source.file_scope_revision,
            'id': source.id, 'kind': source.kind, 'path': source.path,
            'allowed_tables': selection(source.allowed_tables),
            'allowed_columns': None if source.allowed_columns is None else {
                table: selection(columns) for table, columns in source.allowed_columns.items()},
            'credential_revision': source.credential_revision,
        })
    return digest({
        'isolation_domain': config.isolation_domain,
        **({'inherited_graphs': [ref.model_dump(mode='json') for ref in config.inherited_graphs]}
           if getattr(config, 'inherited_graphs', None) else {}),
        'document_scopes': selection(config.document_scopes),
        'scope_bindings': [{'source_id': key[0], 'schema': key[1], 'table': key[2], 'columns': columns}
                           for key, columns in sorted(scope_binding_map(config.scope_bindings).items())],
        'goal': {name: selection(getattr(goal, name)) for name in (
            'source_scope', 'excluded_columns', 'evidence_only_columns', 'entity_kinds', 'relation_kinds')},
        'sources': sorted(sources, key=lambda source: (source['id'], source['kind'], source['path'] or '')),
    })


def filter_graph_scopes(graph, scopes, *, external_visible_ids=None, business_context=None):
    """Authorize each existence, label, alias, property and edge before display.

    Aggregated convenience fields are never trusted to be visible. Rebuild them
    exclusively from allowed supports, including when a node spans many scopes.
    """
    if not isinstance(scopes, (list, tuple, set, frozenset)) or any(not isinstance(s, str) for s in scopes):
        raise LightGraphError('explicit_scope_set_required')
    selection = normalize_context_selection(business_context)
    allowed = set(scopes)
    if graph.get('support_visibility_version') != 1:
        if graph.get('nodes') or graph.get('edges'):
            raise LightGraphError('graph_support_visibility_missing')
        return deepcopy(graph)

    def supports_for(item):
        return [deepcopy(support) for support in item.get('supports', [])
                if support.get('scope_ref') in allowed
                and all(scope in allowed for scope in support.get('required_scope_refs', []))
                and business_context_matches(support, selection)]

    def attributes(supports):
        properties = {}
        provenance = []
        for support in supports:
            for name, values in support.get('properties', {}).items():
                selected = properties.setdefault(name, [])
                for value in values:
                    if canonical(value) not in {canonical(v) for v in selected}:
                        selected.append(value)
            origin = {**support.get('provenance', {}), 'scope_ref': support['scope_ref']}
            if origin not in provenance:
                provenance.append(origin)
        for values in properties.values():
            values.sort(key=canonical)
        return properties, sorted(provenance, key=canonical)

    nodes = []
    merged_observation_nodes = 0
    for node in graph.get('nodes', []):
        supports = supports_for(node)
        if not supports:
            continue
        if len({origin['contributor_id']
                for support in supports if support.get('identity_basis') in {
                    'observed_source_key_v1', 'observed_dictionary_key_v1', 'observed_hierarchy_projection_v1'}
                for origin in support.get('execution_origins') or [support] if origin.get('contributor_id')}) > 1:
            merged_observation_nodes += 1
        labels = sorted({support['label'] for support in supports if support.get('label') is not None})
        aliases = sorted({alias for support in supports for alias in support.get('aliases', [])})
        english_names = sorted({support['english_name'] for support in supports
                                if isinstance(support.get('english_name'), str) and support['english_name'].strip()})
        descriptions = sorted({support['description'] for support in supports
                               if isinstance(support.get('description'), str) and support['description'].strip()})
        properties, provenance = attributes(supports)
        extra = {}
        if english_names:
            extra['english_names'] = english_names
        if descriptions:
            extra['descriptions'] = descriptions
        nodes.append({**_view_shell(node), 'label': labels[0] if labels else '',
            'aliases': aliases, 'properties': properties, 'provenance': provenance,
            'supports': supports, 'scopes': sorted({support['scope_ref'] for support in supports}),
            **extra})
    visible = {node['id'] for node in nodes}
    external_visible = external_visible_ids if isinstance(external_visible_ids, (set, frozenset)) else set(external_visible_ids or ())
    edges = []
    for edge in graph.get('edges', []):
        if ((edge['source'] not in visible and edge['source'] not in external_visible)
                or (edge['target'] not in visible and edge['target'] not in external_visible)):
            continue
        supports = supports_for(edge)
        if not supports:
            continue
        properties, provenance = attributes(supports)
        edges.append({**_view_shell(edge), 'properties': properties, 'provenance': provenance,
                      'supports': supports, 'scopes': sorted({support['scope_ref'] for support in supports})})
    entity_types = {node['type'] for node in nodes}
    relation_types = {edge['type'] for edge in edges}
    node_ids, edge_ids = {node['id'] for node in nodes}, {edge['id'] for edge in edges}
    unresolved = []
    for item in graph.get('unresolved_relations', []):
        summary = _visible_candidates([item], node_ids, edge_ids)
        supports = supports_for(item) if summary else []
        if not supports:
            continue
        endpoints = {name: [nid for nid in ids if nid in node_ids]
                     for name, ids in item.get('candidate_endpoints', {}).items()
                     if name in ('source', 'target')}
        properties, provenance = attributes(supports)
        unresolved.append({**summary[0], 'id': item['id'], 'type': item.get('type'),
                           'candidate_endpoints': endpoints, 'supports': supports,
                           'properties': properties, 'provenance': provenance})
    definitions = graph.get('definitions', {})
    # The published document is not reused. Copy only the small header; nodes and
    # edges above already own their supports. A second full copy dominated large reads.
    # Merge reports and validation records cover the complete run. Keep the
    # reports on the original artifact; the viewer gets only evidence referenced
    # by the supports it can read.
    report_fields = {'conflict_isolation', 'candidate_identity_merges', 'candidate_column_alignments', 'definition_conflicts',
                     'unresolved_relations', 'context_cycle_diagnostics',
                     'evidence_catalog', 'evidence_refs', 'validation_snapshots', 'read_window',
                     'quality_diagnostics', 'fusion_diagnostics', 'fusion_policy'}
    view = _view_shell(graph, omit=report_fields)
    refs = set()
    for item in nodes + edges + unresolved:
        for support in item['supports']:
            prov = support.get('provenance', {})
            endpoint = prov.get('metadata_endpoint', {}).get('column', {})
            for origin in (support, prov, endpoint):
                refs.update(origin.get('evidence_refs', []))
    if 'evidence_refs' in graph or 'evidence_catalog' in graph:
        view['evidence_refs'] = sorted(refs)
        if 'evidence_catalog' in graph:
            # Individual records can themselves contain values from other scopes.
            # Export only their namespaced references and origin identifiers.
            catalog = graph['evidence_catalog']
            view['evidence_catalog'] = {ref: {k: v for k, v in catalog[ref].items()
                if k in ('contributor_id', 'kind', 'original_ref')}
                for ref in sorted(refs.intersection(catalog))}
    if 'conflict_isolation' in graph:
        view['conflict_isolation'] = {k: v for k, v in graph['conflict_isolation'].items()
            if k in ('nodes', 'edges', 'candidate_retained', 'policy', 'coverage')}
    # Candidate evidence has the same read boundary as facts. A partially
    # visible pair is not safe to disclose, even as a count or a type name.
    for field in ('candidate_identity_merges', 'definition_conflicts', 'context_cycle_diagnostics'):
        if field in graph:
            view[field] = _visible_candidates(graph[field], node_ids, edge_ids)
    if 'unresolved_relations' in graph:
        # Detail readers can inspect authorized witnesses, while transport
        # previews strip supports. These records never become accepted edges.
        view['unresolved_relations'] = unresolved
    if selection is not None:
        view['business_context'] = selection
    view['merged_observation_nodes'] = merged_observation_nodes
    view['nodes'] = nodes
    view['edges'] = edges
    view['definitions'] = {'entities': [deepcopy(item) for item in definitions.get('entities', []) if item['type'] in entity_types],
                           'relations': [deepcopy(item) for item in definitions.get('relations', []) if item['type'] in relation_types]}
    for definition in view['definitions']['entities']:
        if 'identity_evidence_refs' in definition:
            definition['identity_evidence_refs'] = sorted(set(definition['identity_evidence_refs']) & refs)
    return derive_display_graph(view, copy=False)


def _visible_candidates(candidates, node_ids, edge_ids):
    """Expose IDs and fixed diagnostics, never run-wide source payloads."""
    result = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        ids = candidate.get('node_ids', [])
        links = candidate.get('edge_ids', [])
        if (not isinstance(ids, list) or not ids or not all(isinstance(nid, str) for nid in ids)
                or not isinstance(links, list) or not all(isinstance(eid, str) for eid in links)
                or not set(ids).issubset(node_ids) or not set(links).issubset(edge_ids)):
            continue
        result.append({key: deepcopy(candidate[key]) for key in (
            'status', 'reason', 'category', 'node_ids', 'edge_ids', 'definition_ids') if key in candidate})
    return result


def _view_shell(item, *, omit=()):
    """Copy identity and metadata without cloning support evidence again."""
    return deepcopy({key: value for key, value in item.items()
                     if key not in omit and key not in {'nodes', 'edges', 'supports', 'provenance', 'properties', 'aliases',
                         'english_names', 'descriptions', 'display_label', 'label_origin', 'qualifiers',
                         'label', 'label_variants', 'label_conflict', 'scopes', 'definitions'}})

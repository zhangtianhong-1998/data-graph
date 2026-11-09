"""Program-owned result visibility, separate from source connection permissions."""
from copy import deepcopy
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from .display import derive_display_graph


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
            'id': source.id, 'kind': source.kind, 'path': source.path,
            'allowed_tables': selection(source.allowed_tables),
            'allowed_columns': None if source.allowed_columns is None else {
                table: selection(columns) for table, columns in source.allowed_columns.items()},
            'credential_revision': source.credential_revision,
        })
    return digest({
        'isolation_domain': config.isolation_domain,
        'document_scopes': selection(config.document_scopes),
        'scope_bindings': [{'source_id': key[0], 'schema': key[1], 'table': key[2], 'columns': columns}
                           for key, columns in sorted(scope_binding_map(config.scope_bindings).items())],
        'goal': {name: selection(getattr(goal, name)) for name in (
            'source_scope', 'excluded_columns', 'evidence_only_columns', 'entity_kinds', 'relation_kinds')},
        'sources': sorted(sources, key=lambda source: (source['id'], source['kind'], source['path'] or '')),
    })


def filter_graph_scopes(graph, scopes):
    """Authorize each existence, label, alias, property and edge before display.

    Aggregated convenience fields are never trusted to be visible. Rebuild them
    exclusively from allowed supports, including when a node spans many scopes.
    """
    if not isinstance(scopes, (list, tuple, set, frozenset)) or any(not isinstance(s, str) for s in scopes):
        raise LightGraphError('explicit_scope_set_required')
    allowed = set(scopes)
    if graph.get('support_visibility_version') != 1:
        if graph.get('nodes') or graph.get('edges'):
            raise LightGraphError('graph_support_visibility_missing')
        return deepcopy(graph)

    def supports_for(item):
        return [deepcopy(support) for support in item.get('supports', [])
                if support.get('scope_ref') in allowed
                and all(scope in allowed for scope in support.get('required_scope_refs', []))]

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
    for node in graph.get('nodes', []):
        supports = supports_for(node)
        if not supports:
            continue
        labels = sorted({support['label'] for support in supports if support.get('label') is not None})
        aliases = sorted({alias for support in supports for alias in support.get('aliases', [])})
        properties, provenance = attributes(supports)
        nodes.append({**_view_shell(node), 'label': labels[0] if labels else '',
            'aliases': aliases, 'properties': properties, 'provenance': provenance,
            'supports': supports, 'scopes': sorted({support['scope_ref'] for support in supports})})
    visible = {node['id'] for node in nodes}
    edges = []
    for edge in graph.get('edges', []):
        if edge['source'] not in visible or edge['target'] not in visible:
            continue
        supports = supports_for(edge)
        if not supports:
            continue
        properties, provenance = attributes(supports)
        edges.append({**_view_shell(edge), 'properties': properties, 'provenance': provenance,
                      'supports': supports, 'scopes': sorted({support['scope_ref'] for support in supports})})
    entity_types = {node['type'] for node in nodes}
    relation_types = {edge['type'] for edge in edges}
    definitions = graph.get('definitions', {})
    # The published document is not reused. Copy only the small header; nodes and
    # edges above already own their supports. A second full copy dominated large reads.
    view = _view_shell(graph)
    if 'conflict_isolation' in view:
        # Quarantine IDs are internal merge state, not authorized visible nodes.
        view['conflict_isolation'].pop('node_ids', None)
        view['conflict_isolation'].pop('edge_ids', None)
    view['nodes'] = nodes
    view['edges'] = edges
    view['definitions'] = {'entities': [deepcopy(item) for item in definitions.get('entities', []) if item['type'] in entity_types],
                           'relations': [deepcopy(item) for item in definitions.get('relations', []) if item['type'] in relation_types]}
    return derive_display_graph(view, copy=False)


def _view_shell(item):
    """Copy identity and metadata without cloning support evidence again."""
    return deepcopy({key: value for key, value in item.items()
                     if key not in {'nodes', 'edges', 'supports', 'provenance', 'properties', 'aliases', 'label', 'scopes', 'definitions'}})

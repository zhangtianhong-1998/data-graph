"""Catalog-column graph objects: physical identity and explicit semantic claims.

This operator never enumerates a column's fact values. The catalog is observed;
names and meaning supplied by the Agent remain separately declared evidence.
"""
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.query.predicate import combine_scope_predicates
from .models import COLUMN_IDENTITY_NAMES, MetadataColumnMapping


def physical_column_key(mapping):
    return [mapping.source_id, mapping.schema_name, mapping.table, mapping.column]


def metadata_endpoint(mapping):
    for end in ('source', 'target'):
        endpoint = getattr(mapping, end + '_metadata', None)
        if endpoint is not None:
            return end, endpoint
    return None, None


def mapping_predicate(mapping):
    """Push existence evidence into SQL, never return measure values as identity."""
    _, endpoint = metadata_endpoint(mapping)
    if endpoint is not None and mapping.metadata_basis == 'observed_non_null':
        return combine_scope_predicates(mapping.predicate, {'column': endpoint.column, 'op': 'is_not_null'})
    return mapping.predicate


def checked_column(mapping, catalog, goal=None):
    table = catalog.get((mapping.source_id, mapping.schema_name, mapping.table))
    if table is None:
        raise LightGraphError('unauthorized_object')
    column = next((item for item in table.columns if item.name == mapping.column), None)
    if column is None:
        raise LightGraphError('unauthorized_column')
    if goal is not None:
        from lightgraph.discovery.goal import column_is_evidence_only, column_is_excluded, source_in_goal
        if not source_in_goal(goal, table):
            raise LightGraphError('outside_source_goal')
        if column_is_excluded(goal, table, column.name):
            raise LightGraphError('excluded_column_projected')
        if column_is_evidence_only(goal, table, column.name):
            raise LightGraphError('evidence_only_column_projected')
    return table, column


def column_node(entity, index, mapping, catalog, goal=None):
    table, column = checked_column(mapping, catalog, goal)
    key = physical_column_key(mapping)
    catalog_label = column.comment.strip() or column.name
    catalog_basis = 'catalog_comment' if column.comment.strip() else 'column_name'
    origin = {'definition': entity.type, 'mapping': index,
              'source': {'source_id': table.source_id, 'schema': table.schema_name, 'table': table.name},
              'columns': [column.name], 'scope_columns': [], 'source_kind': 'metadata_column',
              'evidence_refs': mapping.evidence_refs}
    catalog_properties = {'physical_column': [column.name], 'data_type': [column.data_type],
                          'nullable': [column.nullable], 'catalog_comment': [column.comment]}
    # Catalog existence does not depend on fact scope values. Semantic annotations
    # and explicit names are independently attributable to declared evidence.
    catalog_support = {'scope_ref': 'public', 'provenance': {**origin, 'origin': 'authorized_catalog'},
        'properties': catalog_properties, 'label': None if mapping.label else catalog_label,
        'aliases': [], 'source_kind': 'catalog', 'label_basis': catalog_basis}
    supports = [catalog_support]
    if mapping.label or mapping.aliases or mapping.semantic_annotations:
        supports.append({'scope_ref': 'public',
            'provenance': {**origin, 'origin': 'agent_declared_semantics'},
            'properties': {name: [value] for name, value in mapping.semantic_annotations.items()},
            'label': mapping.label, 'aliases': sorted(set(mapping.aliases)),
            'source_kind': 'declared', 'label_basis': 'agent_declared' if mapping.label else None,
            'evidence_refs': mapping.evidence_refs})
    properties = {key: list(values) for key, values in catalog_properties.items()}
    for name, value in mapping.semantic_annotations.items():
        if name in catalog_properties:
            raise LightGraphError('metadata_annotation_catalog_conflict')
        properties[name] = [value]
    return {'id': 'n_' + digest(entity.type, key), 'type': entity.type,
            'identity': dict(zip(COLUMN_IDENTITY_NAMES, key)), 'identity_origin': 'physical_column',
            'label': mapping.label or catalog_label, 'label_observed': bool(mapping.label or catalog_label),
            'aliases': sorted(set(mapping.aliases)), 'properties': properties,
            'provenance': [support['provenance'] for support in supports], 'supports': supports}


def is_metadata_mapping(mapping):
    return isinstance(mapping, MetadataColumnMapping)


def merge_column_supports(existing, incoming):
    """Repeated column bindings add evidence, not a second identity.

    A declared name may refine a catalog fallback. Two different declared
    primary names or conflicting annotations still require Agent revision.
    """
    supports = existing['supports'] + incoming['supports']
    labels = {s['label'] for s in supports if s.get('source_kind') == 'declared' and s.get('label')}
    if len(labels) > 1 or any(existing['properties'][name] != values
            for name, values in incoming['properties'].items() if name in existing['properties']):
        raise LightGraphError('metadata_column_mapping_conflict')
    for name in ('supports', 'provenance', 'aliases'):
        for item in incoming[name]:
            if item not in existing[name]:
                existing[name].append(item)
    existing['properties'].update(incoming['properties'])
    if labels:
        existing['label'] = next(iter(labels))
        for support in existing['supports']:
            if support.get('source_kind') == 'catalog':
                support['label'] = None

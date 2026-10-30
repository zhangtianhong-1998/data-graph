"""Declared, bounded concept projection with explicit joint-context bindings.

The ordinary rule engine owns authorization, complete projection, snapshot,
staging and rollback. This operator never guesses a separator or executes code.
"""
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError


def component_value(component, row):
    value = row.get(component.column)
    if not isinstance(value, str) or not value:
        raise LightGraphError('concept_value_missing')
    if component.delimiter is not None:
        parts = value.split(component.delimiter)
        if len(parts) != component.segment_count or any(not part.strip() for part in parts):
            raise LightGraphError('concept_segment_mismatch')
        value = parts[component.position]
    # Whitespace is semantic unless a future explicit normalization permits it.
    return value


def emit_concepts(engine, publication, rule, rows):
    spec = rule.concept_mapping
    concepts = set()
    contexts = set()
    seen = {}
    definitions = {}
    for row in rows:
        if any(row.get(c) in (None, '') for c in rule.scope.namespaces):
            raise LightGraphError('null_identity_namespace')
        key = tuple(row.get(c) for c in rule.identity.key_columns)
        values = [component_value(component, row) for component in spec.components]
        keys = [row.get(c.key_column) if c.key_column else value for c,value in zip(spec.components,values)]
        if any(not isinstance(key,str) or not key for key in keys):
            raise LightGraphError('concept_key_missing')
        signature = canonical([row.get(c) for c in rule.scope.namespaces] + list(key))
        assignments = list(zip(keys,values))
        previous = seen.setdefault(signature, assignments)
        if previous != assignments:
            raise LightGraphError('concept_binding_conflict')
        # One source identity owns the joint assignment. Independent concept
        # edges never imply unobserved combinations of product and channel.
        context, binding, scope = engine._emit_entity(
            publication, rule, row, key, label='组合上下文',
            binding_context=digest('concept_binding',spec.model_dump(mode='json'),assignments))
        contexts.add(context)
        for component, value, concept_key in zip(spec.components, values, keys):
            namespaces = rule.scope.namespaces if component.namespace_columns is None else component.namespace_columns
            namespace_values = [row.get(c) for c in namespaces]
            semantic_key = canonical([component.entity_kind,component.role,component.definition_ref,namespace_values,concept_key])
            if definitions.setdefault(semantic_key,value) != value:
                raise LightGraphError('concept_key_label_conflict')
            resolution = engine.store.identity.resolve(
                kind=component.entity_kind,
                source_ref=f'{rule.source.source_id}.{rule.source.schema_name}.{rule.source.table}',
                namespace=canonical([component.role, component.definition_ref, namespaces,namespace_values]),
                business_key=canonical(concept_key), definition={'label': value},
                isolation_domain=engine.domain, definition_version='',
                identity_domain=component.identity_domain or 'concept:' + canonical([component.role, component.definition_ref]),
                # Ordinary entity rules encode the business key as a list. An
                # explicit shared domain must use that exact contract; legacy
                # definition-local concepts keep their historical key shape.
                verified_key=[namespace_values, [concept_key] if component.identity_domain else concept_key],
                identity_evidence=tuple(component.identity_evidence) if component.identity_domain else (spec.evidence_ref,))
            eid = resolution.entity_id
            concepts.add(eid)
            engine._stage(publication, rule, resolution.binding_id, scope, 'NodeExistence',
                {'entity_id': eid, 'kind': component.entity_kind, 'layer': 'business'})
            engine._stage(publication, rule, resolution.binding_id, scope, 'PropertyValue',
                {'entity_id': eid, 'name': 'label', 'value': value})
            if component.key_column:
                engine._stage(publication, rule, resolution.binding_id, scope, 'PropertyValue',
                    {'entity_id': eid, 'name': 'business_code', 'value': concept_key})
            engine._stage(publication, rule, binding, scope, 'Edge',
                {'source': context, 'target': eid, 'relation': component.relation})
    return {'entities': len(concepts) + len(contexts), 'concept_entities': len(concepts),
            'context_entities': len(contexts), 'projected_tuples': len(rows)}, []

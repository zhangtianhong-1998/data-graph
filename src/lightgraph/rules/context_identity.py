"""Detect competing context interpretations without changing persisted identities."""
from lightgraph.contracts import canonical


def _component_domain(component):
    # A verified code domain, not the document revision proving it, owns identity.
    return component.identity_domain or ['concept', component.role, component.definition_ref]


def _projection_contract(rule):
    """Physical selectors permit comparison without reading or retaining row values."""
    components = []
    for component in rule.concept_mapping.components:
        selector = ({'column': component.key_column} if component.key_column else
                    {'column': component.column, 'delimiter': component.delimiter,
                     'position': component.position, 'segment_count': component.segment_count})
        components.append({
            'role': component.role, 'kind': component.entity_kind,
            'domain': _component_domain(component), 'selector': selector,
            'namespaces': (rule.scope.namespaces if component.namespace_columns is None
                           else component.namespace_columns),
        })
    return canonical(sorted(components, key=canonical))


def competing_context_rules(rule, existing_rules):
    """Return alternative interpretations requiring revision, never implicit merging.

    Equal source keys across these operators do not prove that a plain entity is
    a concept binding. They do establish a competing interpretation worth
    resolving before publishing another graph. Predicate differences cannot
    prove disjointness, so they do not bypass this check.
    """
    if rule.kind not in {'column_group_entity', 'concept_mapping'}:
        return []
    matches = []
    for other in existing_rules:
        if other.rule_id == rule.rule_id or other.kind not in {'column_group_entity', 'concept_mapping'}:
            continue
        if (other.source != rule.source or other.identity.entity_kind != rule.identity.entity_kind
                or set(other.scope.namespaces) != set(rule.scope.namespaces)
                or set(other.identity.key_columns) != set(rule.identity.key_columns)):
            continue
        if other.kind != rule.kind:
            matches.append(other)
        elif rule.kind == 'concept_mapping' and _projection_contract(rule) == _projection_contract(other):
            # The historical execution namespace includes this complete spec.
            # Do not migrate those immutable identities during admission. A
            # renamed predicate, new evidence locator or attribute mapping must
            # revise the original rule instead of minting a parallel graph.
            if (canonical(rule.concept_mapping) != canonical(other.concept_mapping)
                    or canonical(rule.identity) != canonical(other.identity)
                    or rule.scope.namespaces != other.scope.namespaces
                    or canonical(rule.normalization) != canonical(other.normalization)):
                matches.append(other)
    return sorted(matches, key=lambda item: (item.rule_id, item.revision))

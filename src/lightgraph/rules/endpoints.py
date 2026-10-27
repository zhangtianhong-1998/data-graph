"""Map an explicit relationship endpoint to the rule's physical and identity keys."""
from lightgraph.errors import LightGraphError


def endpoint_columns(rule, level=None):
    """Wide hierarchy references must identify one declared level, never guess it."""
    if rule.kind == 'wide_hierarchy':
        if level is None:
            raise LightGraphError('reference_hierarchy_level_required')
        selected = next((item for item in rule.hierarchy.levels if item.level == level), None)
        if selected is None:
            raise LightGraphError('reference_hierarchy_level_unknown')
        return list(rule.scope.namespaces) + [selected.code]
    if level is not None:
        raise LightGraphError('reference_level_on_nonhierarchy')
    if rule.kind not in ('column_group_entity', 'long_hierarchy'):
        raise LightGraphError('reference_endpoint_kind_unsupported')
    return list(rule.identity.key_columns)


def endpoint_identity_key(rule, values, level=None):
    columns = endpoint_columns(rule, level)
    if len(values) != len(columns):
        raise LightGraphError('reference_arity_mismatch')
    values = tuple(values)
    if rule.kind == 'wide_hierarchy' and rule.identity.level_policy == 'level_qualified':
        return values[:-1] + (level, values[-1])
    return values

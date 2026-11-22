"""Persist the declared catalog key behind a row entity's ordered source binding."""
from lightgraph.discovery.goal import column_is_evidence_only, column_is_excluded, source_in_goal


def catalog_identity_reference(mapping, catalog, goal=None):
    """Return one authorized dictionary address, without claiming to have read a dictionary row.

    Use complete table constraints. A per-column primary-key flag can survive
    after another component of a composite primary key has been hidden.
    """
    local = catalog[(mapping.source_id, mapping.schema_name, mapping.table)]
    columns = mapping.key_columns
    if not columns or len(columns) != len(set(columns)) or not set(columns) <= set(local.column_names):
        return None

    target, target_columns = local, columns
    kind = 'unique_key'
    if columns not in local.unique_keys:
        # A different key order is a different positional mapping. Several
        # constraints on the same columns may point into different domains.
        targets = {(fk.target_schema, fk.target_table, tuple(fk.target_columns))
                   for fk in local.foreign_keys if fk.columns == columns}
        if len(targets) != 1:
            return None
        schema, table, ordered = targets.pop()
        target = catalog.get((local.source_id, schema, table))
        target_columns = list(ordered)
        if (target is None or len(ordered) != len(columns) or len(set(ordered)) != len(ordered)
                or not set(ordered) <= set(target.column_names) or target_columns not in target.unique_keys):
            return None
        kind = 'foreign_key'
    if goal is not None and (not source_in_goal(goal, target) or any(
            column_is_excluded(goal, target, col) or column_is_evidence_only(goal, target, col)
            for col in target_columns)):
        return None
    return {'kind': kind, 'local_columns': list(columns), 'target': {
        'source_id': target.source_id, 'schema': target.schema_name, 'table': target.name,
        'columns': list(target_columns)}}

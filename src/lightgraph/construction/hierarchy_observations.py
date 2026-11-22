"""Compare source observations without changing the evidence stored on a member or edge."""
import re
from typing import NamedTuple

from lightgraph.contracts import canonical

from .contexts import business_context_identity


class ObservationBoundary(NamedTuple):
    scope: str
    row_context: str
    predicate: tuple


def _predicate_terms(predicate, nonnull):
    """Normalize conjunction on this particular observation. Other filters stay binding."""
    if predicate is None or predicate == {}:
        return ()
    if isinstance(predicate, dict):
        if len(predicate) == 1 and next(iter(predicate)) in ('all', 'and'):
            children = next(iter(predicate.values()))
            if isinstance(children, list) and children and all(isinstance(c, dict) and c for c in children):
                return tuple(sorted({term for c in children for term in _predicate_terms(c, nonnull)}))
        if (set(predicate) == {'column', 'op'} and predicate['op'] in ('is_not_null', 'not_null')
                and predicate['column'] in nonnull):
            return ()
    return (canonical(predicate),)


def observation_boundary(support, key_columns, positions):
    """Compare access, applicability, filters and the non-hierarchy part of row context.

    Positions are physical columns verified on the same source hierarchy. Values
    from adjacent levels still live on their original supports and are checked
    separately when comparing the actual directed edges.
    """
    prov = support['provenance']
    context = support.get('business_context') or {}
    if (not support.get('scope_ref') or support.get('inherited_from') or support.get('lookup_matches')
            or context.get('missing_dimensions') or context.get('conflicts')):
        return None
    row = support.get('row_context') or {}
    mapping = prov.get('business_context_mapping') or {}
    protected = set(prov.get('scope_columns') or []) | set((mapping.get('columns') or {}).values())
    path = positions.keys() - protected
    values = {k: v for k, v in (row.get('values') or {}).items() if k not in path}
    types = {k: v for k, v in (row.get('value_types') or {}).items() if k not in path}
    nonnull = set(key_columns)
    if prov.get('label_column') and (support.get('observed_label') or support.get('label')):
        nonnull.add(prov['label_column'])
    return ObservationBoundary(
        canonical([support['scope_ref'], sorted(support.get('required_scope_refs') or []),
                   sorted(prov.get('scope_columns') or []), business_context_identity(support)]),
        canonical([values, types]), _predicate_terms(prov.get('predicate'), nonnull))


# The mapping language permits arbitrary property names. Restrict positional
# interpretation to explicit structural labels so a project or version named
# "L1" remains a project or version even if it occurs only in the first slot.
_POSITION_NAME = re.compile(r'(?:(?:tree|hierarchy|source|target|parent|child|physical|column|level)_)*'
                            r'(?:level|depth|position|slot|ordinal|locator|location|level_source|source_column)$', re.I)


def _matches_position(name, value, columns, positions, prov, column_ordinals):
    if not _POSITION_NAME.fullmatch(name):
        return False
    if isinstance(value, str) and value in columns:
        return True  # Direct citation of a mapped endpoint, also supported by older outputs.
    if not columns or not all(c in positions for c in columns):
        return False
    depths = {positions[c] for c in columns}
    if len(depths) != 1:
        return False
    depth, = depths
    if type(value) is int:
        return value == depth
    if not isinstance(value, str):
        return False
    table = (prov.get('source') or {}).get('table')
    prefix = rf'(?:{re.escape(table)}[_ .-]+)?' if table else ''
    if re.fullmatch(prefix + rf'(?:L(?:evel)?[_ .-]*)?{depth}', value, re.I):
        return True
    if len(columns) == 1:
        column, = columns
        # The source column prefix is also present on its paired label and is
        # unique to this slot, e.g. <slot>_id and <slot>_name.
        label = prov.get('label_column') or ''
        needle = value + '_'
        if (value and column.startswith(needle) and label.startswith(needle)
                and not any(k != column and k.startswith(needle) for k in positions)):
            return True
        coordinate = re.fullmatch(r'(?:excel_|sheet_)?(?:col|column)[_ .-]*(\d+)', value, re.I)
        if coordinate and isinstance(column_ordinals, dict):
            ordinal = column_ordinals.get(column)
            return type(ordinal) is int and ordinal > 0 and str(ordinal) == coordinate.group(1)
    return False


def observation_properties(support, key_columns, positions, *, column_ordinals=None):
    """Omit a materialized position annotation only when its physical role is verified.

    Optional source ordinals are 1-based physical positions in the full original
    source header, never positions in a projection or an authorization-filtered catalog.
    """
    prov = support['provenance']
    if column_ordinals is None:
        column_ordinals = prov.get('source_column_ordinals')
    constants = prov.get('constant_properties') or {}
    observed = support.get('properties') or {}
    positions_only = {k for k, v in constants.items() if canonical(observed.get(k)) == canonical([v])
                      and _matches_position(k, v, key_columns, positions, prov, column_ordinals)}
    # Preserve physical property observations as pairs: two names can denote
    # the same column without one overwriting the other's observed values.
    properties = {((prov.get('property_columns') or {}).get(k, k), canonical(values))
                  for k, values in observed.items() if k not in positions_only}
    remaining = {k: v for k, v in constants.items() if k not in positions_only}
    return canonical([sorted(properties), remaining])

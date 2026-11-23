"""Compare source observations without changing the evidence stored on a member or edge."""
import re
from collections import defaultdict
from typing import NamedTuple

from lightgraph.contracts import canonical
from lightgraph.source_values import scalar_kind

from .contexts import business_context_identity


class ObservationBoundary(NamedTuple):
    scope: str
    row_context: str | tuple
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


def observation_scope(support):
    return canonical([support['scope_ref'], sorted(support.get('required_scope_refs') or []),
                      sorted(support['provenance'].get('scope_columns') or []), business_context_identity(support)])


def observation_boundary(support, key_columns, positions, *, witnessed_captions=()):
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
    # A mapped physical attribute may be compared through its typed row cell
    # below. Keep that cell in the boundary even if it doubles as a path slot.
    protected.update((prov.get('property_columns') or {}).values())
    path = (positions.keys() | set(witnessed_captions)) - protected
    values = {k: v for k, v in (row.get('values') or {}).items() if k not in path}
    types = {k: v for k, v in (row.get('value_types') or {}).items() if k not in path}
    nonnull = set(key_columns)
    if prov.get('label_column') and (support.get('observed_label') or support.get('label')):
        nonnull.add(prov['label_column'])
    return ObservationBoundary(
        observation_scope(support),
        canonical([values, types]), _predicate_terms(prov.get('predicate'), nonnull))


def _worksheet_projection(support):
    witness = support.get('source_row_witness')
    if witness is None:
        return None
    if (not isinstance(witness, dict) or witness.get('kind') != 'excel_rows_v1'
            or not isinstance(witness.get('snapshot'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', witness['snapshot'])):
        raise ValueError('invalid worksheet witness')
    ranges = witness.get('rows')
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('missing worksheet rows')
    previous = 0
    for span in ranges:
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not previous < span[0] <= span[1] <= 1_048_576):
            raise ValueError('invalid worksheet rows')
        previous = span[1]
    context = support.get('row_context') or {}
    values, types = context.get('values', {}), context.get('value_types', {})
    columns = set(support['provenance'].get('context_columns') or ())
    if (not isinstance(values, dict) or not isinstance(types, dict)
            or set(values) != columns or set(types) != columns
            or not columns.issubset(support['provenance'].get('columns') or ())):
        raise ValueError('incomplete worksheet context')
    # Two imports can select different header rows from the same workbook.
    # Bind every shared column name to its original physical position, including
    # endpoints not requested as row context. A projected column index is insufficient.
    projected = set(support['provenance'].get('columns') or ())
    ordinals = support['provenance'].get('source_column_ordinals') or {}
    if (not isinstance(ordinals, dict)
            or any(type(n) is not int or not 1 <= n <= 16_384 for n in ordinals.values())
            or len(set(ordinals.values())) != len(ordinals)):
        raise ValueError('invalid worksheet column coordinates')
    cells = {('ordinal', c): n for c, n in ordinals.items() if c in projected}
    cells.update({('column', n): c for c, n in ordinals.items() if c in projected})
    complete_header = bool(projected) and projected <= ordinals.keys()
    for col, value in values.items():
        kind = types[col]
        if value is not None and not isinstance(value, (bool, str, float, int)):
            raise ValueError('invalid worksheet cell')
        if not (kind == scalar_kind(value) or
                (isinstance(value, str) and kind in ('date', 'datetime', 'time', 'decimal'))):
            raise ValueError('invalid worksheet cell type')
        cells[col] = canonical([kind, value])
    return witness['snapshot'], tuple(map(tuple, ranges)), cells, complete_header


# A pathological set of staggered projections can otherwise expand quadratically
# even though the input contains only a few thousand intervals for this one edge.
_MAX_EDGE_ROW_COMPARISONS = 65_536


def worksheet_edge_boundaries(entries, diagnostics):
    """Compare independent projections of one physical edge against its complete worksheet row sets.

    entries contains (projection owner, support, original boundary/property pair). Row intervals are
    split only at a recorded endpoint. Each piece keeps its own predicate and scope.
    A legacy support keeps its original row comparison and cannot bridge different
    witnessed rows. None marks contradictory or malformed evidence.
    """
    result = [[boundary] for _, _, (boundary, _) in entries]
    if not any('source_row_witness' in support for _, support, _ in entries):
        return result
    events = defaultdict(lambda: defaultdict(list))
    owner_facts = defaultdict(set)
    cells_by_index = {}
    protected_by_index = {}
    legacy_facts = set()
    located_facts = defaultdict(lambda: defaultdict(set))
    incomplete_headers = set()
    for i, (owner, support, fact) in enumerate(entries):
        boundary, _ = fact
        try:
            witness = _worksheet_projection(support)
        except ValueError:
            result[i] = None
            continue
        if witness is None:
            legacy_facts.add(fact)
            continue
        snapshot, ranges, cells, complete_header = witness
        if not complete_header:
            incomplete_headers.add(i)
            legacy_facts.add(fact)
        cells_by_index[i] = cells
        prov = support['provenance']
        mapping = prov.get('business_context_mapping') or {}
        protected_by_index[i] = (set((prov.get('property_columns') or {}).values())
                                 | set(prov.get('scope_columns') or ())
                                 | set((mapping.get('columns') or {}).values()))
        physical_fact = (snapshot, ranges, frozenset(cells.items()))
        owner_facts[owner].add((fact, physical_fact))
        raw_cells = frozenset((col, cell) for col, cell in cells.items() if isinstance(col, str))
        located_facts[fact][owner].add((snapshot, ranges, raw_cells))
        for first, last in ranges:
            events[snapshot][first].append((i, True))
            events[snapshot][last + 1].append((i, False))
    # Identical projections keep their original boundary. In particular, a
    # disconnected member can still compare its mappings to its role anchor.
    changed = len({frozenset(facts) for facts in owner_facts.values()}) > 1
    keep_exact = {fact for fact in legacy_facts
                  if len({frozenset(rows) for rows in located_facts[fact].values()}) == 1}
    normalize = {i for i in cells_by_index if changed and entries[i][2] not in keep_exact}
    for i in normalize:
        result[i] = None if i in incomplete_headers else []
    # Charge a support once per active interval piece, without expanding a
    # single range to individual worksheet rows. Most merges have one active
    # support per projection. On exhaustion no located support from this edge
    # may supply identity evidence, including those already compared.
    range_count = sum(sum(map(len, steps.values())) for steps in events.values()) // 2
    remaining = min(_MAX_EDGE_ROW_COMPARISONS, 16 * range_count)
    for snapshot, steps in events.items():
        positions = sorted(steps)
        active = set()
        for first, following in zip(positions, positions[1:]):
            for i, start in steps[first]:
                if start:
                    active.add(i)
                else:
                    active.remove(i)
            if not active:
                continue
            remaining -= len(active)
            if remaining < 0:
                field = 'worksheet_edges_exceeding_row_comparison_budget'
                diagnostics[field] = diagnostics.get(field, 0) + 1
                return [None if i in cells_by_index else boundaries for i, boundaries in enumerate(result)]
            common = {}
            protected = set()
            conflict = False
            for i in active:
                protected.update(protected_by_index[i])
                for col, cell in cells_by_index[i].items():
                    if common.setdefault(col, cell) != cell:
                        conflict = True
            for i in active:
                if conflict:
                    result[i] = None
                elif i in normalize and result[i] is not None:
                    boundary, _ = entries[i][2]
                    binding = tuple(sorted((c, cells_by_index[i][c]) for c in protected & cells_by_index[i].keys()))
                    section = ('excel_rows_v1', snapshot, first, following - 1, binding)
                    result[i].append(boundary._replace(row_context=section))
    return result


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


def edge_observation_properties(support, columns, positions, orientation):
    """Bind structural annotations to their specific endpoint before comparing edges."""
    src, dst = columns
    roles = None
    if support['provenance'].get('constant_properties'):
        roles = {'source': src, 'target': dst, 'parent': (), 'child': ()}
        if orientation == 'parent_to_child':
            roles['parent'], roles['child'] = src, dst
        elif orientation == 'child_to_parent':
            roles['parent'], roles['child'] = dst, src
        elif len(src) == len(dst) == 1 and src[0] in positions and dst[0] in positions:
            # Layout can decode an annotation. A separate observation still
            # has to establish a direction for this exact edge and scope.
            roles['parent'], roles['child'] = sorted(columns, key=lambda c: positions[c[0]])
    return observation_properties(support, src + dst, positions, endpoint_roles=roles)


def observation_properties(support, key_columns, positions, *, column_ordinals=None, endpoint_roles=None):
    """Omit a materialized position annotation only when its physical role is verified.

    Optional source ordinals are 1-based physical positions in the full original
    source header, never positions in a projection or an authorization-filtered catalog.
    """
    prov = support['provenance']
    if column_ordinals is None:
        column_ordinals = prov.get('source_column_ordinals')
    constants = prov.get('constant_properties') or {}
    observed = support.get('properties') or {}
    positions_only = set()
    for name, value in constants.items():
        columns = key_columns
        if endpoint_roles is not None:
            # On a relationship a position annotation must name its endpoint.
            # A parent annotation citing the child column is not redundant.
            roles = set(name.lower().split('_')).intersection(endpoint_roles)
            resolved = {endpoint_roles[role] for role in roles}
            columns = next(iter(resolved)) if len(resolved) == 1 else ()
            if len(columns) != 1:
                continue
        if (canonical(observed.get(name)) == canonical([value]) and
                _matches_position(name, value, columns, positions, prov, column_ordinals)):
            positions_only.add(name)
    # One directly projected source cell can also have been recorded as a
    # typed value on this very row. Compare it once through the row boundary.
    # Properties alone carry no driver type in historical checkpoints.
    bindings = prov.get('property_columns') or {}
    row = support.get('row_context') or {}
    row_values, row_types = row.get('values') or {}, row.get('value_types') or {}
    projected = set(prov.get('context_columns') or ()) & set(prov.get('columns') or ())
    properties = set()
    for name, values in observed.items():
        if name in positions_only:
            continue
        column = bindings.get(name)
        if name not in constants and column in projected and column in row_values and column in row_types:
            value, kind = row_values[column], row_types[column]
            if isinstance(value, (str, bool, int, float)) and canonical(values) == canonical([value]):
                # These driver types are serialized as strings on disk. The
                # type comes from the row witness, not from the string's spelling.
                if scalar_kind(value) == kind or (isinstance(value, str) and kind in {'date', 'time', 'datetime', 'decimal'}):
                    continue
        properties.add((column or name, canonical(values)))
    remaining = {k: v for k, v in constants.items() if k not in positions_only}
    return canonical([sorted(properties), remaining])

"""Decide whether independently observed relations support a common source record."""
from lightgraph.contracts import canonical


def _intersection(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        if max(a, c) <= min(b, d):
            result.append((max(a, c), min(b, d)))
        if b <= d:
            i += 1
        if d <= b:
            j += 1
    return result


def _columns(predicate):
    if 'column' in predicate:
        return {predicate['column']}
    if 'not' in predicate:
        return _columns(predicate['not'])
    for op in ('all', 'and', 'any'):
        if op in predicate:
            return set().union(*(_columns(child) for child in predicate[op]))
    return set(predicate)  # Legacy shorthand mapping from a physical column to its condition.


def _clauses(predicate):
    if predicate:
        if len(predicate) == 1 and ('all' in predicate or 'and' in predicate):
            for child in next(iter(predicate.values())):
                yield from _clauses(child)
        else:
            yield predicate


def worksheet_row_witness(value):
    """Read a complete physical worksheet locator without copying unrelated support fields."""
    if not isinstance(value, dict) or value.get('kind') != 'excel_rows_v1':
        return None
    snapshot, rows = value.get('snapshot'), value.get('rows')
    if (not isinstance(snapshot, str) or len(snapshot) != 64 or any(c not in '0123456789abcdef' for c in snapshot)
            or not isinstance(rows, list) or not rows):
        return None
    last = 0
    for span in rows:
        if (not isinstance(span, list) or len(span) != 2 or any(type(v) is not int for v in span)
                or not last < span[0] <= span[1] <= 1_048_576):
            return None
        last = span[1]
    return {'kind': 'excel_rows_v1', 'snapshot': snapshot, 'rows': rows}


def common_record(observations):
    """Edges already share a source, typed projected grain and compatible applicability.

    A source-content hash is a candidate. When physical rows are unavailable, all
    endpoint and scope bindings must be fixed by the common tuple. Each predicate
    wholly determined by those values holds for every row in that slice; remaining
    conditions must have been observed together on at least one relation.
    """
    snapshot = spans = None
    physical = 0
    filters = []
    complete_bindings = True
    for obs in observations:
        if obs.get('source_row_witness') is not None:
            witness = worksheet_row_witness(obs['source_row_witness'])
            if witness is None:
                return None
            revision, rows = witness['snapshot'], witness['rows']
            if snapshot is not None and revision != snapshot:
                return None
            snapshot = revision
            spans = rows if spans is None else _intersection(spans, rows)
            if not spans:
                return None
            physical += 1
        prov = obs.get('provenance') or {}
        grain = set(prov.get('context_columns') or ())
        binding = set(prov.get('source_key_columns') or ()) | set(prov.get('target_key_columns') or ())
        binding.update(prov.get('scope_columns') or ())
        binding.update(((prov.get('business_context_mapping') or {}).get('columns') or {}).values())
        complete_bindings &= bool(binding) and binding <= grain
        filters.append({canonical(clause) for clause in _clauses(prov.get('predicate'))
                        if not _columns(clause) <= grain})
    if physical == len(observations) and physical:
        return {'basis': 'physical_source_row', 'snapshot': snapshot, 'worksheet_row': spans[0][0]}
    required = set().union(*filters)
    if complete_bindings and any(required <= f for f in filters):
        return {'basis': 'common_typed_projection'}
    return None

"""Explain the first observable difference between two directed neighborhood projections."""
from collections import Counter, defaultdict


def _cross_unit_count(counts):
    total = sum(counts.values())
    return (total * total - sum(n * n for n in counts.values())) // 2


def _matching_pairs(records, unit_counts, length):
    groups = defaultdict(Counter)
    for rows, counts in zip(records, unit_counts, strict=True):
        groups[frozenset(row[:length] for row in rows)].update(counts)
    return sum(_cross_unit_count(counts) for counts in groups.values())


def _row_context_differences(records, unit_counts, previous, current, counts):
    """Refine just the pairs lost at row context, retaining the preceding edge fields."""
    if all(isinstance(row[-2], str) for rows in records for row in rows):
        counts['original_row_context'] += previous - current
        return
    expanded = []
    for rows in records:
        projected = []
        for *prefix, context, _properties in rows:
            if isinstance(context, tuple) and len(context) == 5 and context[0] == 'excel_rows_v1':
                _, snapshot, first, last, bindings = context
                parts = ('worksheet', snapshot, (first, last), bindings)
            else:
                # An original boundary can also be retained for witnessed supports.
                parts = ('original', None, None, None)
            projected.append((*prefix, *parts))
        expanded.append(projected)
    details = ('boundary_representation', 'worksheet_snapshot', 'worksheet_row_intervals',
               'worksheet_protected_bindings')
    for length, detail in enumerate(details, 7):
        matching = _matching_pairs(expanded, unit_counts, length)
        if previous != matching:
            counts[detail] += previous - matching
        previous = matching
        if previous == current:
            return
    # Normalized worksheet contexts are fully represented above. Any remaining
    # pairs differ on the original context associated with an incident edge.
    counts['original_row_context'] += previous - current


def neighborhood_difference_counts(matches, members, *, row_context_differences=None):
    """Partition unequal-signature, cross-unit pairs without enumerating individual pairs.

    Successive projections retain everything checked at the previous stage,
    including correlations between the neighbor, role and edge observations.
    The first differing field is a diagnostic, not a verdict on either source.
    """
    levels = ('neighbor_values', 'direction', 'endpoint_columns', 'neighbor_observations',
              'edge_applicability', 'edge_predicate', 'edge_row_context', 'edge_properties')
    unit_counts, records = [], []
    for signature, ids in matches.items():
        unit_counts.append(Counter(members[nid].unit for nid in ids))
        rows = []
        for is_parent, own_columns, other_columns, value, neighbor_facts, (boundary, properties) in signature:
            rows.append((value, is_parent, (own_columns, other_columns), neighbor_facts,
                         boundary.scope, boundary.predicate, boundary.row_context, properties))
        records.append(rows)

    total_by_unit = Counter()
    for counts in unit_counts:
        total_by_unit.update(counts)
    previous = _cross_unit_count(total_by_unit)
    identical = sum(_cross_unit_count(counts) for counts in unit_counts)
    differences = Counter()
    for length, level in enumerate(levels, 1):
        if previous == identical:
            break
        current = _matching_pairs(records, unit_counts, length)
        if previous != current:
            differences[level] = previous - current
            if level == 'edge_row_context' and row_context_differences is not None:
                _row_context_differences(records, unit_counts, previous, current, row_context_differences)
        previous = current
    return differences

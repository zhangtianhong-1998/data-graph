"""Explain the first observable difference between two directed neighborhood projections."""
from collections import Counter, defaultdict


def _cross_unit_count(counts):
    total = sum(counts.values())
    return (total * total - sum(n * n for n in counts.values())) // 2


def neighborhood_difference_counts(matches, members):
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
        groups = defaultdict(Counter)
        for rows, counts in zip(records, unit_counts, strict=True):
            groups[frozenset(row[:length] for row in rows)].update(counts)
        current = sum(_cross_unit_count(counts) for counts in groups.values())
        if previous != current:
            differences[level] = previous - current
        previous = current
    return differences

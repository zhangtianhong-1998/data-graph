"""Counts of overlaps at the exact table and column bindings available to the reviewer."""
from collections import Counter
from heapq import nsmallest
from itertools import combinations, product


def review_priority(pattern):
    """Under a finite call budget, inspect confirmed overlap before using an opaque ID to break ties."""
    rows = pattern.get('binding_comparisons', {}).get('rows', ())
    matched = max((r['matching_observed_names'] for r in rows), default=0)
    return ('apply' not in pattern['eligible_actions'], -matched, -pattern['overlapping_keys'], pattern['pattern_id'])


class BindingComparisons:
    """Read name pages only for routed families with a positive typed-key intersection."""

    def __init__(self, family_rows, counts, comparisons_budget):
        self.family_rows = family_rows
        self.counts = counts
        self.remaining = comparisons_budget
        self.names = {}

    def _qualified_names(self, family):
        slot = (family.owner, family.name)
        if slot not in self.names:
            names = {}
            if not family.problems.intersection({'multiple_members_in_one_projection', 'incomplete_source_binding',
                                                 'source_cell_conflict'}):
                # Format 1 stores observed names per type and key. Attribute a name
                # to a physical binding only when this key occurs at exactly one.
                occurrences = Counter(key for keys in family.by_source.values() for key in keys)
                self.counts['alias_sections'] += 1
                names = {key: label for key, label, _ in self.family_rows[slot].get('aliases', ())
                         if occurrences[key] == 1}
            self.names[slot] = names
        return self.names[slot]

    def compare(self, families, options, bindings, max_rows=24):
        pairs = [(i, j) for i, j in combinations(range(len(options)), 2) if options[i] and options[j]]
        total = sum(len(options[i]) * len(options[j]) for i, j in pairs)
        checked = 0

        def rows():
            nonlocal checked
            for i, j in pairs:
                left, right = families[i], families[j]
                for s, t in product(options[i], options[j]):
                    if self.remaining == 0:
                        return
                    a, b = left.by_source[bindings[s]], right.by_source[bindings[t]]
                    # Bound both the number of probes and the cost of each intersection.
                    self.remaining -= 1
                    cost = min(len(a), len(b))
                    if cost > self.remaining:
                        continue
                    self.remaining -= cost
                    shared = a & b
                    matching = conflicting = 0
                    if shared:
                        ln, rn = self._qualified_names(left), self._qualified_names(right)
                        for key in shared:
                            if key in ln and key in rn:
                                if ln[key] == rn[key]:
                                    matching += 1
                                else:
                                    conflicting += 1
                    checked += 1
                    lreg = left.source_registries.get(bindings[s])
                    rreg = right.source_registries.get(bindings[t])
                    yield {'member_indexes': [i, j], 'source_indexes': [s, t], 'overlapping_keys': len(shared),
                           **({'versioned_directory': 'matches' if lreg & rreg else 'different'} if lreg and rreg else {}),
                           'matching_observed_names': matching, 'conflicting_observed_names': conflicting,
                           'unverified_names': len(shared) - matching - conflicting}

        best = nsmallest(max_rows, rows(), key=lambda r: (-r['matching_observed_names'], -r['overlapping_keys'],
                                                          r['member_indexes'], r['source_indexes']))
        return {'candidate_pairs': total, 'compared_pairs': checked, 'rows': best}

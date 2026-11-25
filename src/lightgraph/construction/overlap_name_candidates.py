"""Route cross-table name comparisons by directed role before reading any alias pages."""
from collections import defaultdict
from itertools import combinations


def different_key_name_candidates(families, rows, covered, *, max_rows, max_comparisons, fanout,
                                  max_patterns, group_pairs, inputs_by_owner):
    """Only complete, unambiguous postings count. No identity or relation rule is produced."""
    stats = {'status': 'complete', 'alias_sections': 0, 'observations_read': 0, 'comparisons': 0,
             'ambiguous_names_suppressed': 0, 'high_frequency_names_suppressed': 0,
             'incomplete_families': 0, 'covered_pairs': 0, 'omitted_patterns': 0}
    sources, roles = [], defaultdict(set)
    for slot, family in sorted(families.items()):
        # Unknown applicability can be reviewed without granting authority.
        # Incomplete or contradictory key/name/contributor evidence cannot
        # establish the one-to-one observations needed even for this cohort.
        if (len(family.by_source) != 1 or family.problems - {'unverified_source_or_scope'}
                or family.definition.get('identity_origin', 'row') != 'row' or rows[slot]['key_count'] < 2):
            continue
        binding, = family.by_source
        # The named column can differ between tables. Its directed role must share
        # the opposite endpoint columns; direction on its own compares unrelated axes.
        signatures = {(end, peer, len(columns)) for _, end, columns, peer in family.role_descriptors
                      if columns == binding[3] and peer}
        pos = len(sources)
        sources.append((family, binding, signatures))
        for role in signatures:
            roles[role].add(pos)
    selected = set()
    for group in roles.values():
        if len({sources[i][1][:3] for i in group}) >= 2 and len({sources[i][1][3] for i in group}) >= 2:
            selected.update(group)
    count = sum(rows[sources[i][0].owner, sources[i][0].name]['key_count'] for i in selected)
    if count > max_rows:
        stats['status'] = 'observation_budget_exceeded'
        return [], stats

    postings = defaultdict(list)
    keys = {}
    for i in sorted(selected):
        f, _, _ = sources[i]
        row = rows[f.owner, f.name]
        aliases = row['aliases']
        stats['alias_sections'] += 1
        stats['observations_read'] += len(aliases)
        if len(aliases) != row['key_count']:
            stats['incomplete_families'] += 1
            continue
        keys[i] = {cell for cell, _, _ in aliases}
        for cell, name, scope in aliases:
            occurrences = postings[name]
            if len(occurrences) <= fanout:
                occurrences.append((i, cell, scope))

    pairs = defaultdict(dict)
    for name, occurrences in postings.items():
        if len(occurrences) > fanout:
            stats['high_frequency_names_suppressed'] += 1
            continue
        if len({i for i, _, _ in occurrences}) != len(occurrences):
            # Multiple codes for the same exact observed name within a projection
            # give no one-to-one crosswalk. Omit that name for every comparison.
            stats['ambiguous_names_suppressed'] += 1
            continue
        for (i, a, sa), (j, b, sb) in combinations(occurrences, 2):
            if stats['comparisons'] >= max_comparisons:
                stats['status'] = 'comparison_budget_exceeded'
                return [], stats
            stats['comparisons'] += 1
            _, left_binding, left_roles = sources[i]
            _, right_binding, right_roles = sources[j]
            if (left_binding[:3] == right_binding[:3] or left_binding[3] == right_binding[3]
                    or not left_roles & right_roles or a == b):
                continue
            pairs[i, j][name] = (name, sa if sa == sb else None)
    qualified = {}
    for (i, j), names in pairs.items():
        if len(names) < 2:
            continue
        left, right = sources[i][0], sources[j][0]
        if frozenset(((left.owner, left.name), (right.owner, right.name))) in covered:
            stats['covered_pairs'] += 1
            continue
        qualified[i, j] = names

    choices = group_pairs(sources, qualified, inputs_by_owner)
    stats['omitted_patterns'] = max(0, len(choices) - max_patterns)
    result = []
    for members, names_count in choices[:max_patterns]:
        ordered = sorted(members, key=lambda m: len(keys[m]))
        # Disclose the exact full typed-key intersection, even when an equal key
        # names a different object. This is independent of matching names.
        cost = (len(ordered) - 1) * len(keys[ordered[0]])
        if stats['comparisons'] + cost > max_comparisons:
            stats['status'] = 'comparison_budget_exceeded'
            return [], stats
        stats['comparisons'] += cost
        shared = sum(all(cell in keys[m] for m in ordered[1:]) for cell in keys[ordered[0]])
        result.append(([sources[i][:2] for i in members], names_count, shared))
    return result, stats

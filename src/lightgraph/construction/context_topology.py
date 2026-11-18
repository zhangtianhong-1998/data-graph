"""Bounded cycle checks over compatible business observations, not their union.

Global observations may accompany any known context. Scoped observations may
coexist only when their common dimensions agree. Unknown observations keep the
legacy structural check in a separate lane; they never establish applicability
along a known-context path. No Cartesian product of context values is built.
"""
from collections import defaultdict, deque

from lightgraph.contracts import canonical, digest
from .contexts import support_business_context
from .statistics import directed_topology


def detect_context_cycles(edges, *, max_states=50000, max_steps=500000, max_dimensions=128,
                          max_state_bytes=8 * 1024 * 1024, check=None):
    """Return proven cyclic edge IDs and whether bounded analysis completed.

First discard structurally acyclic edges. For each remaining edge, search for
a compatible return path while carrying only the dimensions observed on that
path. Exceeding the work bound is inconclusive, never a claim of acyclicity.
    """
    lanes = defaultdict(list)
    unknown_ids = set()
    for edge in edges:
        alternatives = {}
        for support in edge.get('supports') or [edge]:
            context = support_business_context(support)
            lane = 'unknown' if context['status'] == 'unknown' else 'known'
            constraints = {name: canonical([context['value_types'][name], value])
                           for name, value in context['dimensions'].items()}
            alternatives[(lane, canonical(constraints))] = constraints
            if lane == 'unknown':
                unknown_ids.add(edge['id'])
        for (lane, _), constraints in alternatives.items():
            lanes[lane].append({'id': edge['id'], 'source': edge['source'], 'target': edge['target'],
                                'constraints': constraints})
    cycles = set()
    explored = steps = 0
    peak_state_bytes = 0
    complete = True
    reason = None

    def combine(left, right):
        if any(name in left and left[name] != value for name, value in right.items()):
            return None
        return {**left, **right}

    for lane in sorted(lanes):
        observed = lanes[lane]
        pairs = set()
        ids = {edge[end] for edge in observed for end in ('source', 'target')}
        directed_topology(ids, observed, cycle_edge_pairs=pairs)
        candidates = [edge for edge in observed if (edge['source'], edge['target']) in pairs]
        if not candidates:
            continue
        # Common trees and coherent single-context cycles need no path search.
        unified = {}
        for edge in candidates:
            unified = combine(unified, edge['constraints'])
            if unified is None or len(unified) > max_dimensions:
                break
        if unified is not None and len(unified) <= max_dimensions:
            cycles.update(edge['id'] for edge in candidates)
            continue
        outgoing = defaultdict(list)
        for edge in candidates:
            outgoing[edge['source']].append(edge)
        for first in candidates:
            if first['id'] in cycles:
                continue
            # Parent pointers keep path retention linear in the bounded state
            # count rather than copying an entire path into every queue entry.
            queue = deque([0])
            states = [(first['target'], first['constraints'], None, first['id'])]
            visited = {(first['target'], digest(first['constraints']))}
            state_bytes = 512 + 72 * len(first['constraints'])
            peak_state_bytes = max(peak_state_bytes, state_bytes)
            explored += 1
            if explored > max_states:
                complete, reason = False, 'state_limit'
                break
            if state_bytes > max_state_bytes:
                complete, reason = False, 'state_memory_limit'
                break
            while queue:
                state_index = queue.popleft()
                node, dimensions, _, _ = states[state_index]
                if node == first['source']:
                    cursor = state_index
                    while cursor is not None:
                        _, _, parent, edge_id = states[cursor]
                        cycles.add(edge_id)
                        cursor = parent
                    break
                for edge in outgoing[node]:
                    steps += 1
                    if check is not None and steps % 256 == 0:
                        check()
                    if steps > max_steps:
                        complete, reason = False, 'step_limit'
                        break
                    combined = combine(dimensions, edge['constraints'])
                    if combined is None:
                        continue
                    if len(combined) > max_dimensions:
                        complete, reason = False, 'dimension_limit'
                        break
                    token = (edge['target'], digest(combined))
                    if token in visited:
                        continue
                    explored += 1
                    if explored > max_states:
                        complete, reason = False, 'state_limit'
                        break
                    state_bytes += 512 + 72 * len(combined)
                    peak_state_bytes = max(peak_state_bytes, state_bytes)
                    if state_bytes > max_state_bytes:
                        complete, reason = False, 'state_memory_limit'
                        break
                    visited.add(token)
                    states.append((edge['target'], combined, state_index, edge['id']))
                    queue.append(len(states) - 1)
                if not complete:
                    break
            if not complete:
                break
        if not complete:
            break
    return {'cycle_edge_ids': sorted(cycles), 'complete': complete,
            'explored_states': explored, 'examined_steps': steps,
            'estimated_peak_state_bytes': peak_state_bytes,
            'unknown_context_edge_count': len(unknown_ids),
            **({'incomplete_reason': reason} if reason else {})}

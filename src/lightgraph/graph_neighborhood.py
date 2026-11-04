"""Bounded traversal over already-authorized nodes and edges."""
from .errors import LightGraphError


def neighborhood_paths(node_ids, edges, focus, hops, traversal='all'):
    """Return breadth-first nodes and their connecting parents.

    ``chains`` follows incoming and outgoing paths separately. It never turns
    around at a shared endpoint (A -> organization <- B), so membership in the
    same organization does not pull unrelated siblings into A's chain view.
    Authorization must happen before this function; absent endpoints are ignored.
    """
    if traversal not in ('all', 'chains') or type(hops) is not int or hops not in (1, 2):
        raise LightGraphError('graph_page_invalid')
    outgoing = {identifier: set() for identifier in node_ids}
    incoming = {identifier: set() for identifier in outgoing}
    for edge in edges:
        source, target = edge['source'], edge['target']
        if source in outgoing and target in outgoing:
            outgoing[source].add(target)
            incoming[target].add(source)
    if focus not in outgoing:
        return [], {}
    adjacency = ([{key: outgoing[key] | incoming[key] for key in outgoing}]
                 if traversal == 'all' else [outgoing, incoming])
    parents, order = {focus: None}, [focus]
    frontier = [(focus, direction) for direction in range(len(adjacency))]
    visited = set(frontier)
    for _ in range(hops):
        following = []
        for current, direction in frontier:
            for neighbor in sorted(adjacency[direction][current]):
                state = (neighbor, direction)
                if state in visited:
                    continue
                visited.add(state)
                following.append(state)
                if neighbor not in parents:
                    parents[neighbor] = current
                    order.append(neighbor)
        frontier = following
    return order, parents

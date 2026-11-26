"""Append distinct JSON observations without comparing every existing value."""
from copy import deepcopy

from lightgraph.contracts import digest


def merge_list_values(existing, incoming, field, indexes, owner):
    values = existing.setdefault(field, [])
    key = (*owner, field)
    if key not in indexes:
        indexes[key] = {digest(value) for value in values}
    seen = indexes[key]
    for value in incoming[field]:
        token = digest(value)
        if token not in seen:
            seen.add(token)
            values.append(deepcopy(value))

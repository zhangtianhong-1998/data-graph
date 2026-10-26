"""Short-lived handoff of bounded query pages from exploration to review.

The shared task packet carries references and counts only. The workflow owns
this window and closes it in ``finally`` after compilation/review. It must never
be attached to the model, a result, a log record or a persisted work item.
"""
from lightgraph.errors import LightGraphError


def clear_observations(items):
    """Clear nested containers as well as the owner's reference."""
    for item in items:
        for row in item.get('rows',[]):row.clear()
        item.get('rows',[]).clear()
        item.get('columns',[]).clear()
        item.clear()
    items.clear()


class ReviewObservations:
    def __init__(self):
        self._items=[]
        self._closed=False

    def __repr__(self):
        return f'ReviewObservations(closed={self._closed})'

    def __reduce_ex__(self, protocol):
        raise LightGraphError('ephemeral_serialization_forbidden')

    def values(self):
        if self._closed:raise LightGraphError('observation_window_closed')
        return self._items

    def replace(self, items):
        """Take ownership of a validated runtime packet's observation pages."""
        if self._closed:raise LightGraphError('observation_window_closed')
        self.clear()
        self._items=items

    def clear(self):
        clear_observations(self._items)

    def close(self):
        self.clear()
        self._closed=True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

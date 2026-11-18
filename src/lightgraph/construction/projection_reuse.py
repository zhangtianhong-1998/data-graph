"""Small run-local cache for complete projections from versioned immutable inputs."""
from collections import OrderedDict


class ImmutableProjections:
    def __init__(self, source_versions, max_bytes):
        self.versions = dict(source_versions)
        self.limit = max_bytes
        self.bytes = 0
        self.items = OrderedDict()

    def trim(self, room):
        while self.items and self.bytes > max(0, room):
            _, (_, cost, _, _) = self.items.popitem(last=False)
            self.bytes -= cost

    def get(self, source, key):
        token = (source, self.versions.get(source), key)
        item = self.items.get(token)
        if item is not None:
            self.items.move_to_end(token)
        return (item[0], item[3], item[2]) if item is not None else None

    def contains(self, source, key):
        return (source, self.versions.get(source), key) in self.items

    def put(self, source, key, rows, size, window):
        if source not in self.versions:
            return
        # Charge a per-entry overhead as well as serialized rows, including empty sets.
        cost = size + len(key.encode()) + 512
        if cost > self.limit:
            return
        token = (source, self.versions[source], key)
        previous = self.items.pop(token, None)
        if previous:
            self.bytes -= previous[1]
        self.trim(self.limit - cost)
        self.items[token] = (rows, cost, dict(window), size)
        self.bytes += cost

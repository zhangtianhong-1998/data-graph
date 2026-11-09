"""Append-only graph accounting without repeatedly serializing growing facts.

The graph's canonical payload size remains the admission boundary. Membership
indexes additionally have a conservative per-entry allowance; neither estimate
claims to measure total Python RSS or database-side working memory.
"""
from hashlib import sha256

from lightgraph.contracts import canonical


class GraphAccumulator:
    def __init__(self, account):
        self.account = account
        self.seen = {}
        self.labels = {}
        self.serialized_bytes = 0

    def encoded(self, value):
        data = canonical(value).encode()
        self.serialized_bytes += len(data)
        return data

    def register(self, item):
        self.account(len(self.encoded(item)), 0)

    def append(self, item, field, value, *, property_name=None):
        container = item if property_name is None else item['properties']
        name = field if property_name is None else property_name
        key = (item['id'], field, property_name)
        encoded = self.encoded(value)
        token = sha256(encoded).digest()
        members = self.seen.get(key)
        if members is not None and token in members:
            return
        # Empty dict becomes {"key":[]}; subsequent keys need a separator.
        delta = 0
        if name not in container:
            delta = len(self.encoded(name)) + 3 + bool(container)
            container[name] = []
        values = container[name]
        delta += len(encoded) + bool(values)
        if members is None:
            members = self.seen[key] = set()
            index_bytes = 320
        else:
            index_bytes = 0
        members.add(token)
        values.append(value)
        self.account(delta, index_bytes + 128)

    def replace(self, item, name, value):
        delta = len(self.encoded(value)) - len(self.encoded(item[name]))
        item[name] = value
        self.account(delta, 0)

    def label_conflicts(self, node_id, scope, label):
        """Only the first label and an existing conflict are needed per scope."""
        key = (node_id, scope)
        token = sha256(label.encode()).digest()
        previous = self.labels.get(key)
        if previous is None:
            # Keep a fixed-size fingerprint, not a second retained long name.
            # Full names and counterexamples remain in the graph supports.
            self.labels[key] = (token, False)
            self.account(0, 320 + len(scope.encode()))
            return False
        first, conflicted = previous
        conflict = conflicted or first != token
        if conflict and not conflicted:
            self.labels[key] = (first, True)
        return conflict

    def clear(self):
        self.seen.clear()
        self.labels.clear()

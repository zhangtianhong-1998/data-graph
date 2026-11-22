"""Recognize redundant text cells on physically witnessed rows of a hierarchy."""
from collections import defaultdict

from .hierarchy_observations import observation_scope


class HierarchyCaptions:
    """A caption can be omitted from comparison only with its typed key and a matching source observation."""

    def __init__(self):
        self.columns = defaultdict(dict)
        self.values = {}

    def record(self, node, member, layouts):
        for support in node['supports']:
            prov = support['provenance']
            keys = tuple(prov['key_columns'])
            if len(keys) != 1 or (node['type'], member.address, keys) not in layouts:
                continue
            column, = keys
            typed_key, = member.raw_key
            names = [(prov['label_column'], support.get('observed_label') or support.get('label')),
                     (prov.get('english_name_column'), support.get('english_name'))]
            aliases = prov.get('alias_columns') or []
            if len(aliases) == 1 and len(support.get('aliases') or []) == 1:
                names.append((aliases[0], support['aliases'][0]))
            roles = self.columns[member.address, member.unit, node['type']]
            scope = observation_scope(support)
            row = support.get('row_context') or {}
            values, types = row.get('values') or {}, row.get('value_types') or {}
            for name, value in names:
                if not name or name == column:
                    continue
                roles[name] = column if roles.get(name, column) == column else None
                witness = (member.address, column, typed_key, name, scope)
                # Missing, contradictory or non-text captions cannot certify a
                # row context. Ambiguity is shared across contributor copies.
                inconsistent_cell = name in values and (types.get(name) != 'text' or value != values[name])
                if inconsistent_cell or not isinstance(value, str) or not value or self.values.get(witness, value) != value:
                    self.values[witness] = None
                else:
                    self.values[witness] = value

    def witnessed(self, support, kind, address, positions, endpoints):
        row = support.get('row_context') or {}
        values = row.get('values') or {}
        if not positions or not values or not support.get('scope_ref'):
            return ()
        roles = self.columns.get((address, support.get('contributor_id'), kind), {})
        prov = support['provenance']
        recorded = set(prov.get('context_columns') or ()) & set(prov.get('columns') or ())
        columns = roles.keys() & values.keys() & recorded
        if not columns:
            return ()
        types = row.get('value_types') or {}
        scope = observation_scope(support)
        proven = set()
        for name in columns:
            key = roles[name]
            if key not in positions or name in positions or types.get(name) != 'text' or not isinstance(values[name], str):
                continue
            typed_key = endpoints.get(key)
            if key in values and key in recorded:
                row_key = (types.get(key), values[key])
                if row_key[0] is None or (typed_key is not None and typed_key != row_key):
                    continue
                typed_key = row_key
            if typed_key is not None and self.values.get((address, key, typed_key, name, scope)) == values[name]:
                proven.add(name)
        return proven

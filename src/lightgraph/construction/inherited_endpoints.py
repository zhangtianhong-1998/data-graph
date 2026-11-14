"""Resolve inherited relation endpoints against the supports of a frozen graph."""
from collections import defaultdict
from itertools import product

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import identity_cell, scalar_cell, scalar_kind, typed_key


def inherited_keys(graph):
    return {entity['type']: entity['key_names']
            for entity in (graph or {}).get('definitions', {}).get('entities', [])
            if entity.get('key_names')}


def inherited_contract_error(draft, key_names):
    """Check the actual frozen key, independently of the Agent's optional arity hint."""
    if not draft.extends_base_graph:
        return None
    if not key_names:
        return 'inherited_base_graph_missing'
    for kind in draft.inherited_entity_types:
        if kind not in key_names:
            return 'inherited_entity_type_missing'
        if draft.inherited_entity_key_arities.get(kind, len(key_names[kind])) != len(key_names[kind]):
            return 'relation_key_arity_mismatch'
    local_types = {definition.type for definition in draft.entities}
    for relation in draft.relations:
        for mapping in relation.mappings:
            for end in ('source', 'target'):
                kind = getattr(relation, end + '_type')
                lookup = getattr(mapping, end + '_lookup')
                if lookup and lookup.source_id == '__base_graph__':
                    if list(lookup.key_columns) != key_names[kind]:
                        return 'inherited_lookup_key_mismatch'
                elif kind not in local_types:
                    keys = lookup.key_columns if lookup else getattr(mapping, end + '_key_columns')
                    if len(keys) != len(key_names[kind]):
                        return 'relation_key_arity_mismatch'
    return None


def _cells(node, support, column):
    if column in node['identity']:
        return [identity_cell(node, column)]
    if column in ('label', 'english_name'):
        values = [support.get(column)]
    else:
        values = support.get('properties', {}).get(column, ())
    return [scalar_cell(value) for value in values if value is not None and value != '']


class InheritedEndpoints:
    def __init__(self, graph):
        self.keys = inherited_keys(graph)
        self.nodes = defaultdict(list)
        for node in (graph or {}).get('nodes', []):
            self.nodes[node['type']].append(node)
        self.indexes = {}

    def resolve(self, kind, lookup, columns, values, end, *, clock, charge):
        keys = self.keys.get(kind)
        if not keys:
            raise LightGraphError('inherited_entity_type_missing')
        match = tuple(lookup.match_columns if lookup else keys)
        index_key = (kind, match)
        if index_key not in self.indexes:
            index = defaultdict(dict)
            for node in self.nodes[kind]:
                for support in node.get('supports', []):
                    clock()
                    # Names and properties from different supports cannot form a
                    # composite match. Preserve the access conditions of this witness.
                    required = tuple(sorted({support['scope_ref'], *support.get('required_scope_refs', [])}))
                    for cells in product(*(_cells(node, support, name) for name in match)):
                        clock()
                        token = canonical(cells)
                        witness = (node['id'], support['scope_ref'], required)
                        if witness not in index[token]:
                            charge(len(token.encode()) + len(canonical(witness).encode()) + 32)
                            index[token][witness] = node
            self.indexes[index_key] = index
        matched = self.indexes[index_key].get(typed_key(values), {})
        if not matched:
            if lookup and lookup.on_missing == 'omit':
                return []
            raise LightGraphError('lookup_reference_unmatched' if lookup else 'unresolved_relation_endpoint')
        if (lookup is None or lookup.cardinality == 'one') and len({w[0] for w in matched}) > 1:
            raise LightGraphError('lookup_reference_ambiguous')
        results = []
        for (nid, scope, required), node in matched.items():
            key = [node['identity'][name] for name in keys]
            results.append((key, {
                'endpoint': end, 'source': {'source_id': '__base_graph__', 'schema': 'inherited', 'table': kind},
                'match_columns': list(match), 'key_columns': keys, 'input_columns': columns,
                'input_values': values, 'resolved_key': key,
                'input_value_types': [scalar_kind(value) for value in values],
                'resolved_key_types': [identity_cell(node, name)[0] for name in keys],
                'scope_ref': scope, 'required_scope_refs': list(required), 'inherited_node_id': nid,
            }))
        return results

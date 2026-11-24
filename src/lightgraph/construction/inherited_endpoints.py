"""Resolve inherited relation endpoints against the supports of a frozen graph."""
from collections import defaultdict
from copy import deepcopy
from itertools import product

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.source_values import identity_cell, scalar_cell, scalar_kind, typed_key
from .contexts import support_business_context, intersect_business_contexts


def graph_entity_contracts(graph):
    """The frozen identifier contract. Old snapshots can omit default fields."""
    return {entity['type']: {
        'key_names': list(entity['key_names']),
        'identity_origin': entity.get('identity_origin', 'row'),
        'identity_scope': entity.get('identity_scope', 'unknown'),
        'identity_context_keys': list(entity.get('identity_context_keys', [])),
        'identity_domain': entity.get('identity_domain'),
    } for entity in (graph or {}).get('definitions', {}).get('entities', []) if entity.get('key_names')}


def inherited_contract_error(draft, contracts):
    """A declared reuse must keep the frozen identity, including its grain."""
    if not draft.extends_base_graph:
        return None
    if not contracts:
        return 'inherited_base_graph_missing'
    key_names = {kind: item['key_names'] for kind, item in contracts.items()}
    for kind in draft.inherited_entity_types:
        if kind not in key_names:
            return 'inherited_entity_type_missing'
        if draft.inherited_entity_key_arities.get(kind, len(key_names[kind])) != len(key_names[kind]):
            return 'relation_key_arity_mismatch'
    local_types = {definition.type for definition in draft.entities}
    for entity in draft.entities:
        if entity.type in draft.inherited_entity_types:
            proposed = entity.model_dump(include={'key_names', 'identity_scope', 'identity_context_keys', 'identity_domain'})
            proposed['identity_origin'] = 'physical_column' if entity.mappings[0].kind == 'metadata_column' else 'row'
            if proposed != contracts[entity.type]:
                return 'inherited_entity_identity_contract_changed'
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
        self.contracts = graph_entity_contracts(graph)
        self.keys = {kind: item['key_names'] for kind, item in self.contracts.items()}
        self.nodes = defaultdict(list)
        for node in (graph or {}).get('nodes', []):
            self.nodes[node['type']].append(node)
        self.indexes = {}

    def resolve(self, kind, lookup, columns, values, end, *, clock, charge, business_context=None):
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
                    witness = (node['id'], digest(support))
                    for cells in product(*(_cells(node, support, name) for name in match)):
                        clock()
                        token = canonical(cells)
                        # Same node and permission can still have different names,
                        # contexts and source observations. Keep each actual witness.
                        if witness not in index[token]:
                            charge(len(token.encode()) + len(canonical(witness).encode()) + 192)
                            index[token][witness] = (node, support, required)
            self.indexes[index_key] = index
        matched = self.indexes[index_key].get(typed_key(values), {})
        if not matched:
            if lookup and lookup.on_missing == 'omit':
                return []
            raise LightGraphError('lookup_reference_unmatched' if lookup else 'unresolved_relation_endpoint')
        compatible = {key: value for key, value in matched.items()
                      if intersect_business_contexts(business_context or {}, support_business_context(value[1])) is not None}
        if not compatible:
            if lookup and lookup.on_missing == 'omit':
                return []
            raise LightGraphError('inherited_lookup_context_unmatched')
        matched = compatible
        if (lookup is None or lookup.cardinality == 'one') and len({w[0] for w in matched}) > 1:
            raise LightGraphError('lookup_reference_ambiguous')
        results = []
        for (nid, _), (node, support, required) in matched.items():
            key = [node['identity'][name] for name in keys]
            provenance = support.get('provenance', {})
            refs = sorted(set(support.get('evidence_refs', [])) | set(provenance.get('evidence_refs', [])))
            results.append((key, {
                'endpoint': end, 'source': {'source_id': '__base_graph__', 'schema': 'inherited', 'table': kind},
                'match_columns': list(match), 'key_columns': keys, 'input_columns': columns,
                'input_values': values, 'resolved_key': key,
                'input_value_types': [scalar_kind(value) for value in values],
                'resolved_key_types': [identity_cell(node, name)[0] for name in keys],
                'scope_ref': support['scope_ref'], 'required_scope_refs': list(required), 'inherited_node_id': nid,
                'business_context': support_business_context(support), 'evidence_refs': refs,
                **{name: deepcopy(support[name]) for name in ('row_context', 'observation_id') if name in support},
                **({'provenance': deepcopy(provenance)} if provenance else {}),
            }))
        return results

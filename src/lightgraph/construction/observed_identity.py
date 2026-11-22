"""Recognize repeated projections of the same source fact in one workflow.

The source binding is local to a project. A physical key proves a replay only
within the same role and applicability. A matching name or code alone gives no
authority to combine different tables, business contexts or hierarchy levels.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass

from lightgraph.contracts import canonical, digest
from lightgraph.source_values import identity_cell
from .contexts import business_context_identity
from .fusion import definition_contract


@dataclass
class _Projection:
    index: int
    definition: dict
    family: str
    roles: set
    endpoint_roles: set
    entries: list
    bindings: set
    complete: bool


def _physical_source(provenance):
    source = provenance.get('source', {})
    parts = [source.get(name) for name in ('source_id', 'schema', 'table')]
    return parts if all(isinstance(value, str) and value for value in parts) else None


def _relation_roles(graph):
    """A coincident key/name projection can describe separate roles, e.g. buyer and seller."""
    roles, endpoint_roles = defaultdict(set), defaultdict(set)
    definitions = {item['type']: item for item in graph.get('definitions', {}).get('relations', [])}
    for edge in graph.get('edges', []):
        relation = definitions.get(edge['type'], {})
        for support in edge.get('supports', []):
            if support.get('inherited_from'):
                continue
            prov = support.get('provenance', {})
            source = _physical_source(prov)
            if not source or not prov.get('source_key_columns') or not prov.get('target_key_columns'):
                continue
            signature = [source, prov['source_key_columns'], prov['target_key_columns'],
                         prov.get('predicate'), prov.get('constant_properties'),
                         prov.get('source_lookup'), prov.get('target_lookup')]
            for end in ('source', 'target'):
                entity_type = relation.get(end + '_type')
                roles[entity_type].add(digest(end, signature))
                # A draft can attach a new relation to the very same object.
                # Only this end's binding identifies its role in such a relation.
                endpoint_roles[entity_type].add(digest(end, source, prov[end + '_key_columns'],
                                                       prov.get(end + '_lookup')))
    return roles, endpoint_roles


def _node_evidence(node, definition, *, dictionary=None):
    names = definition.get('key_names', [])
    if (not names or set(node.get('identity', {})) != set(names)
            or node.get('identity_domain') != definition.get('identity_domain')
            or set(node.get('identity_types', {})) != set(names)
            or node.get('identity_origin', 'row') != 'row' or not node.get('supports')):
        return None
    values = [identity_cell(node, key) for key in names]
    if any(value is None or value == '' for _, value in values):
        return None
    observations, applicability = set(), set()
    unknown_scope = False
    for support in node['supports']:
        prov = support.get('provenance') or {}
        source = _physical_source(prov)
        columns = prov.get('key_columns') or []
        context = support.get('business_context') or {}
        label_column = prov.get('label_column')
        if (support.get('inherited_from') or not source or (support.get('label') is not None and not label_column)
                or len(columns) != len(names) or len(set(columns)) != len(columns)
                or not support.get('scope_ref') or context.get('conflicts') or context.get('missing_dimensions')):
            return None
        # Dictionary membership is checked separately against an observed
        # unique-key row. Its address can then stand in for the local binding.
        binding_parts = [source, columns, label_column] if dictionary is None else ['catalog_key', dictionary]
        binding = digest(*binding_parts, prov.get('predicate'), prov.get('constant_properties'),
                         prov.get('scope_columns', []))
        facts = business_context_identity(support)
        if definition.get('identity_scope') == 'scoped':
            if facts['status'] != 'scoped' or any(
                key not in facts['dimensions'] or canonical(facts['dimensions'][key]) != canonical(node['identity'][key])
                for key in definition.get('identity_context_keys', [])
            ):
                return None
        # For unknown applicability a differing source-row context may be the
        # only surviving witness of a project, level or business role.
        row = support.get('row_context', {}) if facts['status'] == 'unknown' else {}
        unscoped_properties = {}
        if facts['status'] == 'unknown':
            unknown_scope = True
            # A mapping may project only some columns into row_context.
            # Properties outside that projection can still distinguish two
            # projects or roles, even when the row_context values agree.
            property_columns = prov.get('property_columns', {})
            row_values = row.get('values') or {}
            for name, property_values in support.get('properties', {}).items():
                column = property_columns.get(name, name)
                if (name in property_columns and column in row_values and
                        canonical(property_values) == canonical([row_values[column]])):
                    continue
                unscoped_properties[column] = sorted(property_values, key=canonical)
        boundary = canonical([support['scope_ref'], sorted(support.get('required_scope_refs', [])),
                              facts, row.get('values'), row.get('value_types'), unscoped_properties])
        observations.add((binding, boundary))
        applicability.add(boundary)
    identity_context = business_context_identity({'business_context': node.get('identity_context')})
    applicability.add(canonical({'identity_context': identity_context}))
    if unknown_scope:
        # A shared C observation cannot declare whether unrelated A and B
        # facts belong to M, K, or a globally reusable business entity.
        applicability.add(canonical({'unknown_bindings': sorted({binding for binding, _ in observations})}))
    return tuple(sorted(observations)), canonical(values), tuple(sorted(applicability))


def _dictionary_binding(node, definition):
    """All contributing observations must identify the same complete dictionary key."""
    address = None
    roles = set()
    member = False
    for support in node.get('supports', []):
        prov = support.get('provenance') or {}
        ref = prov.get('catalog_identity_reference')
        if not isinstance(ref, dict) or support.get('inherited_from'):
            return None
        target = ref.get('target')
        source = _physical_source(prov)
        keys = prov.get('key_columns')
        if (not source or not isinstance(target, dict) or not isinstance(keys, list) or not keys
                or ref.get('local_columns') != keys):
            return None
        table = [target.get(name) for name in ('source_id', 'schema', 'table')]
        columns = target.get('columns')
        if (table[0] != source[0] or not all(isinstance(s, str) and s for s in table)
                or not isinstance(columns, list) or len(columns) != len(keys)
                or not all(isinstance(s, str) and s for s in columns + keys)
                or len(set(columns)) != len(columns)):
            return None
        # A previously applied normalization can change which dictionary row
        # a value would address. Only the original typed key witnesses that row.
        if 'original_key_values' in support:
            raw = support['original_key_values']
            names = definition.get('key_names', [])
            if (not isinstance(raw, dict) or len(keys) != len(names)
                    or any(k not in raw or canonical(raw[k]) != canonical(node['identity'].get(n))
                           for k, n in zip(keys, names))):
                return None
        coordinate = (*table, tuple(columns))
        if address is not None and coordinate != address:
            return None
        address = coordinate
        if ref.get('kind') == 'unique_key' and table == source and columns == keys:
            member = True
        elif ref.get('kind') == 'foreign_key':
            roles.add(tuple(keys))
        else:
            return None
    return (address, roles, member) if address is not None else None


def align_dictionary_nodes(prepared, observed_ids, *, local_bases, excluded_types):
    """Join complete FK projections only with an actual row of the referenced unique key.

    Called after definition variants are resolved. Typed values, role, predicate,
    visibility and applicability remain part of the comparison. A match from
    an earlier source-replay pass must move as a whole or stay as it was.
    """
    overrides = [{} for _ in prepared]
    groups = defaultdict(list)
    source_group_sizes = Counter(target for remap in observed_ids for target in remap.values())
    for index, (cid, kind, graph) in enumerate(prepared):
        if kind == 'base' and cid not in local_bases:
            continue
        definitions = {d['type']: d for d in graph.get('definitions', {}).get('entities', [])}
        for node in graph.get('nodes', []):
            definition = definitions.get(node['type'])
            if (not definition or definition.get('identity_scope') == 'global'
                    or definition.get('original_type', node['type']) in excluded_types
                    or node['type'] in excluded_types):
                continue
            binding = _dictionary_binding(node, definition)
            if binding is None:
                continue
            address, roles, member = binding
            evidence = _node_evidence(node, definition, dictionary=address)
            if evidence:
                groups[node['type'], address, evidence].append((index, node, roles, member))

    counts = {'dictionary_eligible_inputs': sum(len(entries) for entries in groups.values()),
              'dictionary_groups_without_observed_member': 0, 'observed_dictionary_collapsed_nodes': 0}
    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        source_groups = Counter(observed_ids[i][n['id']] for i, n, _, _ in entries if n['id'] in observed_ids[i])
        incomplete = {nid for nid, size in source_groups.items() if size != source_group_sizes[nid]}
        if incomplete:
            entries = [e for e in entries if observed_ids[e[0]].get(e[1]['id']) not in incomplete]
        if len(entries) < 2 or len({i for i, *_ in entries}) < 2:
            continue
        roles = {role for _, _, node_roles, _ in entries for role in node_roles}
        # A prior merge can contain two unaligned occurrences from different
        # units. Keep that distinction when a later stage adds the dictionary.
        owners = {(i, n.get('unit_id') if prepared[i][1] in {'merged', 'base'} else None)
                  for i, n, _, _ in entries}
        if len(roles) != 1 or len(owners) != len(entries):
            continue
        # Shared frozen inputs may already contain the same endpoint in a diamond DAG.
        before = set()
        for index, node, _, _ in entries:
            shared_id = observed_ids[index].get(node['id'])
            if shared_id is None and prepared[index][1] in {'merged', 'base'}:
                shared_id = node['id']
            before.add((None, shared_id) if shared_id is not None else (index, node['id']))
        if len(before) == 1:
            continue
        if not any(member for _, _, _, member in entries):
            counts['dictionary_groups_without_observed_member'] += 1
            continue
        frozen = {n['id'] for i, n, _, _ in entries if prepared[i][1] in {'merged', 'base'}}
        target = min(frozen) if frozen else 'n_' + digest('observed_dictionary_key_v1', key, sorted(roles))
        counts['observed_dictionary_collapsed_nodes'] += len(before) - 1
        for index, node, _, _ in entries:
            overrides[index][node['id']] = target
            for support in node['supports']:
                support.setdefault('identity_basis', 'observed_dictionary_key_v1')
                if 'identity_origins' not in support:
                    support['identity_origins'] = [{k: deepcopy(node[k]) for k in ('id', 'type', 'identity', 'identity_types')}]
    return overrides, counts


def align_draft_nodes(graphs, excluded_types):
    """Recognize additional source-backed aliases in an existing local namespace.

    Keep established IDs when several drafts already use the same local
    identity contract. Each support still carries its independent ACL and
    applicability; cross-draft topology checks must see the shared endpoint.
    """
    inputs = [(graph.get('draft_id', ''), 'draft', graph) for graph in graphs]
    overrides, _ = align_observed_nodes(inputs, local_bases=set(), excluded_types=excluded_types, emit_candidates=False)
    origins_by_target = defaultdict(set)
    for remap in overrides:
        for original, target in remap.items():
            origins_by_target[target].add(original)
    for remap in overrides:
        for original, target in remap.items():
            if origins_by_target[target] == {original}:
                remap[original] = original
    return overrides


def _overlapping_types(by_contract):
    # Components bound the search; each actual merge also needs a common
    # physical witness across all members. A-B and B-C cannot prove A-C.
    for contract, entries in by_contract.items():
        by_binding = defaultdict(list)
        for index, entry in enumerate(entries):
            for binding in entry.bindings:
                by_binding[binding].append(index)
        seen = set()
        for index in range(len(entries)):
            if index in seen:
                continue
            pending = [index]
            component = []
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                entry = entries[current]
                component.append(entry)
                for binding in entry.bindings:
                    pending.extend(by_binding.pop(binding, []))
            if len(component) > 1:
                yield contract, component


def align_observed_nodes(prepared, *, local_bases, excluded_types, emit_candidates, diagnostics=None):
    """Reconcile typed observations on private input copies; return per-input ID overrides.

    Logical key names may differ positionally. A type alias additionally needs
    a corroborating directional relation binding. Every source binding for a
    type is checked, and competing roles in one input block the entire group.
    """
    overrides = [{} for _ in prepared]
    groups = defaultdict(list)
    type_owners = defaultdict(set)
    witnesses = defaultdict(set)
    considered_nodes = eligible_nodes = 0
    for index, (cid, kind, graph) in enumerate(prepared):
        nodes_by_type = defaultdict(list)
        for node in graph.get('nodes', []):
            nodes_by_type[node['type']].append(node)
        roles, endpoint_roles = _relation_roles(graph)
        for definition in graph.get('definitions', {}).get('entities', []):
            name = definition['type']
            type_owners[name].add(index)
            if (kind == 'base' and cid not in local_bases or name in excluded_types
                    or definition.get('identity_scope') == 'global'
                    or definition.get('identity_origin', 'row') != 'row'):
                continue
            nodes = nodes_by_type.get(name, [])
            entries = [(node, evidence) for node in nodes
                       if (evidence := _node_evidence(node, definition)) is not None]
            considered_nodes += len(nodes)
            eligible_nodes += len(entries)
            if not entries:
                continue
            if emit_candidates:
                for node, (observations, values, contexts) in entries:
                    for observation in observations:
                        witnesses[observation, values, contexts].add((index, node['id']))
            shape = definition_contract(definition)
            family = shape.pop('type')
            key_names = shape.pop('key_names')
            if not set(shape['identity_context_keys']) <= set(key_names):
                continue
            shape['context_positions'] = [key_names.index(key) for key in shape['identity_context_keys']]
            bindings = {binding for _, (facts, _, _) in entries for binding, _ in facts}
            groups[canonical(shape)].append(
                _Projection(index, definition, family, roles[name], endpoint_roles[name], entries, bindings,
                            len(entries) == len(nodes)))

    for contract, members in _overlapping_types(groups):
        component_inputs = {entry.index for entry in members}
        for peers in _compatible_roles(members):
            _align_roles(peers, contract, prepared, type_owners, overrides, component_inputs)
    review = [group for group in witnesses.values() if len({index for index, _ in group}) > 1]
    if diagnostics is not None:
        diagnostics.update(considered_row_nodes=considered_nodes, eligible_row_nodes=eligible_nodes)
        if emit_candidates:
            # Count complete source/typed-value/context witnesses. A node may
            # have more than one, so this is not a count of redundant nodes.
            diagnostics['cross_contributor_source_witness_groups'] = len(review)
    return overrides, review


def _compatible_roles(members):
    """Different logical names need corroboration from the same directed relationship role."""
    known = [entry.roles for entry in members if entry.roles]
    if len({entry.family for entry in members}) == 1:
        endpoints = [entry.endpoint_roles for entry in members if entry.endpoint_roles]
        if not endpoints or set.intersection(*endpoints):
            return [members]
    elif len(known) == len(members) and set.intersection(*known):
        # A shared directed role is positive alias evidence even if a branch
        # contributes additional relationships that were absent in its peers.
        return [members]
    partitions = defaultdict(list)
    for entry in members:
        key = ('roles', *sorted(entry.roles)) if entry.roles else ('family', entry.family)
        partitions[key].append(entry)
    return partitions.values()


def _align_roles(peers, contract, prepared, type_owners, overrides, component_inputs):
    if len(peers) < 2 or len({entry.index for entry in peers}) != len(peers):
        return
    winner = min(peers, key=lambda entry: (prepared[entry.index][1] not in {'base', 'merged'}, entry.complete,
                                          entry.definition['type'], entry.definition['key_names'], entry.index))
    target = winner.definition
    # A disjoint contributor may use this type name for its own source. It
    # constrains an alias that would rewrite definitions, not a replay between
    # two peers that already have the same local type and key names.
    adopting = any(entry.definition['type'] != target['type'] or entry.definition['key_names'] != target['key_names']
                   for entry in peers)
    other_owners = type_owners[target['type']] - {entry.index for entry in peers if entry.definition['type'] == target['type']}
    # Competing roles with the same physical evidence must not independently
    # mint the same source-key ID. Disjoint physical components cannot collide.
    if other_owners and (adopting or other_owners & component_inputs):
        return
    peers = [entry for entry in peers if entry.complete or
             (entry.definition['type'], entry.definition['key_names']) == (target['type'], target['key_names'])]
    if len(peers) < 2:
        return
    shared_bindings = set.intersection(*(entry.bindings for entry in peers))
    if not shared_bindings and adopting:
        return
    # Renaming a definition also changes the type of its unmatched
    # instances. Do this only if every one is grounded in that same role.
    if any(not any(binding in shared_bindings for binding, _ in facts)
           for entry in peers if (entry.definition['type'], entry.definition['key_names'])
           != (target['type'], target['key_names'])
           for _, (facts, _, _) in entry.entries):
        return
    observations = defaultdict(list)
    for entry in peers:
        for node, (facts, values, contexts) in entry.entries:
            witnessed = tuple(fact for fact in facts if fact[0] in shared_bindings)
            # This node may have no physical source in common with all
            # branches. Equal complete observations still prove the two A
            # replays in A, A, A+B, B without identifying A with B.
            if not witnessed and not adopting:
                witnessed = facts
            if witnessed:
                observations[witnessed, values, contexts].append((entry.index, node))
    matches = [(key, batch) for key, batch in observations.items()
               if len(batch) > 1 and len({index for index, _ in batch}) == len(batch)]
    if not matches:
        return
    for key, batch in matches:
        frozen = sorted(node['id'] for index, node in batch if prepared[index][1] in {'base', 'merged'})
        node_id = frozen[0] if frozen else 'n_' + digest('observed_source_key_v1', target['type'], contract, key)
        for index, node in batch:
            overrides[index][node['id']] = node_id
    for entry in peers:
        _adopt_definition(entry, target, prepared[entry.index][2], overrides[entry.index])


def _adopt_definition(entry, target, graph, matched_ids):
    """Preserve the original identity on its support when applying a verified logical alias."""
    definition = entry.definition
    old_type, new_type = definition['type'], target['type']
    old_keys, new_keys = definition['key_names'], target['key_names']
    for node, _ in entry.entries:
        if node['id'] not in matched_ids and old_type == new_type and old_keys == new_keys:
            continue
        origin = {'id': node['id'], 'type': old_type,
                  'identity': deepcopy(node['identity']), 'identity_types': deepcopy(node.get('identity_types', {}))}
        for support in node['supports']:
            if node['id'] in matched_ids:
                support['identity_basis'] = 'observed_source_key_v1'
            if 'identity_origins' not in support:
                support['identity_origins'] = [deepcopy(origin)]
            columns = support['provenance']['key_columns']
            support.setdefault('original_key_values', dict(zip(columns, (node['identity'][n] for n in old_keys))))
        node.update(type=new_type, identity=dict(zip(new_keys, (node['identity'][n] for n in old_keys))))
        if 'identity_types' in node:
            node['identity_types'] = {new: node['identity_types'][old] for new, old in
                                     zip(new_keys, old_keys) if old in node['identity_types']}
        for marker in ('original_type', 'definition_variant'):
            if marker in target:
                node[marker] = deepcopy(target[marker])
            else:
                node.pop(marker, None)
    if definition is not target:
        definition.update(type=new_type, key_names=list(new_keys))
        for marker in ('original_type', 'definition_variant'):
            if marker in target:
                definition[marker] = deepcopy(target[marker])
            else:
                definition.pop(marker, None)
    if old_type != new_type:
        for relation in graph.get('definitions', {}).get('relations', []):
            for end in ('source_type', 'target_type'):
                if relation.get(end) == old_type:
                    relation[end] = new_type

"""Inventory candidate blocks on one checkpoint or merged graph, without applying identity rules."""
from collections import Counter, defaultdict
from decimal import InvalidOperation
import json

from lightgraph.construction.contexts import business_context_identity
from lightgraph.construction.identity_authority import source_address
from lightgraph.construction.observed_identity import contradictory_qualifiers
from lightgraph.construction.merger import _norm_label
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.source_values import identity_cell, original_key_types


def _typed_cells(stored, names):
    values = stored.get('identity') or {}
    types = stored.get('identity_types') or {}
    result = []
    for key in names:
        if key not in values or values[key] is None or values[key] == '':
            return None
        try:
            cell = identity_cell(stored, key)
        except (InvalidOperation, LightGraphError, ValueError, TypeError):
            return None
        if key in types and types[key] != cell[0]:
            return None
        result.append(cell)
    return tuple(result) if result else None


def _physical_values(node, support, columns, names):
    """Use the support's original keys after a remap. A composite key needs a known order."""
    if 'original_key_values' in support:
        values = support['original_key_values']
        if not isinstance(values, dict) or set(values) != set(columns):
            return None
        types = (support.get('original_key_types')
                 or original_key_types(values, support.get('identity_origins') or []))
        if set(types) != set(columns):
            return None
        stored = {'identity': values, 'identity_types': types}
        names = columns
    else:
        if support.get('identity_basis') or support.get('identity_origins'):
            return None
        stored = node
    if len(columns) != len(names) or set(names) != set(stored.get('identity') or {}):
        return None
    return _typed_cells(stored, names)


def _partitions(groups):
    """Connected candidate blocks partition the review workload. Links do not confer equivalence."""
    neighbors = defaultdict(list)
    for index, group in enumerate(groups):
        for nid in group['node_ids']:
            neighbors[nid].append(index)
    result = []
    while neighbors:
        stack = [next(iter(neighbors))]
        nodes, indices = set(), set()
        while stack:
            nid = stack.pop()
            if nid in nodes:
                continue
            nodes.add(nid)
            for index in neighbors.pop(nid):
                if index not in indices:
                    indices.add(index)
                    stack.extend(groups[index]['node_ids'])
        node_ids = sorted(nodes)
        result.append({'component_id': 'nc_' + digest('node_census_component_v1', node_ids),
                       'node_ids': node_ids, 'group_ids': sorted(groups[i]['group_id'] for i in indices)})
    return sorted(result, key=lambda row: row['component_id'])


def _summarize_group(ids, owners, audiences, codes, observed):
    known = [owners[nid] for nid in ids if owners[nid]]
    occurrences = Counter(unit for units in known for unit in units)
    within = any(count >= 2 for count in occurrences.values())
    across = len(known) >= 2 and len(occurrences) >= 2
    unit_span = ('within_and_across' if within and across else 'across' if across
                 else 'within' if within else 'unknown')
    scopes = {audiences[nid] for nid in ids}
    missing_scopes = sum(not audiences[nid] or None in audiences[nid] for nid in ids)
    known_codes = {codes[nid] for nid in ids if codes[nid] is not None}
    label_sets = [observed[nid] for nid in ids]
    return {
        'unit_span': unit_span, 'contributors': sorted(occurrences),
        'members_without_contributor': len(ids) - len(known),
        'scope_evidence': {'members_with_incomplete_scope': missing_scopes,
                           'distinct_recorded_scope_sets': len(scopes - {frozenset()}),
                           'all_scope_sets_equal_and_complete': missing_scopes == 0 and len(scopes) == 1},
        'distinct_typed_key_values': len(known_codes),
        'members_without_typed_key': sum(codes[nid] is None for nid in ids),
        'members_without_observed_name': sum(not names for names in label_sets),
        'members_with_multiple_observed_names': sum(len(names) > 1 for names in label_sets),
        'all_have_same_single_observed_name': len(label_sets[0]) == 1 and all(
            names == label_sets[0] for names in label_sets),
    }


def analyze_nodes(graph, *, contributor_id=None, excluded_values=()):
    """Enumerate every indexed block with at least two distinct live nodes and report uncovered nodes.

    All source values and member IDs are local audit data. Physical columns and driver scalar
    types participate in comparison. Only an actual authorized simulation can establish a reduction.
    """
    nodes = graph.get('nodes', [])
    by_id = {n['id']: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('Duplicate node IDs in census input')
    definitions = {d['type']: d for d in (graph.get('definitions') or {}).get('entities', [])}
    blocks = defaultdict(set)
    owners, audiences, codes, observed = {}, {}, {}, {}
    projection_blocks = defaultdict(lambda: defaultdict(set))
    incomplete = Counter()

    def add(basis, key, nid):
        blocks[basis, canonical(key)].add(nid)

    for node in nodes:
        nid, kind = node['id'], node['type']
        definition = definitions.get(kind, {})
        family = node.get('original_type') or definition.get('original_type') or kind
        identity = node.get('identity') or {}
        names = definition.get('key_names') or (list(identity) if len(identity) == 1 else [])
        ordered = sorted(identity)
        typed = _typed_cells(node, ordered)
        codes[nid] = None
        if typed:
            cells = dict(zip(ordered, typed))
            add('same_type_typed_identity', [kind, cells], nid)
            if names and set(names) == set(cells) and len(names) == len(cells):
                values = [cells[k] for k in names]
                codes[nid] = canonical(values)
                # Different axes, logical key names and namespaces can still share the same scalar.
                add('typed_key_values', values, nid)
        if codes[nid] is None:
            incomplete['nodes_without_complete_ordered_key'] += 1

        supports = node.get('supports') or []
        units = {s['contributor_id'] for s in supports if s.get('contributor_id')}
        if contributor_id and (not supports or any(not s.get('contributor_id') for s in supports)):
            units.add(contributor_id)
        owners[nid] = units
        audiences[nid] = frozenset(
            tuple(sorted({s['scope_ref'], *(s.get('required_scope_refs') or [])})) if s.get('scope_ref') else None
            for s in supports)
        if not units:
            incomplete['nodes_without_contributor'] += 1
        labels = {label for s in supports if isinstance(label := s.get('observed_label') or s.get('label'), str)
                  and label.strip()}
        if not labels and node.get('label_origin', 'source' if 'supports' not in node else None) == 'source':
            label = node.get('label')
            if isinstance(label, str) and label.strip():
                labels.add(label)
        observed[nid] = labels
        display = node.get('display_label') or node.get('label')
        if isinstance(display, str) and display.strip():
            add('display_label', _norm_label(display), nid)
        if not labels:
            incomplete['nodes_without_observed_name'] += 1
        aliases = {a for s in supports for a in (s.get('aliases') or []) if isinstance(a, str)}
        aliases.update(a for a in (node.get('aliases') or []) if isinstance(a, str))
        for name in {_norm_label(label) for label in labels | aliases} - {''}:
            add('observed_name_or_alias', name, nid)

        bound = False
        for support in supports:
            provenance = support.get('provenance') or {}
            address = source_address(provenance.get('source'))
            cols = provenance.get('key_columns')
            if not (address and isinstance(cols, list) and cols and all(isinstance(c, str) and c for c in cols)
                    and len(set(cols)) == len(cols)) or support.get('inherited_from'):
                incomplete['supports_without_comparable_physical_binding'] += 1
                continue
            values = _physical_values(node, support, cols, names)
            if values is None:
                incomplete['supports_without_comparable_physical_binding'] += 1
                continue
            bound = True
            add('physical_typed_key', [*address, cols, values], nid)
            unit = support.get('contributor_id') or contributor_id
            if unit:
                # Keep projections for all branches sharing this key together, without enumerating pairs.
                projection = (unit, *address, tuple(cols), kind)
                key = (family, tuple(cols), values)
                projection_blocks[key][projection].add(nid)
        if not bound:
            incomplete['nodes_without_comparable_physical_binding'] += 1

    pattern_keys = defaultdict(list)
    for (family, columns, values), bindings in projection_blocks.items():
        if len(bindings) < 2:
            continue
        ids = sorted(set.union(*bindings.values()))
        if len(ids) < 2:
            continue
        signature = (family, columns, tuple(sorted(bindings)))
        block_key = [family, columns, values, signature[2]]
        for nid in ids:
            add('same_family_axis_across_projections', block_key, nid)
        pattern_keys[signature].append(canonical(block_key))
    del projection_blocks

    unresolved = []
    recorded = Counter()
    for source, field in [('candidate_identity_merges', 'node_ids'), ('candidate_column_alignments', 'columns')]:
        for record in graph.get(source, []):
            members = record.get(field) or []
            ids = (set(members) if field == 'node_ids' else {item['node_id'] for item in members})
            recorded[source] += 1
            missing = sorted(nid for nid in ids if nid not in by_id)
            key = [record.get('reason'), record.get('match_basis'), record.get('candidate_id'), sorted(ids)]
            if missing or len(ids) < 2:
                unresolved.append({'source': source, 'key': key, 'missing_node_ids': missing})
                continue
            blocks[source, canonical(key)].update(ids)

    groups = []
    axis_groups = {}
    by_basis, by_span = Counter(), Counter()
    coverage_by_basis = defaultdict(set)
    for (basis, key), members in sorted(blocks.items()):
        if len(members) < 2:
            continue
        ids = sorted(members)
        gid = 'ng_' + digest('node_census_block_v1', basis, key, ids)
        row = {'group_id': gid, 'basis': basis, 'key': json.loads(key), 'node_ids': ids,
               'status': 'candidate_only', **_summarize_group(ids, owners, audiences, codes, observed)}
        if basis == 'same_family_axis_across_projections':
            axis_groups[key] = gid
        groups.append(row)
        by_basis[basis] += 1
        by_span[row['unit_span']] += 1
        coverage_by_basis[basis].update(members)
    components = _partitions(groups)
    candidate_nodes = sum(len(c['node_ids']) for c in components)
    patterns = [{'pattern_id': 'np_' + digest('node_census_projection_pattern_v1', sig),
                 'entity_family': sig[0], 'key_columns': list(sig[1]),
                 'projections': [{'contributor_id': owner, 'source_id': source, 'schema': schema,
                                  'table': table, 'key_columns': list(columns), 'output_type': kind}
                                 for owner, source, schema, table, columns, kind in sig[2]],
                 'overlapping_keys': len(keys), 'group_ids': sorted(axis_groups[k] for k in keys)}
                for sig, keys in sorted(pattern_keys.items())]
    return {'summary': {'total_nodes': len(by_id), 'candidate_nodes': candidate_nodes,
                        'nodes_outside_candidate_blocks': len(nodes) - candidate_nodes,
                        'candidate_groups': len(groups), 'review_components': len(components),
                        'by_basis': dict(by_basis), 'candidate_nodes_by_basis': {
                            k: len(v) for k, v in sorted(coverage_by_basis.items())},
                        'by_unit_span': dict(by_span), 'projection_patterns': len(patterns),
                        'recorded_candidate_rows': dict(recorded), 'unresolved_recorded_groups': len(unresolved),
                        'evidence_gaps': dict(incomplete)},
            'groups': groups, 'components': components, 'projection_patterns': patterns,
            'physical_hypothesis': _physical_hypothesis(
                graph, groups, by_id, definitions, contributor_id, excluded_values),
            'unresolved_recorded_groups': unresolved}


def _physical_role_profiles(graph, candidates):
    """Constrain a possible identity by every incident non-self edge's directed, mapped role."""
    roles, incomplete, adjacent = defaultdict(set), set(), defaultdict(set)
    definitions = {r['type']: r for r in (graph.get('definitions') or {}).get('relations', [])}
    for edge in graph.get('edges', []):
        if edge['source'] == edge['target']:
            continue
        incident = [(end, edge[end]) for end in ('source', 'target') if edge[end] in candidates]
        if not incident:
            continue
        if len(incident) == 2:
            adjacent[edge['source']].add(edge['target'])
        relation = definitions.get(edge['type']) or {}
        supports = edge.get('supports') or []
        if not supports:
            incomplete.update(nid for _, nid in incident)
        for s in supports:
            prov = s.get('provenance') or {}
            address = source_address(prov.get('source'))
            cols = [prov.get(k) for k in ('source_key_columns', 'target_key_columns')]
            if (not address or s.get('inherited_from') or not relation or
                    not all(isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys)
                            for keys in cols)):
                incomplete.update(nid for _, nid in incident)
                continue
            family = relation.get('original_type') or edge.get('original_type') or edge['type']
            properties = [prov.get(k) for k in ('predicate', 'constant_properties', 'source_lookup', 'target_lookup')]
            for end, nid in incident:
                roles[nid].add(canonical([end, family, address, cols, properties, relation.get('hierarchy_direction')]))
    return roles, incomplete, adjacent


def _physical_member(node, definition, fallback_owner, excluded, roles, incomplete):
    family = node.get('original_type') or definition.get('original_type') or node['type']
    identity = node.get('identity') or {}
    if any(canonical(v) in excluded for v in identity.values()):
        return None, 'excluded_key_value'
    if node.get('identity_origin', definition.get('identity_origin', 'row')) != 'row':
        return None, 'non_row_identity'
    names = definition.get('key_names') or []
    declared_scope = definition.get('identity_scope')
    if (not names or set(names) != set(identity) or declared_scope not in {'global', 'scoped'}
            or not definition.get('identity_domain')):
        return None, 'incomplete_identity_contract'
    context_keys = definition.get('identity_context_keys') or []
    if (any(k not in names for k in context_keys) or (declared_scope == 'scoped' and not context_keys)
            or (declared_scope == 'global' and context_keys)):
        return None, 'unverified_identity_context_keys'
    bindings, contexts, labels, audiences = set(), set(), set(), set()
    units, drafts = set(), set()
    complete_drafts = True
    for support in node.get('supports') or []:
        unit = support.get('contributor_id') or fallback_owner
        if (not unit or support.get('inherited_from') or support.get('identity_decision_refs')
                or support.get('lookup_matches')):
            return None, 'unverified_or_previously_reviewed_origin'
        units.add(unit)
        origins = support.get('execution_origins') or [support.get('validation_origin') or {}]
        for o in origins:
            if o.get('contributor_id', unit) != unit:
                return None, 'unverified_contributor'
            if not o.get('draft_id') or o.get('draft_revision', o.get('revision')) is None:
                complete_drafts = False
            else:
                drafts.add((unit, o['draft_id']))
        prov = support.get('provenance') or {}
        cols = prov.get('key_columns')
        address = source_address(prov.get('source'))
        if (not address or not isinstance(cols, list) or not cols
                or not all(isinstance(c, str) and c for c in cols) or len(set(cols)) != len(cols)):
            return None, 'incomplete_physical_key'
        values = _physical_values(node, support, cols, names)
        if values is None:
            return None, 'incomplete_physical_key'
        if any(canonical(v) in excluded for _, v in values):
            return None, 'excluded_key_value'
        label = support.get('observed_label') or support.get('label')
        if not isinstance(label, str) or not label.strip() or not prov.get('label_column'):
            return None, 'missing_observed_name_binding'
        row = support.get('row_context') or {}
        row_values = row.get('values') or {}
        known_cols = [c for c in cols if c in row_values]
        if known_cols:
            cells = _typed_cells({'identity': row_values, 'identity_types': row.get('value_types') or {}}, known_cols)
            if cells is None or any(cell != values[cols.index(c)] for c, cell in zip(known_cols, cells)):
                return None, 'contradictory_key_or_name_observation'
        label_col = prov['label_column']
        if label_col in row_values and row_values[label_col] != label:
            return None, 'contradictory_key_or_name_observation'
        labels.add(label)
        if not support.get('scope_ref'):
            return None, 'missing_audience'
        audience = tuple(sorted({support['scope_ref'], *(support.get('required_scope_refs') or [])}))
        audiences.add(audience)
        ctx = business_context_identity(support)
        if ctx['status'] == 'unknown':
            return None, 'unverified_business_scope'
        if context_keys:
            context_cells = _typed_cells({'identity': ctx['dimensions'], 'identity_types': ctx['value_types']}, context_keys)
            if context_cells is None or context_cells != _typed_cells(node, context_keys):
                return None, 'unverified_identity_context_keys'
        contexts.add(canonical(ctx))
        # Keep the audience and context correlated to their actual source observations.
        bindings.add(canonical([address, cols, values, prov['label_column'], audience, ctx,
                                sorted(cols[names.index(k)] for k in context_keys)]))
    if not bindings or len(labels) != 1:
        return None, 'missing_or_conflicting_observed_names'
    if len(units) != 1:
        return None, 'composite_or_unknown_contributor'
    if node['id'] in incomplete or not roles.get(node['id']):
        return None, 'unverified_incident_role'
    return {'unit': next(iter(units)), 'drafts': drafts if complete_drafts else set(),
            'family': family, 'role': node.get('node_role'), 'roles': roles[node['id']],
            'bindings': bindings, 'scope': declared_scope, 'contexts': contexts,
            'audiences': audiences, 'label': next(iter(labels))}, None


def _group_difference(nids, facts, by_id):
    for nid in nids:
        if facts[nid][1]:
            return facts[nid][1]
    members = [facts[nid][0] for nid in nids]
    first = members[0]
    for key, reason in [('label', 'different_observed_names'), ('scope', 'different_identity_scope'),
                        ('contexts', 'different_business_scopes'), ('audiences', 'different_audiences'),
                        ('bindings', 'different_complete_bindings'), ('role', 'different_node_roles'),
                        ('roles', 'different_incident_roles')]:
        if any(m[key] != first[key] for m in members):
            return reason
    if len({m['family'] for m in members}) > 1 and not first['role']:
        return 'cross_family_without_declared_shared_role'
    # Distinct members within a unit need independent versioned drafts. Two
    # output IDs claimed by the same draft represent an ambiguous projection.
    by_unit = defaultdict(list)
    for m in members:
        by_unit[m['unit']].append(m['drafts'])
    for drafts in by_unit.values():
        if len(drafts) > 1 and (not all(drafts) or len(set.union(*drafts)) != sum(map(len, drafts))):
            return 'ambiguous_or_unversioned_same_unit_projection'
    if contradictory_qualifiers(by_id[nid] for nid in nids):
        return 'conflicting_identity_qualifiers'
    return None


def _physical_hypothesis(graph, groups, by_id, definitions, contributor_id, excluded_values):
    member_sets = defaultdict(list)
    for group in groups:
        if group['basis'] == 'physical_typed_key':
            member_sets[tuple(group['node_ids'])].append(group['group_id'])
    claims = Counter(nid for members in member_sets for nid in members)
    roles, incomplete, adjacent = _physical_role_profiles(graph, claims)
    excluded = {canonical(v) for v in excluded_values}
    facts = {nid: _physical_member(by_id[nid], definitions.get(by_id[nid]['type'], {}),
                                  contributor_id, excluded, roles, incomplete) for nid in claims}
    lanes = {k: {'groups': [], 'hypothetical_node_reduction': 0} for k in ('within_only', 'cross_only', 'joint')}
    rejected, counts = [], Counter()
    for ids, witnesses in sorted(member_sets.items()):
        group_set = set(ids)
        if any(claims[n] > 1 for n in ids):
            problem = 'overlapping_competing_member_sets'
        elif any(other in group_set for n in ids for other in adjacent.get(n, ())):
            problem = 'distinct_endpoints_of_recorded_relationship'
        else:
            problem = _group_difference(ids, facts, by_id)
        if problem:
            counts[problem] += 1
            rejected.append({'node_ids': list(ids), 'physical_group_ids': sorted(witnesses), 'reason': problem})
            continue
        by_unit = defaultdict(list)
        for nid in ids:
            by_unit[facts[nid][0]['unit']].append(nid)
        cases = {'joint': [ids], 'within_only': [part for part in by_unit.values() if len(part) >= 2],
                 'cross_only': [ids] if len(by_unit) == len(ids) else []}
        for lane, subsets in cases.items():
            for part in subsets:
                lanes[lane]['groups'].append({'node_ids': list(part), 'physical_group_ids': sorted(witnesses),
                                             'units': sorted({facts[n][0]['unit'] for n in part}),
                                             'types': sorted({by_id[n]['type'] for n in part})})
                lanes[lane]['hypothetical_node_reduction'] += len(part) - 1
    return {'mode': 'source_anchored_counterfactual_only', 'excluded_values': list(excluded_values),
            'physical_key_blocks': sum(map(len, member_sets.values())), 'distinct_member_sets': len(member_sets),
            'rejected_member_sets': len(rejected), 'rejection_counts': dict(counts),
            'rejections': rejected, **lanes}

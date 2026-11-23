"""Recognize independent global declarations that project the same physical hierarchy."""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from itertools import combinations

from lightgraph.contracts import canonical, digest
from lightgraph.source_values import identity_cell
from .fusion import definition_contract, relation_identity, remap_graph_endpoints
from .hierarchy_captions import HierarchyCaptions
from .hierarchy_neighborhood import neighborhood_difference_counts
from .hierarchy_observations import (edge_observation_properties, observation_boundary, observation_properties,
                                     worksheet_edge_boundaries)
from .hierarchy_role_witnesses import IsolatedRoleIndex
from .hierarchy_worksheet_properties import worksheet_property_matches


def _address(prov):
    source = prov.get('source') or {}
    address = tuple(source.get(k) for k in ('source_id', 'schema', 'table'))
    return address if all(isinstance(k, str) and k for k in address) else None


def _level_positions(graph, eligible, relations):
    """Index simple, physically witnessed level chains. Disjoint hierarchies keep separate windows.

    A declared direction gives the layout of physical columns, not proof that
    two nodes or edges are equivalent. The latter is checked for each member.
    Composite keys can contain a project or tenant column and are not used
    to infer projection-only fields.
    """
    bindings = {}
    for node in eligible:
        slots = set()
        for support in node.get('supports', ()):
            prov = support.get('provenance') or {}
            keys = prov.get('key_columns') or ()
            if len(keys) == 1 and not support.get('inherited_from'):
                slots.add((_address(prov), keys[0]))
        bindings[node['id']] = (node['type'], slots)
    declared = defaultdict(set)
    families = defaultdict(set)
    for edge in graph['edges']:
        left = bindings.get(edge['source'])
        right = bindings.get(edge['target'])
        definition = relations.get(edge['type'], {})
        direction = definition.get('hierarchy_orientation')
        if (not left or not right or left[0] != right[0] or direction == 'none'
                or definition.get('source_type') != left[0] or definition.get('target_type') != right[0]):
            continue
        for support in edge.get('supports', ()):
            prov = support.get('provenance') or {}
            address = _address(prov)
            src = prov.get('source_key_columns') or ()
            dst = prov.get('target_key_columns') or ()
            if (not address or len(src) != 1 or len(dst) != 1 or src[0] == dst[0]
                    or (address, src[0]) not in left[1] or (address, dst[0]) not in right[1]
                    or support.get('inherited_from') or support.get('lookup_matches')
                    or prov.get('source_lookup') or prov.get('target_lookup')):
                continue
            pair = tuple(sorted((src[0], dst[0])))
            families[left[0], address].add(pair)
            if direction in ('parent_to_child', 'child_to_parent'):
                declared[address, pair].add(src[0] if direction == 'parent_to_child' else dst[0])

    layouts = {}
    for (kind, address), pairs in families.items():
        adjacent, parents = defaultdict(set), defaultdict(set)
        ambiguous = set()
        for a, b in pairs:
            adjacent[a].add(b)
            adjacent[b].add(a)
            directions = declared[address, (a, b)]
            if len(directions) != 1:
                ambiguous.update((a, b))
            else:
                parent, = directions
                parents[b if parent == a else a].add(parent)
        unseen = set(adjacent)
        while unseen:
            pending = [unseen.pop()]
            component = set(pending)
            while pending:
                others = adjacent[pending.pop()] & unseen
                unseen.difference_update(others)
                component.update(others)
                pending.extend(others)
            roots = [c for c in component if not parents[c]]
            if (len(roots) != 1 or ambiguous & component
                    or any(len(parents[c]) > 1 or len(adjacent[c] - parents[c]) > 1 for c in component)):
                continue
            positions = {}
            column = roots[0]
            while column is not None and column not in positions:
                positions[column] = 1 + len(positions)
                column = next(iter(adjacent[column] - parents[column]), None)
            if len(positions) == len(component):
                for c in component:
                    layouts[kind, address, (c,)] = positions
    return layouts


@dataclass
class _Member:
    address: tuple
    value: str
    bindings: dict[tuple, frozenset]
    observations: frozenset
    unit: str
    has_label_conflict: bool
    raw_key: tuple


def _row_matches_endpoint(row, columns, raw_key):
    values, types = row.get('values') or {}, row.get('value_types') or {}
    return all(key not in values or (types.get(key) == kind and values[key] == value)
               for key, (kind, value) in zip(columns, raw_key, strict=True))


def _member(node, definition, layouts, captions=None):
    names = definition.get('key_names') or []
    identity = node.get('identity') or {}
    types = node.get('identity_types') or {}
    if (definition.get('identity_scope') != 'global' or definition.get('identity_context_keys')
            or not definition.get('identity_domain') or node.get('identity_domain') != definition['identity_domain']
            or (node.get('identity_context') or {}).get('status') != 'global'
            or (node.get('identity_context') or {}).get('dimensions')
            or node.get('identity_origin', definition.get('identity_origin', 'row')) != 'row'
            or not names or set(names) != set(identity) or set(names) != set(types)
            or any(identity[k] is None or identity[k] == '' for k in names)):
        return None
    cells = [identity_cell(node, k) for k in names]
    if any(types[k] != cell[0] for k, cell in zip(names, cells)):
        return None
    raw_key = tuple((types[k], identity[k]) for k in names)
    addresses, units, labels, observations = set(), set(), set(), set()
    for support in node.get('supports') or []:
        prov = support.get('provenance') or {}
        table = _address(prov)
        keys = tuple(prov.get('key_columns') or [])
        label = support.get('observed_label') or support.get('label')
        positions = layouts.get((node['type'], table, keys), {})
        display = (captions.witnessed(support, node['type'], table, positions, dict(zip(keys, raw_key)))
                   if captions else ())
        boundary = observation_boundary(support, keys, positions, witnessed_captions=display) if prov else None
        if (not table or not prov.get('label_column') or not isinstance(label, str) or not label.strip() or boundary is None
                or len(keys) != len(names) or not all(isinstance(k, str) and k for k in keys)
                or len(set(keys)) != len(keys) or support.get('identity_basis')
                or support.get('original_key_values') is not None):
            return None
        # If the row also names its key, it independently constrains the
        # projection. A stale or contradictory row may not certify this node.
        if not _row_matches_endpoint(support.get('row_context') or {}, keys, raw_key):
            return None
        unit = support.get('contributor_id')
        if not unit or any(o.get('contributor_id', unit) != unit for o in support.get('execution_origins', [])):
            return None
        addresses.add(table)
        units.add(unit)
        labels.add(label)
        observations.add((keys, prov['label_column'], label, boundary, observation_properties(support, keys, positions)))
    if len(addresses) != 1 or len(units) != 1 or not labels:
        return None
    # Each edge declares exactly which key columns it observed. Facts about
    # another occurrence of this member in a different hierarchy slot cannot
    # witness that edge. The member's own identity still uses every observation.
    per_binding = defaultdict(set)
    for fact in observations:
        per_binding[fact[0]].add(fact)
    bindings = {keys: frozenset(facts) for keys, facts in per_binding.items()}
    return _Member(addresses.pop(), canonical(cells), bindings, frozenset(observations),
                   units.pop(), len(labels) > 1, raw_key)


def _worksheet_edge_facts(entries, stats):
    projections = [(edge_id, support, fact) for edge_id, _, _, _, support, fact, _ in entries]
    sections = worksheet_edge_boundaries(projections, stats)
    matches = worksheet_property_matches(projections, sections)
    targets = {}
    directions = defaultdict(set)
    if matches:
        for index, (entry, boundaries) in enumerate(zip(entries, sections, strict=True)):
            _, _, columns, orientation, support, original, layout = entry
            residuals = {}
            for boundary in boundaries or ():
                old = (boundary, original[1])
                new = old
                if match := matches.get(index, {}).get(boundary):
                    aligned, names = match
                    if names not in residuals:
                        residuals[names] = edge_observation_properties(
                            support, columns, layout, orientation, worksheet_properties=names)
                    new = (aligned, residuals[names])
                # An old fact must still have one representation across all
                # owners. Rewriting only one copy can exchange an existing pair.
                if targets.setdefault(old, new) != new:
                    targets[old] = old
                if orientation in ('parent_to_child', 'child_to_parent'):
                    parent = columns[0 if orientation == 'parent_to_child' else 1]
                    directions[old].add(parent)
                    directions[new].add(parent)
    for entry, boundaries in zip(entries, sections, strict=True):
        if boundaries is None:
            yield entry, None
            continue
        facts = []
        changed = False
        for boundary in boundaries:
            old = (boundary, entry[5][1])
            target = targets.get(old, old)
            if target != old and len(directions[target]) <= 1:
                changed = True
                facts.append(target)
            else:
                facts.append(old)
        if changed:
            key = 'worksheet_property_corroborated_supports'
            stats[key] = stats.get(key, 0) + 1
        yield entry, facts


def _neighborhoods(graph, nodes, members, relations, layouts, captions, stats):
    """Compare parent and child observations in physical coordinates, including reverse encodings."""
    incidents = defaultdict(set)
    blocked = set()
    known = defaultdict(set)
    physical_edges = defaultdict(list)
    observations = []
    edge_rows = defaultdict(list)
    original_boundaries = defaultdict(set)
    for edge in graph['edges']:
        endpoints = (edge['source'], edge['target'])
        if not any(n in members for n in endpoints):
            continue
        left, right = (members.get(n) for n in endpoints)
        relation = relations.get(edge['type'], {})
        orientation = relation.get('hierarchy_orientation')
        if (not left or not right or left.address != right.address or nodes[endpoints[0]]['type'] != nodes[endpoints[1]]['type']
                or relation.get('source_type') != nodes[endpoints[0]]['type']
                or relation.get('target_type') != nodes[endpoints[1]]['type']
                or orientation == 'none' or not edge.get('supports')):
            blocked.update(endpoints)
            continue
        for support in edge['supports']:
            prov = support.get('provenance') or {}
            columns = tuple(tuple(prov.get(end + '_key_columns') or []) for end in ('source', 'target'))
            level_index = layouts.get((nodes[endpoints[0]]['type'], left.address, columns[0]), {})
            all_keys = columns[0] + columns[1]
            row = support.get('row_context') or {}
            display = captions.witnessed(support, nodes[endpoints[0]]['type'], left.address, level_index,
                                         dict(zip(all_keys, left.raw_key + right.raw_key))) if prov else ()
            boundary = observation_boundary(support, all_keys, level_index, witnessed_captions=display) if prov else None
            if (_address(prov) != left.address or boundary is None or columns[0] not in left.bindings
                    or columns[1] not in right.bindings
                    or not _row_matches_endpoint(row, columns[0], left.raw_key)
                    or not _row_matches_endpoint(row, columns[1], right.raw_key)
                    or (columns[0] == columns[1] and endpoints[0] != endpoints[1])
                    or prov.get('source_lookup') or prov.get('target_lookup')):
                blocked.update(endpoints)
                continue
            if endpoints[0] == endpoints[1]:
                continue
            fact = (boundary, edge_observation_properties(support, columns, level_index, orientation))
            # A declared direction only corroborates projections of this exact
            # physical edge and applicability, never unrelated rows in the table.
            positions = [(col, member.value, member.bindings[col])
                         for col, member in zip(columns, (left, right))]
            physical_pair = (left.address, tuple(sorted(positions, key=lambda pos: pos[0])))
            physical_edges[physical_pair].append((edge['id'], endpoints, columns, orientation, support, fact, level_index))
    while physical_edges:
        physical_pair, entries = physical_edges.popitem()
        for entry, facts in _worksheet_edge_facts(entries, stats):
            _, endpoints, columns, orientation, support, original_fact, level_index = entry
            if facts is None:
                blocked.update(endpoints)
                continue
            row = support.get('row_context') or {}
            ancestor_facts = {original_fact}
            for fact in facts:
                pair = (*physical_pair, fact)
                if orientation in ('parent_to_child', 'child_to_parent'):
                    known[pair].add(columns[0 if orientation == 'parent_to_child' else 1])
                observations.append((pair, columns, endpoints, fact, original_fact[0]))
                ancestor_facts.add(fact)
            if row.get('values') and level_index:
                for index, nid in enumerate(endpoints):
                    neighbor = members[endpoints[1 - index]]
                    for fact in ancestor_facts:
                        edge_rows[nid].append((columns[index], (columns[1 - index], neighbor.value),
                                              row, level_index, fact))
    missing_orientation = set()
    for pair, columns, endpoints, boundary, original in observations:
        if len(known[pair]) != 1:
            missing_orientation.update(endpoints)
            continue
        parent, = known[pair]
        for index in (0, 1):
            other = 1 - index
            neighbor = members[endpoints[other]]
            incident = (columns[index] == parent, columns[index], columns[other], neighbor.value,
                        neighbor.bindings[columns[other]], boundary)
            incidents[endpoints[index]].add(incident)
            if boundary[0] != original:
                original_boundaries[endpoints[index], incident].add(original)
    return incidents, blocked, missing_orientation, edge_rows, original_boundaries


def _complementary_paths_agree(left, right):
    """Node-only and edge-only witnesses can conflict even when no carrier matches.

    An incident edge can cover a subset of a member's paths. Only veto when
    the two sets have no compatible tuple on their jointly recorded columns.
    """
    for scope in left.keys() & right.keys():
        if left[scope].keys() & right[scope].keys():
            continue
        windows = [next(iter(side[scope].values())) for side in (left, right)]
        common = set.intersection(*(set(cols) for side in windows for cols, _ in side))
        if not common:
            continue
        projected = []
        for side in windows:
            values = set()
            for cols, rows in side:
                indices = [i for i, c in enumerate(cols) if c in common]
                values.update(tuple(row[i] for i in indices) for row in rows)
            projected.append(values)
        if projected[0].isdisjoint(projected[1]):
            return False
    return True


def _ancestor_paths_agree(batch, nodes, layouts, edge_rows, *, members=None, captions=None):
    """Compare observed ancestor tuples within the same applicability and row context."""
    views = []
    comparing_worksheet_rows = any(
        observation is not None and isinstance(observation[0].row_context, tuple)
        for nid in batch for _, _, _, _, observation in edge_rows.get(nid, ()))
    for nid in batch:
        node = nodes[nid]
        view = defaultdict(lambda: defaultdict(set))
        rows_to_compare = list(edge_rows.get(nid, ()))
        for support in node['supports']:
            prov = support['provenance']
            keys = tuple(prov['key_columns'])
            address = _address(prov)
            layout = layouts.get((node['type'], address, keys))
            row = support.get('row_context') or {}
            if not layout or not row.get('values'):
                continue
            display = (captions.witnessed(support, node['type'], address, layout,
                                          dict(zip(keys, members[nid].raw_key)))
                       if captions is not None else ())
            observation = (observation_boundary(support, keys, layout, witnessed_captions=display),
                           observation_properties(support, keys, layout))
            rows_to_compare.append((keys, None, row, layout, observation))
        for keys, neighbor, row, layout, observation in rows_to_compare:
            if not layout or not row.get('values'):
                continue
            columns = tuple(sorted(c for c in row['values'] if c in layout and layout[c] < layout[keys[0]]))
            if columns:
                types = row.get('value_types') or {}
                path = tuple(canonical([types.get(c), row['values'][c]]) for c in columns)
                # Keep the unpartitioned check even when the projections
                # recorded ancestry for disjoint subsets of their observations.
                view[keys, neighbor, None][columns].add(path)
                view[keys, neighbor, observation][columns].add(path)
                if comparing_worksheet_rows and observation is not None:
                    # A node may supply an ancestor missing on the other
                    # projection's edge (or vice versa). Extra row columns on
                    # one carrier may not hide a contradiction between them.
                    boundary, _ = observation
                    view[keys, neighbor, ('worksheet_ancestors', boundary.scope)][columns].add(path)
        carriers = defaultdict(lambda: defaultdict(list))
        for (keys, neighbor, observation), windows in view.items():
            if observation is not None:
                carriers[keys, observation][neighbor is None].extend(windows.items())
        views.append((view, carriers))
    for (left, left_carriers), (right, right_carriers) in combinations(views, 2):
        if not _complementary_paths_agree(left_carriers, right_carriers):
            return False
        for keys in left.keys() & right.keys():
            for cols, rows in left[keys].items():
                for other_cols, other_rows in right[keys].items():
                    common = set(cols).intersection(other_cols)
                    if common:
                        a = [i for i, c in enumerate(cols) if c in common]
                        b = [i for i, c in enumerate(other_cols) if c in common]
                        if {tuple(r[i] for i in a) for r in rows} != {tuple(r[i] for i in b) for r in other_rows}:
                            return False
    return True


def _observation_differences(signatures):
    """Identify which parts of the source observations disagree without reporting their values."""
    components = defaultdict(set)
    for signature in signatures:
        observed = defaultdict(set)
        for keys, label_column, _, boundary, props in signature:
            observed['physical_bindings'].add((keys, label_column))
            observed['applicability'].add((keys, boundary.scope))
            observed['row_context'].add((keys, boundary.row_context))
            observed['predicate'].add((keys, boundary.predicate))
            observed['properties'].add((keys, props))
        for field, values in observed.items():
            components[field].add(frozenset(values))
    return [field for field, values in components.items() if len(values) > 1] or ['observation_combinations']


def _cross_unit_pairs(ids, members):
    """Count disjoint contributor pairs without enumerating the Cartesian product."""
    pairs = previous = 0
    for count in Counter(members[nid].unit for nid in ids).values():
        pairs += previous * count
        previous += count
    return pairs


def _verified_groups(graph, excluded_types, stats, physical_sets):
    nodes = {n['id']: n for n in graph['nodes']}
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    relations = {r['type']: r for r in graph['definitions']['relations']}
    eligible = [n for n in nodes.values() if n['type'] not in excluded_types and
                definitions.get(n['type'], {}).get('original_type') not in excluded_types and
                definitions.get(n['type'], {}).get('identity_scope') == 'global']
    stats['global_node_inputs'] = len(eligible)
    if not eligible:
        return
    members = {}
    rejected_nodes = Counter()
    physical_groups = defaultdict(set)
    catalog = graph.get('evidence_catalog', {})
    layouts = _level_positions(graph, eligible, relations)
    captions = HierarchyCaptions()
    for node in eligible:
        definition = definitions[node['type']]
        refs = definition.get('identity_evidence_refs') or []
        if not refs or any(catalog.get(ref, {}).get('kind') != 'source_probe' for ref in refs):
            rejected_nodes['authoritative_or_unverified_domain'] += 1
            continue
        member = _member(node, definition, layouts)
        if member:
            members[node['id']] = member
            captions.record(node, member, layouts)
            for binding in member.bindings:
                physical_groups[member.address, binding, member.value].add(node['id'])
        else:
            rejected_nodes['incomplete_or_incompatible_source_observation'] += 1
    stats['excluded_nodes'] = dict(rejected_nodes)
    stats['endpoint_only_multilabel_nodes'] = sum(m.has_label_conflict for m in members.values())
    stats['verified_source_node_inputs'] = len(members) - stats['endpoint_only_multilabel_nodes']
    if not any(len(ids) > 1 for ids in physical_groups.values()):
        return
    # Caption witnesses come only from members which passed the original
    # identity checks. Recompare only nodes whose rows actually project them.
    for nid, member in members.items():
        node = nodes[nid]
        roles = captions.columns.get((member.address, member.unit, node['type']), {})
        if roles and any(roles.keys() & ((s.get('row_context') or {}).get('values') or {}).keys()
                         for s in node['supports']):
            members[nid] = _member(node, definitions[node['type']], layouts, captions)
    incidents, invalid_roles, missing_orientation, edge_rows, original_boundaries = _neighborhoods(
        graph, nodes, members, relations, layouts, captions, stats)
    blocked = set()
    isolation = graph.get('conflict_isolation') or {}
    incomplete = {c['contributor_id'] for c in isolation.get('contributors_with_gaps', []) if c.get('contributor_id')}
    blocked.update(nid for nid, member in members.items() if member.unit in incomplete)
    for issue in (*isolation.get('details', []), *graph.get('context_cycle_diagnostics', []),
                  *graph.get('unresolved_relations', [])):
        blocked.update(issue.get('node_ids') or [])
        blocked.update(issue.get(k) for k in ('node_id', 'source', 'target'))
        for choices in (issue.get('candidate_endpoints') or {}).values():
            blocked.update(choices)
    # A single key must identify exactly the same set of occurrences at every
    # observed level. Never take a transitive closure across overlapping sets.
    sets = {frozenset(ids) for ids in physical_groups.values() if len(ids) > 1
            and len({members[i].unit for i in ids}) > 1}
    stats['cross_unit_physical_sets'] = len(sets)
    physical_sets.extend(sets)
    rejected_sets, observation_differences = Counter(), Counter()
    partial_sets = retained_members = 0
    equal_node_pairs = Counter()
    neighborhood_differences = Counter()
    row_context_differences = Counter()
    role_index = None
    for ids in sets:
        first = members[next(iter(ids))]
        labels = {label for i in ids for _, _, label, _, _ in members[i].observations}
        if len(labels) > 1:
            reason = 'multiple_labels'
        elif any(physical_groups[first.address, k, first.value] != ids for i in ids for k in members[i].bindings):
            reason = 'overlapping_projection_sets'
        elif len({nodes[i]['type'] for i in ids}) != len(ids):
            reason = 'competing_instances_in_one_type'
        else:
            # Within a complete physical bucket, only identical independently
            # witnessed signatures share an ID. An unrecognized fifth draft
            # cannot veto the first four or act as a bridge between signatures.
            signatures = defaultdict(set)
            for i in ids:
                signatures[members[i].observations].add(i)
            if len(signatures) > 1:
                observation_differences.update(_observation_differences(signatures))
            matched = 0
            inconsistent_path = False
            matching_batches = 0
            for peers in signatures.values():
                remaining = _cross_unit_pairs(peers, members)
                if not remaining:
                    continue
                # For pairs whose own observations already agree, report the
                # first actual exclusion separately from whole-set differences.
                for reason, excluded in (('incomplete', blocked), ('endpoint_or_role', invalid_roles),
                                         ('direction', missing_orientation)):
                    if peers & excluded:
                        peers = peers - excluded
                        count = _cross_unit_pairs(peers, members)
                        equal_node_pairs[reason] += remaining - count
                        remaining = count
                connected = {i for i in peers if incidents[i]}
                isolated = peers - connected
                count = _cross_unit_pairs(connected, members)
                isolated_pairs = _cross_unit_pairs(isolated, members)
                equal_node_pairs['relationship_on_one_side_only'] += remaining - count - isolated_pairs
                matches = defaultdict(set)
                for i in connected:
                    matches[frozenset(incidents[i])].add(i)
                batches = [(batch, 'direct_neighbors') for batch in matches.values()]
                if isolated_pairs:
                    if role_index is None:
                        role_index = IsolatedRoleIndex(nodes, members, layouts, incidents,
                                                       blocked | invalid_roles | missing_orientation,
                                                       original_boundaries)
                    batches.extend((batch, 'isolated_member_role') for batch in role_index.batches(isolated))
                for batch, basis in batches:
                    joint = _cross_unit_pairs(batch, members)
                    if not joint:
                        continue
                    if basis == 'direct_neighbors':
                        count -= joint
                    else:
                        isolated_pairs -= joint
                    if not _ancestor_paths_agree(batch, nodes, layouts, edge_rows, members=members, captions=captions):
                        inconsistent_path = True
                        equal_node_pairs['ancestor_path'] += joint
                        continue
                    matched += len(batch)
                    matching_batches += 1
                    equal_node_pairs['passed_before_endpoint_remap'] += joint
                    yield batch, basis
                equal_node_pairs['different_neighborhood'] += count
                equal_node_pairs['no_witnessed_relationship'] += isolated_pairs
                if count:
                    neighborhood_differences.update(neighborhood_difference_counts(
                        matches, members, row_context_differences=row_context_differences))
            if matched:
                if matching_batches > 1 or matched < len(ids):
                    partial_sets += 1
                    retained_members += len(ids) - matched
                continue
            if inconsistent_path:
                reason = 'different_observed_ancestor_paths'
            elif ids & blocked:
                reason = 'unresolved_or_incomplete_contribution'
            elif ids & invalid_roles:
                reason = 'incompatible_endpoint_or_role'
            elif ids & missing_orientation:
                reason = 'undeclared_or_conflicting_direction'
            elif len(signatures) > 1:
                reason = 'different_node_observations_or_scope'
            elif any(not incidents[i] for i in ids):
                reason = 'no_witnessed_nonself_relationship'
            else:
                reason = 'different_physical_neighbors_or_edge_scope'
        rejected_sets[reason] += 1
    stats['partitioned_physical_sets'] = partial_sets
    stats['unmatched_nodes_in_partitioned_sets'] = retained_members
    stats['rejected_physical_sets'] = dict(sorted(rejected_sets.items()))
    stats['sets_by_observation_difference'] = dict(sorted(observation_differences.items()))
    stats['equal_node_pairs_by_result'] = {key: n for key, n in sorted(equal_node_pairs.items()) if n}
    stats['different_neighborhood_pairs_by_first_difference'] = dict(sorted(neighborhood_differences.items()))
    stats['edge_row_context_pairs_by_first_difference'] = dict(sorted(row_context_differences.items()))
    if role_index is not None:
        stats['isolated_members_by_role_result'] = role_index.diagnostics


def _compatible_remaps(graph, batches, nodes):
    """Retain groups whose edges keep distinct identities and do not introduce new self-links."""
    owners = {nid: winner for winner, ids in batches.items() for nid in ids}
    if not owners:
        return owners
    contracts = {d['type']: (d.get('source_type'), d.get('target_type')) for d in graph['definitions']['relations']}
    # A malformed historical relation cannot be safely specialized. Unrelated
    # relations and members remain eligible for their own verified folds.
    malformed = {e['type'] for e in graph['edges']
                 if contracts.get(e['type']) != (nodes[e['source']]['type'], nodes[e['target']]['type'])}
    while owners:
        ids_by_edge = defaultdict(list)
        rejected = set()
        for edge in graph['edges']:
            old = edge['source'], edge['target']
            new = tuple(owners.get(n, n) for n in old)
            changed = {owners[n] for n in old if n in owners}
            if edge['type'] in malformed:
                rejected.update(changed)
            if new != old and new[0] == new[1] and old[0] != old[1]:
                rejected.update(changed)
            key = relation_identity({**edge, 'source': new[0], 'target': new[1]})
            ids_by_edge[key].append(changed)
            if new == old and edge['id'] != key:
                # Unchanged historical edges retain their published ID.
                ids_by_edge[edge['id']].append(changed)
        for colliding in ids_by_edge.values():
            if len(colliding) > 1:
                rejected.update(set.union(*colliding))
        if not rejected:
            break
        owners = {n: target for n, target in owners.items() if target not in rejected}
    return owners


def _update_endpoint_contracts(graph, nodes, touched):
    """Give each relation a single, accurate pair of endpoint types after a partial fold.

    Keep the published name on the original endpoint pair when it remains in
    use. Specializations retain the original relation family and direction.
    """
    definitions = {d['type']: d for d in graph['definitions']['relations']}
    occupied_ids = {edge['id'] for edge in graph['edges']}
    pending = {r['type'] for r in graph.get('unresolved_relations', []) if 'type' in r}
    partitions = defaultdict(lambda: defaultdict(list))
    for edge in graph['edges']:
        if edge['type'] in touched:
            pair = nodes[edge['source']]['type'], nodes[edge['target']]['type']
            partitions[edge['type']][pair].append(edge)

    for name, by_pair in sorted(partitions.items()):
        relation = definitions[name]
        template = deepcopy(relation)
        initial_pair = (template['source_type'], template['target_type'])
        if name in pending:
            by_pair.setdefault(initial_pair, [])
        primary = initial_pair if initial_pair in by_pair else min(by_pair, key=lambda p: (-len(by_pair[p]), p))
        family = template.get('original_type', name)
        previous_variant = template.get('definition_variant') or {}
        projection = previous_variant.get('endpoint_projection') or digest(definition_contract(template))
        for pair, edges in sorted(by_pair.items()):
            current = relation if pair == primary else deepcopy(template)
            current['source_type'], current['target_type'] = pair
            if len(by_pair) > 1 or pair != initial_pair:
                current.update(original_type=family, definition_variant={'version': 1, 'endpoint_projection': projection})
                signature = digest(definition_contract(current))
                current['definition_variant']['contract_digest'] = signature
                if pair != primary:
                    target = f'{family[:60]}__def_{signature[:32]}'
                    counter = 0
                    while target in definitions or any(
                        relation_identity({**edge, 'type': target}) in occupied_ids for edge in edges
                    ):
                        target = '__definition_' + digest('hierarchy_endpoints_v1', name, signature, counter)
                        counter += 1
                    current['type'] = target
                    definitions[target] = current
            for edge in edges:
                if current.get('definition_variant'):
                    edge.update(original_type=family, definition_variant=deepcopy(current['definition_variant']))
                if current['type'] != name:
                    edge['type'] = current['type']
                    edge['id'] = relation_identity(edge)
                    occupied_ids.add(edge['id'])
    graph['definitions']['relations'] = [definitions[name] for name in sorted(definitions)]


def _merge_members(winner, batch, definitions):
    for node in batch:
        original = {k: deepcopy(node[k]) for k in ('id', 'type', 'identity', 'identity_types', 'identity_domain',
                    'original_type', 'definition_variant') if k in node}
        for support in node['supports']:
            origin = {**original, 'contributor_id': support['contributor_id']}
            origins = support.setdefault('identity_origins', [])
            if origin not in origins:
                origins.append(origin)
            support['identity_basis'] = 'observed_hierarchy_projection_v1'
            # A composite key may use different logical names in each projection.
            keys = support['provenance']['key_columns']
            names = definitions[node['type']]['key_names']
            support['original_key_values'] = dict(zip(keys, (node['identity'][k] for k in names), strict=True))
    for node in batch:
        if node is winner:
            continue
        winner['supports'].extend(node['supports'])
        for field in ('aliases', 'descriptions', 'english_names', 'provenance'):
            if node.get(field):
                previous = winner.setdefault(field, [])
                seen = {canonical(v) for v in previous}
                for value in node[field]:
                    token = canonical(value)
                    if token not in seen:
                        seen.add(token)
                        previous.append(value)
        for key, values in node.get('properties', {}).items():
            dest = winner.setdefault('properties', {}).setdefault(key, [])
            seen = {canonical(v) for v in dest}
            for value in values:
                if canonical(value) not in seen:
                    dest.append(value)
                    seen.add(canonical(value))


def reconcile_hierarchy_projections(graph, *, excluded_types=(), diagnostics=None):
    """Collapse verified source replays in a fresh unit merge; return removed node count."""
    nodes = {n['id']: n for n in graph['nodes']}
    stats = {} if diagnostics is None else diagnostics
    physical_sets = []
    batches, bases = {}, {}
    for ids, basis in _verified_groups(graph, excluded_types, stats, physical_sets):
        winner = min(ids, key=lambda i: (nodes[i]['type'], i))
        batches[winner] = ids
        bases[winner] = basis
    stats['groups_with_matching_roles_and_scope'] = len(batches)
    owners = _compatible_remaps(graph, batches, nodes)
    remap = {nid: winner for nid, winner in owners.items() if nid != winner}
    outcomes = Counter((len(ids), len({owners.get(i, i) for i in ids})) for ids in physical_sets)
    stats['physical_set_outcomes'] = [{'input_nodes': before, 'output_nodes': after, 'sets': count}
                                     for (before, after), count in sorted(outcomes.items())]
    stats['accepted_groups'] = len(set(remap.values()))
    stats['collapsed_nodes_by_role_evidence'] = dict(sorted(Counter(bases[w] for w in remap.values()).items()))
    stats['groups_rejected_during_endpoint_remap'] = len(batches) - stats['accepted_groups']
    if not remap:
        return 0
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    for winner in sorted(set(remap.values())):
        batch = [nodes[i] for i in sorted(batches[winner])]
        _merge_members(nodes[winner], batch, definitions)
    touched = {e['type'] for e in graph['edges'] if e['source'] in remap or e['target'] in remap}
    remap_graph_endpoints(graph, remap, self_contained=True, record_fusion=True)
    graph['nodes'] = [n for n in graph['nodes'] if n['id'] not in remap]
    _update_endpoint_contracts(graph, nodes, touched)
    graph['edges'].sort(key=lambda e: e['id'])
    retired_types = {nodes[i]['type'] for i in remap}
    used_types = {n['type'] for n in graph['nodes']}
    used_types.update(rel.get(end) for rel in graph['definitions']['relations'] for end in ('source_type', 'target_type'))
    graph['definitions']['entities'] = [d for d in graph['definitions']['entities']
                                        if d['type'] not in retired_types or d['type'] in used_types]
    # Suggestions are not identity edges. Remap residual suggestions and
    # discard those that now describe a single visible object.
    candidates = []
    for group in graph.get('candidate_identity_merges', []):
        ids = sorted({remap.get(n, n) for n in group['node_ids']})
        if group.get('match_basis') == 'same_typed_identity' and any(n in remap for n in group['node_ids']):
            ids = [i for i in ids if nodes[i]['type'] == group.get('entity_type')
                   and nodes[i]['identity'] == group.get('identity')]
        if len(ids) > 1:
            group['node_ids'] = ids
            if 'entity_types' in group:
                group['entity_types'] = sorted({nodes[i]['type'] for i in ids})
            candidates.append(group)
    graph['candidate_identity_merges'] = candidates
    return len(remap)

"""Discover finite, unapproved hypotheses from frozen source projections; never publish a rule."""
from collections import Counter, defaultdict
import json

from lightgraph.contracts import canonical, digest
from lightgraph.runtime.config import ReviewedKeyProjection
from lightgraph.source_values import scalar_kind
from prepare_reviewed_key_pairs import _project


def _unit_slots(contributor, graph_digest):
    """Retain candidates lacking a name or full evidence beside eligible siblings on the same physical key."""
    graph = contributor['graph']
    buckets = defaultdict(list)
    name_columns = defaultdict(set)
    for node in graph['nodes']:
        identity = node.get('identity') or {}
        if len(identity) != 1:
            continue
        field, = identity
        value = identity[field]
        family = node.get('original_type') or node['type']
        code = canonical([node.get('identity_types', {}).get(field) or scalar_kind(value), value])
        seen = set()
        for support in node.get('supports') or ():
            prov = support.get('provenance') or {}
            s = prov.get('source') or {}
            address = tuple(s.get(k) for k in ('source_id', 'schema', 'table'))
            cols = prov.get('key_columns')
            if not all(address) or not isinstance(cols, list) or len(cols) != 1 or not cols[0]:
                continue
            slot = (family, *address, cols[0])
            if slot not in seen:
                seen.add(slot)
                buckets[slot].append((code, node))
            if prov.get('label_column') and prov['label_column'] != cols[0]:
                name_columns[slot].add(prov['label_column'])
    for slot, records in sorted(buckets.items()):
        family, source, schema, table, key = slot
        unit = contributor['contributor_id']
        subset = {'contributor_id': unit, 'graph': {'definitions': graph['definitions'],
                                                    'nodes': [n for _, n in records]}}
        matches = defaultdict(list)
        for name in sorted(name_columns[slot]):
            b = ReviewedKeyProjection(contributor_id=unit, graph_digest=graph_digest, source_id=source,
                                      schema=schema, table=table, key_column=key, name_column=name)
            projection = b.model_dump(mode='json', by_alias=True)
            for proof, row in _project(subset, family, b, include_node_ids=True).items():
                matches[row['node_id']].append((proof, projection))
        yield slot, [(code, node['id'], matches[node['id']]) for code, node in records], len(name_columns[slot])


def _endpoint_roles(graph):
    """For a missing explicit node role use exact non-self incident semantic family and directed column roles."""
    roles, missing = defaultdict(set), set()
    definitions = {r['type']: r for r in (graph.get('definitions') or {}).get('relations') or []}
    for e in graph['edges']:
        if e['source'] == e['target']:
            continue
        typ = e.get('original_type') or definitions.get(e['type'], {}).get('original_type') or e['type']
        if not e.get('supports'):
            missing.update((e['source'], e['target']))
        for support in e.get('supports') or ():
            p = support.get('provenance') or {}
            cols = [p.get(k) for k in ('source_key_columns', 'target_key_columns')]
            source = p.get('source') or {}
            address = tuple(source.get(k) for k in ('source_id', 'schema', 'table'))
            if not all(address) or not all(isinstance(v, list) and v and all(isinstance(c, str) and c for c in v) for v in cols):
                missing.update((e['source'], e['target']))
                continue
            for end, own_cols in zip(('source', 'target'), cols):
                roles[e[end]].add((address, tuple(own_cols), canonical([typ, end, cols])))
    return {k: v for k, v in roles.items() if k not in missing}


def _bound_role(profile, address, col):
    if not profile or any(a != address or own != (col,) for a, own, _ in profile):
        return None
    return sorted({sig for _, _, sig in profile})


def _binding_claim(family, projection, token):
    return (family, *(projection[k] for k in ('contributor_id', 'source_id', 'schema', 'table', 'key_column')), token)


def discover(inputs, parents, run, target, reference_rules, excluded_values):
    """Check the entire exact typed code block at once. A pairwise chain cannot create an n-way match."""
    excluded = {canonical(v) for v in excluded_values}
    digests = {r['node_id']: r['graph_digest'] for r in parents}
    pinned = {_binding_claim(rule['entity_type'], p, token) for rule in reference_rules
              if all(digests.get(side['contributor_id']) == side['graph_digest'] for side in rule['projections'])
              for p in rule['projections'] for token in rule['observation_digests']}
    blocks, roles, reserved_ids = defaultdict(list), {}, set()
    projection_count = 0
    coverage = []
    for item in inputs:
        unit = item['contributor_id']
        roles[unit] = _endpoint_roles(item['graph'])
        with_slot, qualified = set(), set()
        for (family, source, schema, table, col), records, count in _unit_slots(item, digests[unit]):
            projection_count += count
            for code, nid, options in records:
                with_slot.add(nid)
                if len(options) == 1:
                    qualified.add(nid)
                blocks[family, col, code].append((unit, nid, (source, schema, table), options))
                if any(_binding_claim(family, p, proof[0]) in pinned for proof, p in options):
                    reserved_ids.add((unit, nid))
        total = len(item['graph']['nodes'])
        coverage.append({'contributor_id': unit, 'input_nodes': total, 'with_single_key_binding': len(with_slot),
                         'without_single_key_binding': total - len(with_slot),
                         'with_qualified_observation': len(qualified), 'bound_without_qualified_observation': len(with_slot - qualified)})

    candidates = []
    rejection_counts = Counter()
    nway_rejections = []
    nway_total = 0
    eligible_by_id = Counter()

    def reject(reason, family, col, code, rows, nway):
        rejection_counts[reason] += 1
        if nway:
            nway_rejections.append({'reason': reason, 'entity_type': family, 'key_column': col,
                                    'typed_key': json.loads(code), 'members': [
                                        {'contributor_id': u, 'node_id': n, 'table_address': list(a)}
                                        for u, n, a, _ in sorted(rows, key=lambda x: (x[0], x[1], x[2]))]})

    for (family, col, code), rows in sorted(blocks.items()):
        owners = {u for u, _, _, _ in rows}
        if len(owners) < 2:
            continue
        nway = len(owners) >= 3
        nway_total += int(nway)
        members = [(u, n) for u, n, _, _ in rows]
        complete = all(len(options) == 1 for _, _, _, options in rows)
        if canonical(json.loads(code)[1]) in excluded:
            reason = 'excluded_key'
        elif any(member in reserved_ids for member in members):
            reason = 'reserved_by_reference'
        elif len(rows) != len(owners):
            reason = 'competing_member_or_projection_in_unit'
        elif not complete:
            reason = 'missing_or_ambiguous_corroborated_observation'
        elif len({options[0][0] for _, _, _, options in rows}) != 1:
            reason = 'different_name_audience_or_role'
        elif nway and len({address for _, _, address, _ in rows}) == 1:
            reason = 'same_table_multiway_covered_by_physical_lane'
        else:
            reason = None
        if not reason and nway and not rows[0][3][0][0][2]:
            profiles = [_bound_role(roles[u].get(n), a, col) for u, n, a, _ in rows]
            if not profiles[0] or any(p != profiles[0] for p in profiles):
                reason = 'no_shared_explicit_or_witnessed_role'
        if reason:
            reject(reason, family, col, code, rows, nway)
            continue
        eligible_by_id.update(members)
        candidates.append((family, col, code, rows, nway))

    patterns, nway_groups = {}, []
    for family, col, code, rows, nway in candidates:
        if any(eligible_by_id[u, n] != 1 for u, n, _, _ in rows):
            reject('claimed_by_multiple_hypotheses', family, col, code, rows, nway)
            continue
        rows = sorted(rows, key=lambda x: (x[0], x[1]))
        proof = rows[0][3][0][0]
        projections = [options[0][1] for _, _, _, options in rows]
        if nway:
            details = [{'contributor_id': u, 'node_id': n, 'projection': p}
                       for (u, n, _, _), p in zip(rows, projections)]
            nway_groups.append({'group_id': digest('multiway_hypothesis_v1', family, proof, details),
                                'entity_type': family, 'typed_key': json.loads(code), 'observation_digest': proof[0],
                                'audience': list(proof[1]), 'role_basis': 'node_role' if proof[2] else 'incident_edge_roles',
                                'role_evidence': proof[2] or _bound_role(roles[rows[0][0]].get(rows[0][1]), rows[0][2], col),
                                'members': details})
            continue
        pid = digest('census_projection_pair_v1', family, *projections)
        group = patterns.setdefault(pid, {'entity_type': family, 'projections': projections, 'tokens': []})
        group['tokens'].append(proof[0])

    bundles = []
    for pid, pattern in sorted(patterns.items()):
        tokens = sorted(pattern.pop('tokens'))
        pattern.update(pattern_id=pid, simulation_hypotheses=len(tokens))
        for start in range(0, len(tokens), 5000):
            selected = tokens[start:start + 5000]
            rule = {'entity_type': pattern['entity_type'], 'projections': pattern['projections'],
                    'observation_digests': selected, 'approval_ref': 'simulation_only'}
            bundles.append({'mode': 'simulation_only', 'version': 1, 'project_id': run['project_id'],
                            'run_id': run['run_id'], 'node_id': target['node_id'],
                            'merge_artifact_id': target['artifact_id'], 'upstream_artifacts': target['upstream_artifacts'],
                            'simulated_count': len(selected), 'simulated_key_pairs': [rule]})
    return {'source_node_coverage': coverage, 'projections_examined': projection_count, 'patterns': list(patterns.values()), 'rule_files': len(bundles),
            'excluded_or_unverified_blocks_by_reason': dict(rejection_counts),
            'multiway': {'mode': 'cross_table_multiway_hypothesis_only', 'blocks_with_three_or_more_units': nway_total,
                         'group_count': len(nway_groups), 'qualified_input_reduction': sum(len(g['members']) - 1 for g in nway_groups),
                         'groups': nway_groups, 'rejected_blocks': len(nway_rejections),
                         'rejection_counts': dict(Counter(g['reason'] for g in nway_rejections)), 'rejections': nway_rejections},
            'hypothesized_pairs': sum(b['simulated_count'] for b in bundles)}, bundles


def summarize_multiway_outputs(multiway, input_node_mapping):
    """Count disjoint output images, rejecting unselected co-owners of an existing merged entity."""
    origins = defaultdict(set)
    for unit, mapping in input_node_mapping.items():
        for nid, output in mapping.items():
            if output is not None:
                origins[output].add((unit, nid))
    accepted, rejected, claims = [], [], Counter()
    for group in multiway['groups']:
        members = {(r['contributor_id'], r['node_id']) for r in group['members']}
        ids = {input_node_mapping.get(u, {}).get(n) for u, n in members}
        if None in ids:
            reason = 'unverified_input_to_output'
        elif any(origins[o] - members for o in ids):
            reason = 'output_contains_other_input_members'
        else:
            reason = None
        row = {'group_id': group['group_id'], 'output_ids': sorted(i for i in ids if i is not None)}
        if reason:
            rejected.append({**row, 'reason': reason})
        else:
            accepted.append(row)
            claims.update(ids)
    groups = []
    for row in accepted:
        if any(claims[out] > 1 for out in row['output_ids']):
            rejected.append({**row, 'reason': 'shared_output_across_hypotheses'})
        else:
            groups.append(row)
    return {'mode': 'counterfactual_only', 'hypothetical_node_reduction': sum(len(r['output_ids']) - 1 for r in groups),
            'group_count': len(groups), 'already_one_output': sum(len(r['output_ids']) == 1 for r in groups),
            'groups': groups, 'excluded_groups': rejected,
            'exclusion_counts': dict(Counter(r['reason'] for r in rejected))}

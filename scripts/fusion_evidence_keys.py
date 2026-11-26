"""Observe actual checkpoint keys at the projections named by an archived proposal."""
from collections import Counter, defaultdict

from lightgraph.construction import projection_proposals as projections
from lightgraph.construction.contexts import business_context_identity
from lightgraph.construction.observed_identity import contradictory_qualifiers, identity_qualifier
from lightgraph.contracts import digest
from lightgraph.runtime.config import ProjectionBinding


def _binding(row, data):
    return ProjectionBinding(
        contributor_id=row['contributor_id'], graph_digest=data.digest(row['contributor_id']),
        entity_type=row['entity_type'], source_id=row['source_id'], schema_name=row['schema'],
        table=row['table'], key_columns=row['key_columns'], relation_type=row['relation_type'],
        end=row['end'], opposite_key_columns=row['opposite_key_columns'],
    )


def _equal(values):
    """None means at least one required observation was unavailable."""
    return None if any(value is None for value in values) else all(v == values[0] for v in values)


def _selected_key(ref, bindings, data):
    owner, node_id = ref
    if owner not in data.inputs:
        return None, None, 'missing_contributor'
    nodes, _, _, entities, _ = data.index(owner)
    node = nodes.get(node_id)
    if node is None:
        return None, None, 'missing_input_node'
    selected = []
    for slot, binding in enumerate(bindings):
        if (owner, node['type']) != (binding.contributor_id, binding.entity_type):
            continue
        source, schema, table, cols = projections._address(binding)
        for support in node.get('supports', ()):
            prov = support.get('provenance') or {}
            if (projections.source_address(prov.get('source')) == (source, schema, table)
                    and tuple(prov.get('key_columns') or ()) == cols):
                selected.append(slot)
                break
    if len(selected) != 1:
        return None, None, 'node_not_in_selected_binding' if not selected else 'ambiguous_selected_binding'
    key = projections._cells(node, entities.get(node['type'], {}).get('key_names'))
    return selected[0], key, None if key is not None else 'incomplete_typed_identity'


def _compare_observations(members, bindings, data, physical, grant):
    # The production observer may return early. Distinguish unevaluated fields
    # from disagreements; read independent scope/qualifier/role evidence directly.
    by_member, roles, registry_roles, section_sets, registry_sets = [], [], [], [], []
    for m, binding in zip(members, bindings, strict=True):
        idx = data.index(binding.contributor_id)
        declared = idx[3][binding.entity_type]
        status = sorted({business_context_identity(s)['status'] for s in m.node.get('supports', [])})
        sections = m.sections
        registries = m.registries
        if m.reason == 'missing_or_conflicting_names' and declared.get('identity_scope') == 'global':
            claims = projections._global_authorities(m.node, declared, {}, local_namespace=False)
            sections = {token for kind, token in claims if kind == 'document'}
            registries = {token for kind, token in claims if kind == 'registry'}
        section_sets.append(sections)
        registry_sets.append(registries)
        role = m.roles or projections._witnesses(m.node, binding, idx, compare_shared_identity=bool(sections or registries))
        roles.append(role[0 if physical else 1] if role else None)
        registry_roles.append(role.shared_identity if role else None)
        qualifier_roles = set()
        for support in m.node.get('supports', ()):
            provenance = support.get('provenance') or {}
            properties = support.get('observed_properties', support.get('properties')) or {}
            qualifier_roles.update(name for prop in properties if (name := identity_qualifier(provenance, prop)))
        by_member.append({'contributor_id': binding.contributor_id, 'entity_type': binding.entity_type,
                          'observation_reason': m.reason, 'input_node_id': m.node['id'],
                          'identity_scope': declared.get('identity_scope'),
                          'identity_context_keys': list(declared.get('identity_context_keys') or ()),
                          'business_context_statuses': status, 'bound_key_sections': len(sections),
                          'observed_identity_qualifier_roles': sorted(qualifier_roles),
                          'selected_role_witnessed': bool(role), 'comparable_role_witness': bool(roles[-1])})
    names = [m.label for m in members]
    shared_sections = sorted(set.intersection(*section_sets))
    shared_registries = sorted(set.intersection(*registry_sets))
    boundary_match = _equal([m.boundaries for m in members])
    all_scopes_known = all(entry['business_context_statuses']
                           and 'unknown' not in entry['business_context_statuses'] for entry in by_member)
    grants_cover = None
    if grant is not None and all(m.ancestors is not None for m in members):
        grants_cover = all(projections._grant_covers(m, b, data, grant) for m, b in zip(members, bindings, strict=True))
    return {
        'member_observations': by_member,
        'names_recorded_for_all': all(names), 'observed_names_equal': _equal(names),
        'shared_bound_section_ids': shared_sections,
        **({'shared_versioned_registry_ids': shared_registries} if shared_registries else {}),
        'all_support_applicability_known': all_scopes_known,
        'contracts_equal': _equal([m.contract for m in members]),
        'applicability_and_row_boundaries_equal': boundary_match,
        'relation_and_neighbor_witnesses_equal': _equal(roles),
        'shared_identity_relation_witnesses_equal': _equal(registry_roles) if shared_sections or shared_registries else None,
        'ancestor_witnesses_equal': _equal([m.ancestors for m in members]),
        'conflicting_identity_qualifier_values_observed': contradictory_qualifiers(m.node for m in members),
        'table_grant_covers_all_members': grants_cover,
        'first_observation_result': projections._observation_mismatch(members, physical) or 'observations_agree',
    }


def _inspect(refs, bindings, tables, data, physical, grant):
    selected = [_selected_key(ref, bindings, data) for ref in refs]
    report = {'requested_binding_slots': [slot for slot, _, _ in selected],
              'requested_member_issues': [reason for _, _, reason in selected]}
    if any(reason for _, _, reason in selected):
        return {**report, 'status': 'requested_member_unverified'}
    (left, key, _), (right, other, _) = selected
    if left == right:
        return {**report, 'status': 'same_selected_binding'}
    if key != other:
        return {**report, 'status': 'different_complete_typed_keys'}
    entries = [None if table is None else table.get(key, ()) for table in tables]
    counts = [None if peers is None else len(peers) for peers in entries]
    report.update(typed_key_sha256=digest('fusion_evidence_key_v1', key), key_types=[kind for kind, _ in key],
                  selected_binding_match_counts=counts)
    if None in counts:
        return {**report, 'status': 'selected_binding_has_incomplete_typed_identity'}
    if 0 in counts:
        return {**report, 'status': 'key_absent_from_selected_binding'}
    if any(count != 1 for count in counts):
        return {**report, 'status': 'nonunique_member_in_projection'}
    members = [peers[0] for peers in entries]
    if any(members[slot].node['id'] != ref[1] for (slot, _, _), ref in zip(selected, refs, strict=True)):
        return {**report, 'status': 'input_node_not_indexed_under_selected_key'}
    return {**report, 'status': 'observed_all_selected_members',
            **_compare_observations(members, bindings, data, physical, grant)}


def observed_pair_checks(inputs, routes, requests):
    """For exact two-binding hits, inspect all selected bindings against frozen inputs.

    Requests are (pair_index, pattern_id, (left_owner, input_id), (right_owner, input_id)).
    This reconstructs observations, without applying rules or attributing decisions to keys.
    """
    data = projections._Inputs(inputs)
    by_pid = {r['pattern_id']: r for r in routes}
    requests_by_pid = defaultdict(list)
    for index, pid, left, right in requests:
        requests_by_pid[pid].append((index, (left, right)))
    plans, reuse = {}, Counter()
    for pid in requests_by_pid:
        route = by_pid[pid]
        if route['action'] != 'propose' or len(route['selected_bindings']) < 2:
            raise ValueError('逐键检查须使用归档提议中实际选中的两个绑定。')
        bindings = [_binding(row, data) for row in route['selected_bindings']]
        signatures = [binding.model_dump_json() for binding in bindings]
        reuse.update(signatures)
        plans[pid] = bindings, signatures
    cache, result = {}, {}
    for pid, pattern_requests in requests_by_pid.items():
        bindings, signatures = plans[pid]
        for signature, binding in zip(signatures, bindings, strict=True):
            if signature not in cache:
                cache[signature] = projections._by_key(binding, data)
        tables = [cache[signature] for signature in signatures]
        axes = {projections._address(b) for b in bindings}
        owners = sorted({b.contributor_id for b in bindings})
        fingerprints = [data.inputs[o]['graph'].get('access_fingerprint') for o in owners]
        equal_access = bool(all(fingerprints) and len(set(fingerprints)) == 1)
        physical = len(axes) == 1
        grant = (projections.common_table_grant([data.inputs[o] for o in owners], bindings[0])
                 if physical and not equal_access else None)
        common = {'basis': 'reconstructed_selected_binding_observations',
                  'same_physical_projection': physical, 'all_unit_fingerprints_present': all(fingerprints),
                  'unit_fingerprints_match': equal_access, 'common_table_grant_available': grant is not None,
                  'table_grant_checked': physical and not equal_access}
        for index, refs in pattern_requests:
            if (index, pid) in result:
                raise ValueError('逐键检查请求含有重复的成员对与模式编号。')
            result[index, pid] = {**common, **_inspect(refs, bindings, tables, data, physical, grant)}
        for signature in signatures:
            reuse[signature] -= 1
            if not reuse[signature]:
                del cache[signature]
    return result

"""Model selections are validated with the original rows; replay never consults the model."""
from copy import deepcopy

import pytest

from lightgraph.contracts import digest
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.runtime.config import MergePolicyConfig, WorkflowConfig
from lightgraph.tests.test_batch_overlap_patterns import unit


def _compile(inputs, mode='same_physical_key'):
    analysis = analyze_overlap_patterns(inputs)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity' and p['basis'] == mode]
    alignment = []
    for idx, member in enumerate(card['members']):
        slot = next(i for i, r in enumerate(member['roles']) if r['end'] == 'source')
        alignment.append({'member_index': idx, 'source_index': member['source_indexes'][0], 'role_index': slot})
    decision = {'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': alignment}
    policy, (report,) = verify_projection_proposals(MergePolicyConfig(), inputs, [card], [decision])
    return policy, report


def _batch():
    return [unit('factory', 'DeliveryRegion', 'zone', field='capacity'),
            unit('service', 'ReportingArea', 'area', field='language')]


def test_model_selects_one_wide_table_axis_partial_coverage_and_replay():
    data = _batch()
    # These are ordinary wide-table columns. There is no catalog, PK/FK, or shared domain declaration.
    for c in data:
        c['graph']['definitions']['entities'][0].pop('identity_domain')
        for node in c['graph']['nodes']:
            node.pop('identity_domain')
    base = merge_construction_graphs(data)
    policy, report = _compile(data)
    assert (report['overlapping_keys'], report['matched_key_names'], report['verified_groups']) == (2, 2, 1)
    assert report['verified_groups_by_basis'] == {'same_physical_projection': 1}
    assert report['rejected_keys_by_reason'] == {'unverified_relation_role': 1}
    assert len(base['nodes']) == 4
    with pytest.raises(ValueError, match='projection_proposals_require_merge_review'):
        WorkflowConfig(merge_policy=policy)
    effective = MergePolicyConfig.model_validate_json(policy.model_dump_json())
    result = merge_construction_graphs(data, merge_policy=effective)
    assert result == merge_construction_graphs(list(reversed(data)), merge_policy=effective)
    assert len(result['nodes']) == 3 and len(result['edges']) == len(base['edges']) == 2
    node, = [n for n in result['nodes'] if len(n['supports']) == 2]
    assert set(node['properties']) == {'capacity', 'language'}
    assert all(s['identity_basis'] == 'verified_projection_alignment_v1' and
               s['overlap_pattern_id'] == report['pattern_id'] and 'identity_approval_ref' not in s
               for s in node['supports'])
    assert {s['reviewed_input_node_id'] for s in node['supports']} == {'factory_0', 'service_0'}
    assert all(e['source'] == node['id'] and e['target'] != node['id'] and
               all(report['pattern_id'] in s['identity_decision_endpoints']['source'] for s in e['supports'])
               for e in result['edges'])
    assert result['fusion_diagnostics']['model_verified_projections']['applied_by_pattern'] == {
        report['pattern_id']: {'applied_groups': 1, 'collapsed_nodes': 1}}
    data[0]['graph']['nodes'][0]['label'] += ' edit after review'
    stale = merge_construction_graphs(data, merge_policy=effective)
    assert stale['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 0


def _bind_section(contributor, revision):
    definition, = contributor['graph']['definitions']['entities']
    for node in contributor['graph']['nodes']:
        for support in node['supports']:
            p = support['provenance']
            address = tuple(p['source'][k] for k in ('source_id', 'schema', 'table'))
            p['global_identity_authority'] = {
                'binding': digest(definition['identity_domain'], address, p['key_columns']),
                'key_sections_v1': [digest('identity_field_section_v1', 'business-terms', revision, 'zones', 'content')]}


@pytest.mark.parametrize(('revisions', 'expected'), [((), 0), (('v1', 'v2'), 0), (('v1', 'v1'), 1)])
def test_different_wide_tables_require_independent_bound_key_evidence(revisions, expected):
    data = [unit('a', 'ActualArea', 'geo_code', table='actuals'),
            unit('b', 'TargetRegion', 'geo_code', table='targets')]
    for contributor in data:
        contributor['graph']['edges'][0]['supports'][0]['business_context'] = {'status': 'global', 'dimensions': {}}
    for contributor, rev in zip(data, revisions):
        _bind_section(contributor, rev)
    policy, report = _compile(data, 'same_logical_keys_across_sources')
    assert report['matched_key_names'] == 2 and report['verified_groups'] == expected
    if expected:
        assert report['verified_groups_by_basis'] == {'shared_versioned_key_section': 1}
        assert merge_construction_graphs(data, merge_policy=policy)['fusion_diagnostics'][
            'model_verified_projections']['collapsed_nodes'] == 1
    else:
        assert report['reason'] == 'cross_table_identity_unverified' and not policy.verified_projection_rules


@pytest.mark.parametrize('case', ['mixed_name', 'competing_code', 'role', 'scope', 'access', 'missing_type',
                                   'property', 'domain', 'extra_role', 'stale_scope', 'condition', 'qualifier'])
def test_conflicts_and_unselected_roles_never_gain_identity(case):
    data = _batch()
    b = data[1]['graph']
    node = b['nodes'][0]
    if case == 'mixed_name':
        extra = deepcopy(node['supports'][0])
        extra['label'] += '13'
        node['supports'].append(extra)
    elif case == 'competing_code':
        duplicate = deepcopy(node)
        duplicate['id'] += '_other'
        b['nodes'].append(duplicate)
    elif case == 'role':
        b['edges'][0]['type'] = b['definitions']['relations'][0]['type'] = 'different_role'
    elif case == 'scope':
        node['supports'][0]['scope_ref'] = 'other-audience'
    elif case == 'access':
        b['access_fingerprint'] = 'different'
    elif case == 'missing_type':
        malformed = deepcopy(node)
        malformed['id'] += '_untyped'
        malformed['identity_types'].clear()
        b['nodes'].append(malformed)
    elif case == 'property':
        node['supports'][0]['provenance']['property_columns']['language'] = 'capacity_col'
    elif case == 'domain':
        b['definitions']['entities'][0]['identity_domain'] = node['identity_domain'] = 'another domain'
    elif case == 'extra_role':
        extra = deepcopy(b['edges'][0])
        extra.update(id='other', type='also_supplies')
        b['edges'].append(extra)
        b['definitions']['relations'].append({**b['definitions']['relations'][0], 'type': 'also_supplies'})
    elif case == 'condition':
        node['supports'][0]['row_context'] = {'values': {'office': 'ED'}, 'value_types': {'office': 'text'}}
        node['supports'][0]['provenance']['context_columns'] = ['office']
    elif case == 'qualifier':
        node['supports'][0]['provenance']['identity_qualifiers'] = {'language': 'caliber'}
    elif case == 'stale_scope':
        node['supports'][0].pop('business_context')
    policy, report = _compile(data)
    assert report['verified_groups'] == 0 and not policy.verified_projection_rules


def test_two_drafts_in_one_worker_and_three_sibling_projections_share_only_verified_members():
    # Every draft observes the same source rows through its own relation definition.
    # One key is in the selected source role. The target-role key remains independent.
    leaves = []
    for i in range(5):
        c = unit('unit' + str(i), 'Area' + str(i), 'logical' + str(i), field='attr' + str(i), role='mapped' + str(i))
        c['graph']['definitions']['relations'][0]['original_type'] = 'reports_to'
        leaves.append(c)
    first, second_draft, *siblings = leaves
    for field in ('nodes', 'edges'):
        first['graph'][field].extend(second_draft['graph'][field])
    for field in ('entities', 'relations'):
        first['graph']['definitions'][field].extend(second_draft['graph']['definitions'][field])
    inputs = [first, *siblings]
    policy, report = _compile(inputs)
    assert report['verified_groups'] == 1
    graph = merge_construction_graphs(inputs, merge_policy=policy)
    counts = graph['fusion_diagnostics']['model_verified_projections']
    assert counts['applied_groups'] == 1 and counts['collapsed_nodes'] == 4
    joined, = [n for n in graph['nodes'] if len(n['supports']) == 5]
    assert {s['reviewed_input_node_id'] for s in joined['supports']} == {'unit' + str(i) + '_0' for i in range(5)}
    assert len(graph['edges']) == 5 and all(e['source'] == joined['id'] for e in graph['edges'])


def test_competing_finite_rules_cannot_claim_the_same_member_or_hide_rejection():
    batch = _batch()
    policy, report = _compile(batch)
    first, = policy.verified_projection_rules
    second = first.model_copy(update={'pattern_id': 'pattern_' + 'a' * 24})
    competing = policy.model_copy(update={'verified_projection_rules': [first, second]})
    graph = merge_construction_graphs(batch, merge_policy=competing)
    stats = graph['fusion_diagnostics']['model_verified_projections']
    assert stats['collapsed_nodes'] == 0 and stats['not_applied_by_pattern'] == {
        report['pattern_id']: {'overlapping_proposals': 1}, second.pattern_id: {'overlapping_proposals': 1}}


@pytest.mark.parametrize('counterexample', ['none', 'columns', 'row_scope', 'credential', 'table', 'no_marker',
                                            'tampered_record', 'graph_claim_only', 'foreign_neighbor', 'scope_claim'])
def test_same_frozen_table_grants_survive_other_table_changes_and_replay(tmp_path, counterexample):
    from lightgraph.construction.access import access_fingerprint
    from lightgraph.construction.projection_proposals import attach_checkpoint_grant, attach_runtime_grants
    from lightgraph.construction.snapshot_registry import _context_path, register_access_context
    from lightgraph.construction.workflow import build_unit_config
    from lightgraph.construction.workspace import read_internal_yaml, write_yaml
    from lightgraph.runtime.config import RunConfig

    data = _batch()
    config = RunConfig.model_validate({
        'sources': [{'id': 'db', 'kind': 'gauss_database', 'dsn_env': 'DB', 'allow_fact_data': True,
                     'allowed_tables': ['dm.items', 'dm.different1', 'dm.different2']}],
        'isolation_domain': 'workspace1', 'workflow': {'units': [
            {'unit_id': item['contributor_id'], 'source_table_overrides': {'db': ['dm.items', f'dm.different{i + 1}']}}
            for i, item in enumerate(data)]}})
    records = {}
    persisted = []
    for i, (contributor, unit_config) in enumerate(zip(data, config.workflow.units, strict=True)):
        owner = contributor['contributor_id']
        output = tmp_path / owner
        scoped = build_unit_config(config, unit_config, unit_output_dir=output)
        if i == 1:
            if counterexample == 'columns':
                scoped.sources[0].allowed_columns = {'dm.items': ['code', 'display_name', 'parent_code']}
            elif counterexample == 'row_scope':
                from lightgraph.runtime.config import GraphScopeBinding
                scoped.scope_bindings = [GraphScopeBinding(source_id='db', schema='dm', table='items', columns=['team'])]
            elif counterexample == 'credential':
                scoped.sources[0].credential_revision = 'changed'
            elif counterexample == 'table':
                scoped.sources[0].allowed_tables = ['dm.different2']
        fp = access_fingerprint(scoped)
        contributor['graph']['access_fingerprint'] = fp
        for row in contributor['graph']['nodes'] + contributor['graph']['edges']:
            row['supports'][0]['provenance']['scope_columns'] = []
        policy_id = register_access_context(output / 'construction', scoped)
        records[owner] = {'artifact_status': 'committed', 'node_id': owner, 'contributor_kind': 'unit',
                          'graph_kind': 'node_delta', 'access_fingerprint': fp, 'result_policy_id': policy_id}
        if i == 1:
            if counterexample == 'tampered_record':
                path = _context_path(output / 'construction', fp, policy_id)
                info = read_internal_yaml(path)
                info['result_access_policy']['isolation_domain'] = 'other_project'
                write_yaml(path, info)
            elif counterexample == 'graph_claim_only':
                contributor['graph']['_projection_grant'] = read_internal_yaml(
                    _context_path(output / 'construction', fp, policy_id))
                records[owner].pop('result_policy_id')
            elif counterexample == 'no_marker':
                records[owner].pop('result_policy_id')
            elif counterexample == 'scope_claim':
                contributor['graph']['nodes'][1]['supports'][0]['scope_ref'] = 'restricted'
            elif counterexample == 'foreign_neighbor':
                contributor['graph']['nodes'][1]['supports'][0]['provenance']['source'] = {
                    'source_id': 'db', 'schema': 'dm', 'table': 'different2'}
        persisted.append(attach_checkpoint_grant(contributor, output, records[owner]))
    assert records['factory']['access_fingerprint'] != records['service']['access_fingerprint']
    policy, report = _compile(persisted)
    if counterexample == 'none':
        assert report['verified_groups'] == 1 and report['access_basis'] == 'same_frozen_table_grant'
        issued = attach_runtime_grants(data, config, records)
        issued_policy, issued_report = _compile(issued)
        assert issued_report == report and issued_policy == policy
        assert (merge_construction_graphs(issued, merge_policy=issued_policy) ==
                merge_construction_graphs(persisted, merge_policy=policy))
        assert not _compile(data)[0].verified_projection_rules
    else:
        assert not policy.verified_projection_rules, report


def test_same_immediate_parent_with_different_ancestors_is_not_one_entity():
    data = _batch()
    for owner_index, branch in enumerate(data):
        graph = branch['graph']
        graph['definitions']['relations'][0]['hierarchy_orientation'] = 'child_to_parent'
        parent = graph['nodes'][1]
        grandparent = deepcopy(parent)
        logical, = grandparent['identity']
        grandparent['identity'][logical] = 'different-grandparent-' + str(owner_index)
        grandparent['id'] += '_ancestor'
        grandparent['supports'][0]['label'] = grandparent['label'] = 'ancestor'
        graph['nodes'].append(grandparent)
        edge = deepcopy(graph['edges'][0])
        edge.update(id=edge['id'] + '_ancestor', source=parent['id'], target=grandparent['id'])
        graph['edges'].append(edge)
    policy, report = _compile(data)
    assert report['rejected_keys_by_reason']['different_ancestor_context'] == 1
    assert not policy.verified_projection_rules

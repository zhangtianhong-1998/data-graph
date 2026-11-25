from copy import deepcopy
from pathlib import Path

import pytest

from lightgraph.construction.graph_storage import _graph_digest
from lightgraph.construction.reviewed_keys import reviewed_observation_digest
from lightgraph.tests.test_cross_source_fusion import source_graph


@pytest.fixture
def campaign(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    import fusion_census_hypotheses
    return fusion_census_hypotheses


def branch(unit, axis='region', label='北京', key='R1'):
    g = source_graph('Location', 'code', key, unit, label, 'statistic', unit)
    # A single member per unit, each independently projected from a different table.
    n = g['nodes'][0]
    g['nodes'], g['edges'] = [n], []
    n.update(node_role='territorial_unit', identity_types={'code': 'text'})
    p = n['supports'][0]['provenance']
    p.update(source={'source_id': 'db', 'schema': 's', 'table': unit}, key_columns=[axis], label_column='caption')
    return {'contributor_id': unit, 'graph': g}


def experiment(module, inputs, reserved=(), excluded=()):
    parents = [{'node_id': i['contributor_id'], 'graph_digest': _graph_digest(i['graph'])} for i in inputs]
    return module.discover(inputs, parents, {'project_id': 'p', 'run_id': 'r'}, {
        'node_id': 'merge', 'artifact_id': 'a', 'upstream_artifacts': []}, reserved, excluded)


def test_three_tables_jointly_qualify_without_enumerating_or_publishing_pair_rules(campaign):
    inputs = [branch(x) for x in ('a', 'b', 'c')]
    report, rules = experiment(campaign, inputs)
    assert rules == []
    evidence = report['multiway']
    assert evidence['rejected_blocks'] == 0
    assert evidence['qualified_input_reduction'] == 2
    g, = evidence['groups']
    assert {m['projection']['graph_digest'] for m in g['members']} == {_graph_digest(i['graph']) for i in inputs}
    assert [m['node_id'] for m in g['members']] == ['a_1', 'b_1', 'c_1']
    mappings = {u: {u + '_1': 'ab' if u != 'c' else 'c'} for u in ('a', 'b', 'c')}
    snapshot = campaign.summarize_multiway_outputs(evidence, mappings)
    assert snapshot['hypothetical_node_reduction'] == 1
    mappings['a']['another'] = 'ab'
    assert campaign.summarize_multiway_outputs(evidence, mappings)['exclusion_counts'] == {'output_contains_other_input_members': 1}
    del mappings['c']['c_1']
    assert campaign.summarize_multiway_outputs(evidence, mappings)['exclusion_counts'] == {'unverified_input_to_output': 1}


@pytest.mark.parametrize(('difference', 'reason'), [
    ('name', 'different_name_audience_or_role'), ('scope', 'different_name_audience_or_role'),
    ('missing', 'missing_or_ambiguous_corroborated_observation'), ('role', 'no_shared_explicit_or_witnessed_role'),
    ('competing', 'competing_member_or_projection_in_unit'), ('exclude', 'excluded_key')])
def test_disagreement_or_unverified_third_member_blocks_pair_and_multiway(campaign, difference, reason):
    inputs = [branch(x) for x in ('a', 'b', 'c')]
    c = inputs[-1]['graph']['nodes'][0]
    if difference == 'name':
        c['supports'][0]['label'] = '天津'
    if difference == 'scope':
        c['supports'][0]['required_scope_refs'] = ['private']
    if difference == 'missing':
        del c['supports'][0]['provenance']['label_column']
    if difference == 'role':
        for i in inputs:
            del i['graph']['nodes'][0]['node_role']
    if difference == 'competing':
        inputs[-1]['graph']['nodes'].append({**deepcopy(c), 'id': 'second'})
    result, rules = experiment(campaign, inputs, excluded=['R1'] if difference == 'exclude' else [])
    assert rules == []
    assert result['multiway']['group_count'] == 0
    assert result['multiway']['rejection_counts'] == {reason: 1}


def test_independent_nodes_on_coincident_axes_survive_approval_pinning(campaign):
    inputs = [branch(x) for x in ('a', 'b')]
    for item in inputs:
        n = deepcopy(item['graph']['nodes'][0])
        n['id'] += '_offices'
        n['supports'][0]['provenance']['key_columns'] = ['office']
        item['graph']['nodes'].append(n)
    first, rules = experiment(campaign, inputs)
    assert first['hypothesized_pairs'] == 2
    assert {r['simulated_key_pairs'][0]['projections'][0]['key_column'] for r in rules} == {'office', 'region'}
    existing = [r['simulated_key_pairs'][0] for r in rules if r['simulated_key_pairs'][0]['projections'][0]['key_column'] == 'region']
    res, remaining = experiment(campaign, inputs, reserved=existing)
    assert res['hypothesized_pairs'] == 1
    rule, = remaining[0]['simulated_key_pairs']
    assert rule['projections'][0]['key_column'] == 'office'
    assert rule['observation_digests'] == [reviewed_observation_digest('text', 'R1', '北京')]


def test_separate_pair_witnesses_do_not_form_a_three_branch_hypothesis(campaign):
    inputs = [branch('a'), branch('b'), branch('c', key='R2', label='天津')]
    other = deepcopy(inputs[-1]['graph']['nodes'][0])
    other.update(id='b_2')
    other['supports'][0]['provenance']['source']['table'] = 'b'
    inputs[1]['graph']['nodes'].append(other)
    report, bundles = experiment(campaign, inputs)
    assert [len(report['multiway']['groups']), report['hypothesized_pairs']] == [0, 2]
    assert len(bundles) == 2


def test_three_branch_roles_may_be_proven_by_identical_directed_column_roles(campaign):
    inputs = [branch(x) for x in ('a', 'b', 'c')]
    for item in inputs:
        g = item['graph']
        n = g['nodes'][0]
        del n['node_role']
        target = {'id': 'catalog', 'type': 'Catalog', 'identity': {'id': 'catalog'}}
        g['nodes'].append(target)
        g['edges'].append({'id': 'loc', 'source': n['id'], 'target': 'catalog', 'type': 'in_catalog', 'supports': [
            {'provenance': {'source': n['supports'][0]['provenance']['source'],
                            'source_key_columns': ['region'], 'target_key_columns': ['catalog_id']}}]})
    report, _ = experiment(campaign, inputs)
    group, = report['multiway']['groups']
    assert group['role_basis'] == 'incident_edge_roles'
    e = inputs[-1]['graph']['edges'][0]
    e['target'], e['source'] = e['source'], e['target']
    report, _ = experiment(campaign, inputs)
    assert report['multiway']['rejection_counts'] == {'no_shared_explicit_or_witnessed_role': 1}

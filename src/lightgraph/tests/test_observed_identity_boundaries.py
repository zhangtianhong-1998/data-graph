"""Boundary cases for source-backed row replay through the workflow merger."""
from copy import deepcopy

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_observed_identity import projection, unit


def test_partial_row_context_cannot_hide_different_observed_projects():
    def contributor(name, project, property_name):
        graph = projection('Specimen', 'key')
        for node in graph['nodes']:
            support = node['supports'][0]
            support.pop('business_context')
            support['row_context'] = {'values': {'year': 2025}, 'value_types': {'year': 'integer'}}
            support['properties'] = {property_name: [project]}
            support['provenance']['property_columns'] = {property_name: 'project_code'}
            node['properties'] = deepcopy(support['properties'])
        return unit(name, graph)

    left = contributor('first', 'M', 'collection')
    right = contributor('second', 'K', 'programme')
    result = merge_construction_graphs([left, right])
    assert len(result['nodes']) == 4
    assert len(result['edges']) == 2
    same_project = contributor('third', 'M', 'programme')
    assert len(merge_construction_graphs([left, same_project])['nodes']) == 2


@pytest.mark.parametrize('shared_child', [False, True])
def test_identical_source_replays_survive_partially_overlapping_type_sources(shared_child):
    left = projection('Specimen', 'key', table='catalogue')
    right = projection('Specimen', 'key', table='register')
    for graph in (left, right):
        graph['edges'][0]['supports'][0]['provenance']['source']['table'] = 'catalogue_relationships'
        if shared_child:
            graph['nodes'][1]['supports'][0]['provenance']['source']['table'] = 'common_children'
    combined = deepcopy(left)
    for node, other in zip(combined['nodes'], right['nodes'], strict=True):
        node['supports'] += deepcopy(other['supports'])
    branches = [unit('first', left), unit('duplicate', left), unit('combined', combined), unit('extra', right)]
    result = merge_construction_graphs(branches)
    assert len(result['nodes']) == (4 if shared_child else 6)
    assert len(result['edges']) == 3
    assert merge_construction_graphs(list(reversed(branches))) == result
    for node in result['nodes']:
        units = {origin['contributor_id'] for support in node['supports'] for origin in support['execution_origins']}
        if shared_child and node['identity']['key'] == 'B':
            assert units == {'first', 'duplicate', 'combined', 'extra'}
        else:
            assert units in ({'first', 'duplicate'}, {'combined'}, {'extra'})


@pytest.mark.parametrize('scope', ['unknown', 'global'])
def test_declared_qualifier_blocks_shared_code_with_two_calibers(scope):
    graphs = []
    for prop, value in [('measure_basis', 'gross'), ('measure_basis_alias', 'net')]:
        graph = projection('Revenue', 'code')
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
        node = graph['nodes'][0]
        if scope == 'global':
            graph['definitions']['entities'][0].update(identity_scope=scope, identity_domain='catalog',
                                                       identity_evidence_refs=['e1'])
            node['identity_domain'] = 'catalog'
        support = node['supports'][0]
        support['provenance']['identity_qualifiers'] = {prop: 'caliber'}
        support['properties'] = {prop: [value]}
        node['properties'] = {prop: [value]}
        graphs.append(unit(prop, graph))
    merged = merge_construction_graphs(graphs)
    assert len(merged['nodes']) == 2
    assert {tuple(node['properties']) for node in merged['nodes']} == {('measure_basis',), ('measure_basis_alias',)}
    assert {c['reason'] for c in merged['candidate_identity_merges']} == {'conflicting_identity_qualifiers'}


@pytest.mark.parametrize('mismatch', [None, 'swapped_values', 'driver_type', 'physical_columns', 'scope'])
def test_composite_replay_compares_typed_values_by_source_column(mismatch):
    def branch(owner, keys, values):
        graph = projection('Account', 'code')
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
        graph['definitions']['entities'][0]['key_names'] = list(keys)
        node = graph['nodes'][0]
        node.update(id=owner, identity=values, identity_types={'code': 'text', 'project': 'text'})
        support = node['supports'][0]
        support['provenance']['key_columns'] = [name.upper() for name in keys]
        return unit(owner, graph)
    values = {'code': '001', 'project': 'M'}
    left = branch('one', ['project', 'code'], values)
    right = branch('two', ['code', 'project'], {'code': 'M', 'project': '001'} if mismatch == 'swapped_values' else values)
    node = right['graph']['nodes'][0]
    if mismatch == 'driver_type':
        node['identity_types']['code'] = 'integer'
    if mismatch == 'physical_columns':
        node['supports'][0]['provenance']['key_columns'] = ['OTHER_CODE', 'PROJECT']
    if mismatch == 'scope':
        node['supports'][0]['scope_ref'] = 'restricted'
    merged = merge_construction_graphs([left, right])
    assert len(merged['nodes']) == (2 if mismatch else 1)
    if not mismatch:
        assert len(merged['definitions']['entities']) == 1
        node, = merged['nodes']
        assert node['identity'] == values
        assert all(s['original_key_values'] == {'PROJECT': 'M', 'CODE': '001'} for s in node['supports'])
        assert merged == merge_construction_graphs([right, left])


def test_property_merge_scans_each_observation_once(monkeypatch):
    from lightgraph.construction import fusion

    calls = 0
    canonical = fusion.canonical

    def count(value):
        nonlocal calls
        calls += 1
        return canonical(value)

    monkeypatch.setattr(fusion, 'canonical', count)
    existing = {'properties': {'value': [True, 1, '1']}}
    seen = {}
    for i in range(128):
        fusion.merge_property_values(existing, {'properties': {'value': [i, {'index': i}, i]}}, seen)
    assert calls == 3 + 128 * 3
    assert existing['properties']['value'][:3] == [True, 1, '1']
    assert len(existing['properties']['value']) == 3 + 128 * 2 - 1


@pytest.mark.parametrize(('right_caliber', 'expected_nodes'), [('gross', 1), ('net', 2)])
def test_explicit_fusion_still_checks_qualifiers_after_property_aliasing(right_caliber, expected_nodes):
    branches, rules = [], []
    for kind, prop, value in [('Billing', 'basis1', 'gross'), ('Ledger', 'basis2', right_caliber)]:
        graph = projection(kind, 'code')
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
        node = graph['nodes'][0]
        support = node['supports'][0]
        support['provenance']['identity_qualifiers'] = {prop: 'caliber'}
        support['properties'] = {prop: [value]}
        node['properties'] = {prop: [value]}
        branches.append(unit(kind, graph))
        rules.append({'entity_type': kind, 'canonical_type': 'Revenue', 'identity_domain': 'metrics',
                      'key_mapping': {'code': 'code'}, 'property_aliases': {prop: 'measurement_basis'}})
    merged = merge_construction_graphs(branches, merge_policy={'fusion_policy': {'entity_rules': rules}})
    assert len(merged['nodes']) == expected_nodes
    assert {tuple(n['properties']['measurement_basis']) for n in merged['nodes']} == {('gross',), (right_caliber,)}
    if expected_nodes == 1:
        assert {s['contributor_id'] for s in merged['nodes'][0]['supports']} == {'Billing', 'Ledger'}
    else:
        assert {g['reason'] for g in merged['candidate_identity_merges']} == {'conflicting_identity_qualifiers'}

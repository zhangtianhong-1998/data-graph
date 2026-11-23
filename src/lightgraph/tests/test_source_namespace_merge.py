"""Equal source addresses registered in different projects are separate physical observations."""
import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_observed_dictionary_merge import _units as dictionary_units
from lightgraph.tests.test_observed_identity import projection, unit
from lightgraph.tests.test_workflow_catalog_replay import _columns, _publication


def owners(item):
    return {origin.get('contributor_id', s.get('contributor_id')) for s in item['supports']
            for origin in s.get('execution_origins') or [s]}


@pytest.mark.parametrize('other', ['museum', None, ''])
def test_physical_replay_unites_verified_peers_within_each_namespace(other):
    inputs = [unit(n, projection(), source_namespace=ns) for n, ns in [
        ('m1', 'library'), ('m2', 'library'), ('k1', other), ('k2', other)]]
    merged = merge_construction_graphs(inputs)
    groups = [{'m1', 'm2'}, {'k1', 'k2'}] if other else [{'m1', 'm2'}, {'k1'}, {'k2'}]
    assert len(merged['nodes']) == 2 * len(groups)
    assert len(merged['edges']) == len(groups)
    by_id = {n['id']: n for n in merged['nodes']}
    for edge in merged['edges']:
        assert owners(edge) in groups
        assert owners(by_id[edge['source']]) == owners(edge) == owners(by_id[edge['target']])
    assert merged == merge_construction_graphs(inputs[::-1])
    assert all(c['source_key_comparison'] == 'unverified_source_namespace'
               for c in merged['candidate_identity_merges'] if c['match_basis'] == 'same_typed_identity')


def test_dictionary_reference_has_to_point_to_a_member_in_the_same_namespace():
    inputs = dictionary_units()
    for unit_, namespace in zip(inputs, ['p', 'p', 'q'], strict=True):
        unit_['source_namespace'] = namespace
    result = merge_construction_graphs(inputs)
    members = [n for n in result['nodes'] if n['type'] == 'Member']
    assert len(members) == 2
    assert {frozenset(owners(n)) for n in members} == {
        frozenset({'registry', 'notes'}), frozenset({'samples'})}
    by_id = {m['id']: m for m in members}
    assert all(owners(e) <= owners(by_id[e['target']]) for e in result['edges'])


def test_physical_catalog_ids_and_their_edges_stay_in_their_project_when_values_and_raw_ids_match():
    graph = _publication()
    inputs = [unit(cid, graph, source_namespace=ns) for cid, ns in [('p1', 'p'), ('p2', 'p'), ('q', 'q')]]
    result = merge_construction_graphs(inputs)
    columns = {n['id']: n for n in _columns(result)}
    assert len(columns) == 2
    for edge in result['edges']:
        assert owners(edge) <= owners(columns[edge['target']])
        assert owners(columns[edge['target']]) in ({'p1', 'p2'}, {'q'})


def test_frozen_outputs_with_same_source_scoped_ids_remain_separate():
    original = merge_construction_graphs([unit('a', projection()), unit('b', projection())])
    inputs = [unit('p', original, 'merged', source_namespace='p'),
              unit('q', original, 'merged', source_namespace='q')]
    result = merge_construction_graphs(inputs)
    assert len(result['nodes']) == 4
    assert len(result['edges']) == 2
    assert not {result['edges'][0]['source'], result['edges'][0]['target']}.intersection(
        {result['edges'][1]['source'], result['edges'][1]['target']})


def test_frozen_publication_cannot_be_relabelled_as_another_physical_project():
    original = merge_construction_graphs([unit('a', projection(), source_namespace='p'),
                                          unit('b', projection(), source_namespace='p')])
    claimed = unit('stage', original, 'merged', source_namespace='q')
    result = merge_construction_graphs([claimed, unit('q', projection(), source_namespace='q')])
    assert len(result['nodes']) == 4
    assert len(result['edges']) == 2

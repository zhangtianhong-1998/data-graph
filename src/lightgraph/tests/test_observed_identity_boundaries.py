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

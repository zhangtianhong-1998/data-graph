"""Business outcomes for identity fusion. Run explicitly with pytest on this file.

The first pair exercises extraction, unit publication and deterministic merging.
The second pair supplies a fixed model proposal to the real verifier and merger.
All tables, claims, names and observations in this file are synthetic.
"""

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_projection_proposals import _bind_section, _compile
from lightgraph.tests.test_scoped_workflow_document_binding import (
    SHARED_ACCESSION_SECTIONS,
    merge_csv_units_citing_registry,
)


LOCAL_ACCESSION_SECTIONS = (
    'Each site assigns its own accession numbers. Matching numbers at different sites identify different specimens.',
    'Site A uses its local accession number; it is only unique at site A.',
    'Site B uses its local accession number; it is only unique at site B.',
)


@pytest.mark.parametrize(('sections', 'expected_source_groups'), [
    (SHARED_ACCESSION_SECTIONS, {frozenset({'a', 'b'})}),
    (LOCAL_ACCESSION_SECTIONS, {frozenset({'a'}), frozenset({'b'})}),
], ids=['common-registry', 'separate-registries-explicit-in-cited-text'])
def test_citation_must_support_the_actual_identity_claim(tmp_path, sections, expected_source_groups):
    graph = merge_csv_units_citing_registry(tmp_path, sections, allow_local_fallback=(sections == LOCAL_ACCESSION_SECTIONS))
    specimens = [node for node in graph['nodes'] if node['type'] == 'Specimen']

    def source_groups(node):
        return frozenset(support['provenance']['source']['source_id'] for support in node['supports'])

    assert {n['identity']['accession'] for n in specimens} == {'0007'}
    groups = [source_groups(n) for n in specimens]
    assert len(groups) == len(expected_source_groups) and set(groups) == expected_source_groups, groups
    classes = {node['id']: source_groups(node) for node in graph['nodes'] if node['type'] == 'LocalClass'}
    sources = {node['id']: source_groups(node) for node in specimens}
    assert len(graph['edges']) == 2
    for edge in graph['edges']:
        assert classes[edge['target']] == source_groups(edge) <= sources[edge['source']]


@pytest.mark.parametrize('second_neighbor', ['000072', '000073'], ids=['same-neighbor', 'additional-neighbor'])
def test_ordinary_neighbors_can_complement_one_verified_identity(second_neighbor):
    contributors = [unit('a', 'ActualArea', 'geo_code', table='actuals', role='served_by'),
                    unit('b', 'TargetRegion', 'geo_code', table='targets', role='served_by')]
    contributors[1]['graph']['nodes'][1]['identity']['geo_code'] = second_neighbor
    for contributor in contributors:
        _bind_section(contributor, 'v1')
        contributor['graph']['edges'][0]['supports'][0]['business_context'] = {'status': 'global', 'dimensions': {}}

    policy, result = _compile(contributors, 'same_logical_keys_across_sources')
    assert result['verified_groups'] == 1, result['rejected_keys_by_reason']
    graph = merge_construction_graphs(contributors, merge_policy=policy)
    areas = [n for n in graph['nodes'] if n['identity'].get('geo_code') == '000071']
    assert len(areas) == 1
    assert {s['reviewed_input_node_id'] for s in areas[0]['supports']} == {'a_0', 'b_0'}
    nodes = {n['id']: n for n in graph['nodes']}
    assert len(graph['edges']) == 2
    observed = {(s['provenance']['source']['table'], nodes[e['target']]['identity']['geo_code'])
                for e in graph['edges'] for s in e['supports'] if e['source'] == areas[0]['id']}
    assert observed == {('actuals', '000072'), ('targets', second_neighbor)}

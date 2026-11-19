"""Regression coverage for the offline topology and its derived artifacts."""
from copy import deepcopy
import json
import random

import pytest

from lightgraph.construction.graph_analytics import ALGORITHM_VERSION, compute_graph_analytics


def graph(ids, links):
    return {'version_id': 'published_v1', 'nodes': [{'id': nid} for nid in ids],
            'edges': [{'source': src, 'target': dst, 'type': relation} for src, dst, relation in links]}


def test_scores_preserve_direction_and_parallel_relations_do_not_multiply_weight():
    data = graph('ab', [('a', 'b', 'child'), ('a', 'b', 'part'), ('a', 'b', 'child')])
    result = compute_graph_analytics(data)
    nodes = {row['id']: row for row in result['nodes']}
    assert result['algorithm_version'] == ALGORITHM_VERSION and result['version_id'] == 'published_v1'
    assert result['status'] == 'completed'
    assert (nodes['a']['out_degree'], nodes['b']['in_degree']) == (1, 1)
    assert result['counts']['edges'] == 3 and result['counts']['directed_pairs'] == 1
    assert result['relation_counts'] == {'child': 2, 'part': 1}
    # Analytic stationary solution for a -> b with one dangling node.
    assert nodes['b']['pagerank'] == pytest.approx(1.85 / 2.85, abs=1e-6)
    assert sum(n['pagerank'] for n in nodes.values()) == pytest.approx(1.0)
    assert nodes['b']['degree_centrality'] == nodes['a']['degree_centrality'] == 1
    assert nodes['a']['community_id'] == nodes['b']['community_id']


def test_version_result_is_order_independent_json_and_preserves_input_and_rng():
    data = graph('abcdef', [('a', 'b', 'r'), ('b', 'c', 'r'), ('c', 'a', 'r'),
                             ('a', 'd', 'r'), ('d', 'e', 'r'), ('e', 'f', 'r'), ('f', 'd', 'r')])
    before = deepcopy(data)
    rng = random.getstate()
    first = compute_graph_analytics(data)
    assert data == before and random.getstate() == rng
    assert json.loads(json.dumps(first)) == first
    data['nodes'].reverse()
    data['edges'].reverse()
    assert compute_graph_analytics(data) == first
    assert first['algorithms']['community']['converged']
    assert first['algorithms']['pagerank']['converged']


def test_components_isolated_nodes_and_self_loops():
    data = graph('abcdef', [('a', 'b', 'r'), ('b', 'c', 'r'), ('c', 'a', 'r'), ('d', 'd', 'r')])
    result = compute_graph_analytics(data)
    node = {row['id']: row for row in result['nodes']}
    assert len({node[n]['community_id'] for n in 'abc'}) == 1
    assert len({node[n]['community_id'] for n in 'adef'}) == 4
    assert result['counts']['isolated_nodes'] == 2
    assert node['d']['in_degree'] == node['d']['out_degree'] == 1
    assert node['d']['degree_centrality'] == 0 and node['e']['degree_centrality'] == 0
    assert sorted(row['size'] for row in result['communities']) == [1, 1, 1, 3]


def test_empty_graph_and_incomplete_records():
    empty = compute_graph_analytics(graph('', []))
    assert empty['status'] == 'completed' and not empty['nodes'] and not empty['communities']
    assert empty['algorithms']['pagerank']['iterations'] == 0
    data = graph(['only', 'only'], [('only', 'hidden', 'r')])
    result = compute_graph_analytics(data)
    assert result['status'] == 'completed_with_gaps'
    assert result['counts']['skipped_node_records'] == result['counts']['edges_missing_endpoints'] == 1
    assert result['nodes'][0]['pagerank'] == 1
    assert 'hidden' not in str(result)

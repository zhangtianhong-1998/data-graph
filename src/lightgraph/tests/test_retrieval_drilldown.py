"""Follow a single evidenced business hierarchy through the production SQLite index."""
import json

import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.drilldown import retrieve_drilldown
from lightgraph.tests.test_business_context_retrieval import _context, _graph, _support


@pytest.fixture
def retriever(tmp_path):
    graph = _graph()
    m = _context('scoped', project='M')
    for nid, label in [('a', '销售科目'), ('b', '清单'), ('z', '最终收入'), ('other', '汇总备注')]:
        graph['nodes'].append({'id': nid, 'type': '指标', 'identity': {'code': nid}, 'supports': [_support(label, m)]})
    for eid, src, dst in [('a1', 'global2', 'a'), ('a2', 'a', 'b'), ('a3', 'b', 'z'), ('b1', 'global2', 'other')]:
        graph['edges'].append({'id': eid, 'type': 'links', 'source': src, 'target': dst, 'supports': [_support('', m)]})
    graph['edges'].append({'id': 'back', 'type': 'cycle', 'source': 'a', 'target': 'a', 'supports': [_support('', m)]})
    sync_graph_to_retrieval(graph, tmp_path)
    return SubgraphRetriever(tmp_path)


def drill(retriever, query='', **opts):
    return retrieve_drilldown(retriever, query, ['public'], {'project': 'M'}, **opts)


def test_drilldown_selects_one_deep_path_covering_both_anchors(retriever):
    result = drill(retriever, '收入', seed_ids=['m', 'a'], relation_types=['links'])
    path, = result['drilldown']['paths']
    assert path['node_ids'] == ['m', 'common', 'global2', 'a', 'b', 'z']
    assert path['stop_reason'] == 'leaf'
    assert result['drilldown']['uncovered_seed_ids'] == []
    assert [n['id'] for n in result['nodes']] == path['node_ids']
    assert [e['id'] for e in result['edges']] == path['edge_ids']
    assert 'K_SECRET' not in json.dumps(result)
    assert not result['truncated']


def test_exact_target_direction_and_budget(retriever):
    up = drill(retriever, seed_ids=['z'], target_id='m', direction='incoming', relation_types=['links'])
    assert up['drilldown']['paths'][0]['node_ids'] == ['z', 'b', 'a', 'global2', 'common', 'm']
    assert up['drilldown']['paths'][0]['stop_reason'] == 'target_reached'
    missing = drill(retriever, seed_ids=['m'], target_id='z', max_depth=2)
    assert not missing['nodes'] and missing['drilldown']['status'] == 'target_not_reached'
    assert 'depth_limit' in missing['truncation_reasons']
    short = drill(retriever, seed_ids=['m'], max_nodes=2)
    assert short['drilldown']['paths'][0]['stop_reason'] == 'output_limit'
    cycle = drill(retriever, seed_ids=['a'], relation_types=['cycle'])
    assert cycle['drilldown']['paths'][0]['stop_reason'] == 'cycle'


def test_query_bootstrap_and_invisible_target(retriever):
    selected = drill(retriever, 'M起点', target_id='z', relation_types=['links'])
    assert selected['slots'] and selected['drilldown']['paths'][0]['node_ids'][0] == 'm'
    assert not drill(retriever, seed_ids=['m'], target_id='k')['nodes']
    assert drill(retriever, seed_ids=['k'])['drilldown']['status'] == 'no_visible_seed'


def test_all_business_contexts_still_require_compatible_witnesses(tmp_path):
    sync_graph_to_retrieval(_graph(), tmp_path)
    retriever = SubgraphRetriever(tmp_path)
    # The shared node has both M and K observations; its identity does not join those paths.
    out = retrieve_drilldown(retriever, '', ['public'], None, seed_ids=['m'], target_id='k')
    assert out['drilldown']['status'] == 'target_not_reached' and not out['edges']



def test_underdetermined_branch_is_reported_and_output_stays_compact(retriever):
    near = drill(retriever, seed_ids=['global2'], max_depth=1, relation_types=['links'])
    assert near['drilldown']['paths'][0]['node_ids'] == ['global2', 'other']
    assert near['drilldown']['paths'][0]['stop_reason'] == 'leaf'
    assert near['truncated']  # Another branch leads beyond the exploration depth.
    fork = drill(retriever, seed_ids=['a', 'other'], relation_types=['unrelated_relation'])
    assert fork['drilldown']['equally_ranked_paths'] == 2
    assert len(fork['nodes']) == 1 and len(fork['drilldown']['uncovered_seed_ids']) == 1


def test_target_guides_search_past_a_wide_root_without_returning_siblings(tmp_path):
    graph = _graph()
    common = _context('global')
    for i in range(65):
        nid = f'sibling_{i}'
        graph['nodes'].append({'id': nid, 'type': 'Entity', 'identity': {'code': nid},
                               'supports': [_support(nid, common)]})
        graph['edges'].append({'id': f'branch_{i}', 'type': 'links', 'source': 'm', 'target': nid,
                               'supports': [_support('', common)]})
    sync_graph_to_retrieval(graph, tmp_path)
    result = drill(SubgraphRetriever(tmp_path), seed_ids=['m'], target_id='global2')
    assert result['drilldown']['paths'][0]['node_ids'] == ['m', 'common', 'global2']
    assert not result['truncated']
    assert result['drilldown']['expanded_nodes'] == 2
    by_hint = drill(SubgraphRetriever(tmp_path), 'sibling_9', seed_ids=['m'])
    assert by_hint['drilldown']['paths'][0]['node_ids'] == ['m', 'sibling_9']


def test_path_rejects_same_printed_dimension_with_different_source_types(tmp_path):
    graph = _graph()
    for item, kind in [(graph['edges'][0], 'date'), (graph['edges'][1], 'text')]:
        item['supports'] = [_support('', {**_context('scoped', period='2026-01-01'),
                                          'value_types': {'period': kind}})]
    sync_graph_to_retrieval(graph, tmp_path)
    result = retrieve_drilldown(SubgraphRetriever(tmp_path), '', ['public'], None, seed_ids=['m'], target_id='global2')
    assert not result['edges'] and not result['drilldown']['paths']

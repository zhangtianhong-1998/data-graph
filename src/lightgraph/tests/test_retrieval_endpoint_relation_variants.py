"""A relation can acquire different endpoint types when just one identity decision is active."""
from copy import deepcopy
import sqlite3

import pytest

from lightgraph.construction.hierarchy_replay import _update_endpoint_contracts
from lightgraph.construction.human_identity import project
from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.tests.test_dimensional_retrieval import graph as dimensional_graph


def _scope(label):
    return {'scope_ref': 'public', 'label': label, 'properties': {},
            'business_context': {'status': 'global', 'dimensions': {}}}


def _publication(active):
    kinds = {'old': 'TreeIndicator', 'canonical': 'Indicator', 'target': 'Dimension'}
    graph = {'support_visibility_version': 1, 'nodes': [], 'edges': [], 'definitions': {
        'entities': [{'type': kind, 'key_names': ['code']} for kind in kinds.values()],
        'relations': [{'type': 'reported', 'source_type': kinds['old'], 'target_type': kinds['target']}]}}
    decisions = []
    for row, code in enumerate(('order', 'revenue'), 10):
        for role, kind in kinds.items():
            graph['nodes'].append({'id': f'{role}-{code}', 'type': kind, 'identity': {'code': code},
                                   'supports': [_scope(f'{role}-{code}')]})
        witness = {'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': [[row, row]]}
        graph['edges'].append({'id': code, 'type': 'reported', 'source': f'old-{code}',
                               'target': f'target-{code}', 'supports': [
                                   {**_scope(''), 'source_row_witness': witness}]})
        decisions.append({'decision_id': code, 'verdict': 'same',
                          'status': 'active' if code in active else 'revoked',
                          'members': [f'old-{code}', f'canonical-{code}']})
    return project(graph, decisions, 'original')


def _reader(graph, root):
    sync_graph_to_retrieval(graph, root)
    return SubgraphRetriever(root)


def _path(reader, edge, name, *, expand=False, mode='traverse', scopes=('public',)):
    request = RetrieveSubgraphRequest(mode=mode, seed_ids=[edge['source']], target_id=edge['target'],
                                      direction='outgoing', max_depth=1, relation_types=[name],
                                      **({'include_endpoint_variants': True} if expand else {}))
    return request.run(reader, list(scopes))['edges']


@pytest.mark.parametrize('active,specialized', [
    ((), set()), (('order',), {'order'}), (('order', 'revenue'), set()), (('revenue',), {'revenue'})])
def test_each_publication_filters_the_same_relationship_after_independent_decisions(tmp_path, active, specialized):
    graph = _publication(active)
    reader = _reader(graph, tmp_path)
    for edge in graph['edges']:
        code = edge['target'].removeprefix('target-')
        assert (edge['type'] != 'reported') == (code in specialized)
        for mode in ('traverse', 'drilldown'):
            assert bool(_path(reader, edge, 'reported', mode=mode)) == (code not in specialized)
            assert _path(reader, edge, edge['type'], mode=mode)[0]['id'] == edge['id']
            match, = _path(reader, edge, 'reported', expand=True, mode=mode)
            assert (match['id'], match['relation'], match['source'], match['target']) == (
                edge['id'], edge['type'], edge['source'], edge['target'])
            assert match['observations'][0]['source_row_witness'] == edge['supports'][0]['source_row_witness']
            assert not _path(reader, edge, 'unknown', expand=True, mode=mode)
    if specialized:
        primary, = [e for e in graph['edges'] if e['type'] == 'reported']
        variant, = [e for e in graph['edges'] if e['type'] != 'reported']
        assert not _path(reader, primary, variant['type'], expand=True)


def test_shared_name_and_restricted_specializations_do_not_widen_visibility_or_meaning(tmp_path):
    graph = _publication(['order'])
    projected, = [e for e in graph['edges'] if e['type'] != 'reported']
    projected['supports'][0]['scope_ref'] = 'staff'
    sibling = deepcopy(next(d for d in graph['definitions']['relations'] if d['type'] == projected['type']))
    sibling.update(type='reported__def_other_meaning', description='A different business relation')
    graph['definitions']['relations'].append(sibling)
    other = {**deepcopy(projected), 'id': 'different', 'type': sibling['type'], 'supports': [_scope('')]}
    graph['edges'].append(other)
    reader = _reader(graph, tmp_path)
    assert _path(reader, other, other['type'])[0]['id'] == other['id']
    assert not _path(reader, other, 'reported', expand=True)
    match, = _path(reader, other, 'reported', expand=True, scopes=('public', 'staff'))
    assert match['id'] == projected['id']


def test_old_index_serves_exact_requests_and_requires_sync_for_expansion(tmp_path):
    graph = _publication(['order'])
    projected, = [e for e in graph['edges'] if e['type'] != 'reported']
    reader = _reader(graph, tmp_path)
    with sqlite3.connect(reader.index.path) as db:
        db.execute('DROP TABLE kg_endpoint_specializations')
    reader = SubgraphRetriever(tmp_path)
    assert _path(reader, projected, projected['type'])[0]['id'] == projected['id']
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        _path(reader, projected, 'reported', expand=True)


def test_spoken_relation_and_explicit_path_filter_intersect_on_visible_types(tmp_path):
    graph = _publication(['order'])
    nodes = {node['id']: node for node in graph['nodes']}
    nodes['canonical-order']['supports'][0]['label'] = '订货指标'
    nodes['target-order']['supports'][0]['label'] = '订单记录'
    primary, = [e for e in graph['edges'] if e['type'] == 'reported']
    primary['supports'][0]['scope_ref'] = 'staff'
    variant, = [e for e in graph['edges'] if e['type'] != 'reported']
    sync_graph_to_retrieval(graph, tmp_path)
    for spoken, selected in [('reported', variant['type']), (variant['type'], 'reported')]:
        parsed = {'intent': 'traverse', 'mentions': ['订货指标', '订单记录'],
                  'traversal': {'start': '订货指标', 'end': '订单记录', 'relation': spoken}}
        reader = SubgraphRetriever(tmp_path, mention_extractor=lambda _: parsed)
        question = f'从订货指标到订单记录，只沿 {spoken} 关系查找路径'
        request = {'mode': 'traverse', 'query': question, 'relation_types': [selected]}
        exact = RetrieveSubgraphRequest(**request).run(reader, ['public'])
        assert not exact['edges']
        expanded = RetrieveSubgraphRequest(**request, include_endpoint_variants=True).run(reader, ['public'])
        assert [e['id'] for e in expanded['edges']] == [variant['id']]


def test_dimensional_drill_expands_only_the_filtered_hierarchy_step(tmp_path):
    graph = dimensional_graph()
    graph['definitions']['relations'].append({'type': 'has_child', 'source_type': '产品', 'target_type': '型号'})
    nodes = {n['id']: n for n in graph['nodes']}
    nodes['r1']['type'] = '销售型号'
    _update_endpoint_contracts(graph, nodes, {'has_child'})
    reader = _reader(graph, tmp_path)
    query = {'mode': 'dimensional', 'conditions': [{'role': role, 'node_id': nid} for role, nid in [
        ('地区', 'east'), ('指标', 'rev'), ('产品', 'router')]], 'business_context': {'project': 'M'},
        'drill_role': '产品', 'relation_types': ['has_child']}
    exact = RetrieveSubgraphRequest(**query).run(reader, ['public'])
    expanded = RetrieveSubgraphRequest(**query, include_endpoint_variants=True).run(reader, ['public'])
    assert exact['dimensional']['children'] == []
    assert expanded['dimensional']['children'] == ['r1']
    assert expanded['dimensional']['fact_ids'] == exact['dimensional']['fact_ids'] == ['fact']

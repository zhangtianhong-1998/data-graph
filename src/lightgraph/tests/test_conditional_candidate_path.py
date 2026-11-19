"""Keep paths supported under a candidate interpretation outside the verified answer."""
from copy import deepcopy

import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.tests.test_semantic_query_planner import MaintenanceEmbedder, maintenance_graph


QUERY = 'M 项目中，装配车间的空气压缩机轴承过热，对应哪些零件？'


def ask(tmp_path, graph=None, query=QUERY, **limits):
    embedder = MaintenanceEmbedder()
    sync_graph_to_retrieval(graph or maintenance_graph(), tmp_path, embedder=embedder)
    request = RetrieveSubgraphRequest.model_validate({'query': query, **limits})
    return request.run(SubgraphRetriever(tmp_path, embedder=embedder), scopes=['public'])


def test_conditional_path_uses_one_fact_and_only_its_supported_child(tmp_path):
    result = ask(tmp_path)
    assert result['plan']['intent'] == 'clarify' and not result['nodes'] and not result['edges']
    path = result['available_context']['candidate_path']
    assert path['assumptions'] == [{'text': '空气压缩机', 'node_id': 'machine', 'display_name': '空压机', 'kind': '设备'}]
    assert path['unverified_terms'] == ['空气压缩机'] and path['requested_level'] == '零件'
    assert path['level_node_ids'] == ['bearing'] and len(path['edges']) == 4
    assert [w['row_id'] for w in path['witnesses']] == ['job1']
    assert {n['id'] for n in path['nodes']} == {'bearing', 'machine', 'job', 'place', 'fault'}
    assert all(edge['target'] != 'valve' for edge in path['edges']) and 'restricted' not in str(path)


@pytest.mark.parametrize('case', ['ambiguous', 'other_row', 'unknown_scope', 'no_anchor', 'budget', 'unknown_condition'])
def test_candidate_path_requires_independent_conditions_and_complete_evidence(tmp_path, case):
    graph = maintenance_graph(ambiguous=case == 'ambiguous')
    query = QUERY
    limits = {}
    if case == 'other_row':
        edge = next(e for e in graph['edges'] if e['target'] == 'machine')
        edge['supports'][0]['row_context'] = {'id': 'another', 'values': deepcopy(edge['supports'][0]['row_context']['values'])}
    if case == 'unknown_scope':
        query = QUERY.replace('M 项目', 'Unknown 项目')
    if case == 'no_anchor':
        query = 'M 项目中，空气压缩机对应哪些零件？'
    if case == 'unknown_condition':
        query += '还需检修时长超过八小时。'
    if case == 'budget':
        limits['max_nodes'] = 3
    result = ask(tmp_path, graph, query, **limits)
    assert 'candidate_path' not in result['available_context']
    assert not result['edges'] and not result['nodes']

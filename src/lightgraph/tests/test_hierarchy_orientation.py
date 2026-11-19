from copy import deepcopy
import sqlite3

import pytest

from lightgraph.construction.models import RelationDefinition
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.tests.test_semantic_query_planner import maintenance_graph


def retrieval(tmp_path, graph):
    sync_graph_to_retrieval(graph, tmp_path)
    return SubgraphRetriever(tmp_path)


def ask(retriever, text):
    return RetrieveSubgraphRequest.model_validate({'query': text}).run(retriever, ['public'])


def definition(name, src, dst, orientation):
    return {'type': name, 'source_type': src, 'target_type': dst, 'hierarchy_orientation': orientation}


def equipment():
    graph = maintenance_graph()
    graph['definitions']['relations'] = [definition('装配有', '设备', '零件', 'parent_to_child')]
    for edge in graph['edges'][-2:]:
        edge['type'] = '装配有'
    return graph


def reverse_edge(edge):
    edge['source'], edge['target'] = edge['target'], edge['source']
    prov = edge['supports'][0]['provenance']
    prov['source_key_columns'], prov['target_key_columns'] = prov['target_key_columns'], prov['source_key_columns']


@pytest.mark.parametrize('reverse', [False, True])
def test_custom_hierarchy_joins_only_children_of_the_fact(tmp_path, reverse):
    graph = equipment()
    if reverse:
        graph['definitions']['relations'] = [definition('装设于', '零件', '设备', 'child_to_parent')]
        for edge in graph['edges'][-2:]:
            reverse_edge(edge)
            edge['type'] = '装设于'
    ret = retrieval(tmp_path, graph)
    out = ask(ret, 'M 项目中，装配车间的空压机轴承过热，对应哪些零件？')
    assert out['dimensional']['level_node_ids'] == ['bearing']
    assert 'valve' not in [n['id'] for n in out['nodes']]


def test_declared_mixed_orientation_is_local_to_each_step(tmp_path):
    graph = equipment()
    node = deepcopy(next(n for n in graph['nodes'] if n['id'] == 'bearing'))
    node.update(id='shaft', type='子件', identity={'code': 'C3'})
    node['supports'][0]['label'] = '轴瓦'
    graph['nodes'].append(node)
    edge = deepcopy(graph['edges'][-2])
    edge.update(id='shaft-bearing', source='shaft', target='bearing', type='装设于')
    graph['edges'].append(edge)
    graph['definitions']['relations'].append(definition('装设于', '子件', '零件', 'child_to_parent'))
    ret = retrieval(tmp_path, graph)
    out = ask(ret, 'M 项目的空压机下钻到子件')
    assert out['drilldown']['paths'][0]['node_ids'] == ['machine', 'bearing', 'shaft']


def test_unverified_conflicting_and_invisible_hierarchies(tmp_path):
    graph = equipment()
    # A directed acyclic association with a plausible name is insufficient.
    graph['definitions']['relations'][0].pop('hierarchy_orientation')
    graph['definitions']['relations'][0]['acyclic'] = True
    for edge in graph['edges'][-2:]:
        edge['type'] = '包含'
    graph['definitions']['relations'][0]['type'] = '包含'
    graph['definitions']['relations'] += [
        definition('has_child', '记录', '设备', 'none'),
        definition('私有层次', '设备', '零件', 'parent_to_child'),
        definition('归属', '记录', '车间', 'parent_to_child'),
        definition('归属', '记录', '车间', 'child_to_parent'),
    ]
    graph['edges'][0]['type'] = '归属'
    graph['edges'][2]['type'] = 'has_child'
    edge = deepcopy(graph['edges'][-1])
    edge.update(id='private', type='私有层次')
    edge['supports'][0]['scope_ref'] = 'secret'
    graph['edges'].append(edge)
    ret = retrieval(tmp_path, graph)
    assert ret.index.authorized_hierarchies(ret.authorized_cohorts(['public'], {'project':'M'})) == {}
    assert ret.index.authorized_hierarchies(ret.authorized_cohorts(['secret','public'], {'project':'M'})) == {
        ('设备','私有层次','零件'): 'outgoing'}


def test_legacy_index_and_draft_preserve_canonical_predicates_and_fingerprint(tmp_path):
    mapping = {'source_id':'s', 'table':'t', 'source_key_columns':['a'], 'target_key_columns':['b']}
    base = dict(type='custom', source_type='A', target_type='B', mappings=[mapping])
    assert 'hierarchy_orientation' not in RelationDefinition.model_validate(base).model_dump()
    directed = RelationDefinition.model_validate({**base, 'hierarchy_orientation':'child_to_parent'})
    assert directed.model_dump()['hierarchy_orientation'] == 'child_to_parent'
    ret = retrieval(tmp_path, maintenance_graph())
    with sqlite3.connect(ret.index.path) as db:
        db.execute('DROP TABLE kg_relation_semantics')
    assert ret.index.authorized_hierarchies(ret.authorized_cohorts(['public'], {'project':'M'})) == {
        ('设备','has_child','零件'): 'outgoing'}

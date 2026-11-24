"""Direct parents and children preserve independent assertions of one endpoint pair."""
from copy import deepcopy
import sqlite3

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest

TABLE = {'source_id': 'file', 'schema': 'public', 'table': 'Sheet1'}


def support(label, levels=None, scope='public', dept=None):
    item = {'scope_ref': scope, 'label': label, 'business_context': {
        'status': 'scoped' if dept else 'global', 'dimensions': {'dept': dept} if dept else {}}}
    if levels:
        a, b = levels
        item['provenance'] = {'source': TABLE, 'source_key_columns': [f'SUBJECT_L{a}_CODE'],
                             'target_key_columns': [f'SUBJECT_L{b}_CODE']}
    return item


def publication():
    nodes = [{'id': key, 'type': '科目', 'identity': {'code': key},
              'supports': [support(label)]} for key, label in [('p', '总成本'), ('c', '服务成本'), ('d', '服务成本')]]
    rels, edges = {}, []

    def add(eid, rel, src, dst, levels, orientation=None, scope='public'):
        definition = {'type': rel, 'source_type': '科目', 'target_type': '科目'}
        if orientation is not None:
            definition['hierarchy_orientation'] = orientation
        rels[rel] = definition
        edges.append({'id': eid, 'type': rel, 'source': src, 'target': dst,
                      'supports': [support(None, levels, scope=scope)]})

    add('s', 'indicator_parent_child', 'p', 'p', (1, 2))
    add('a', 'declared', 'p', 'c', None, 'parent_to_child')
    add('b', 'indicator_parent_child__def_' + 'f' * 32, 'p', 'c', (1, 2))
    add('c', 'indicator_has_child_indicator', 'p', 'c', (1, 2))
    add('d', 'report_item_parent_of_report_item', 'p', 'c', (1, 2))
    add('e', 'report_item_l3_in_l2', 'c', 'p', (3, 2))
    add('f', 'report_item_l5_in_l4', 'c', 'p', (5, 4))
    add('g', '私有的逆向层级', 'd', 'p', (3, 2), 'child_to_parent', scope='private')
    add('h', 'indicator_parent_child', 'p', 'd', (3, 1))
    add('i', 'ledger_parent_child', 'p', 'd', (1, 2), 'none')
    add('j', 'related', 'p', 'd', (1, 2))
    add('k', 'report_item_l3_in_l2', 'd', 'p', (5, 4))
    add('l', 'has_child', 'p', 'd', None)
    return {'support_visibility_version': 1, 'nodes': nodes, 'edges': edges,
            'definitions': {'entities': [{'type': '科目', 'key_names': ['code']}], 'relations': list(rels.values())}}


def ask(reader, scopes=('public',), **kwargs):
    return RetrieveSubgraphRequest(**kwargs).run(reader, list(scopes))


def test_mixed_directions_distinct_physical_levels_and_scopes(tmp_path):
    raw = publication()
    sync_graph_to_retrieval(raw, tmp_path)
    r = SubgraphRetriever(tmp_path)
    out = ask(r, mode='hierarchy', node_id='p')
    assert not out['truncated']
    assert out['hierarchy']['items'] == [{'node_id': 'c', 'edge_ids': list('abcdef')}]
    originals = {e['id']: e for e in raw['edges']}
    for e in out['edges']:
        source = originals[e['id']]
        assert [e['source'], e['target'], e['relation']] == [source['source'], source['target'], source['type']]
        if source['supports'][0].get('provenance'):
            assert e['observations'][0]['provenance'] == source['supports'][0]['provenance']
        assert 'source_row_witness' not in e['observations'][0]
    assert ask(r, mode='hierarchy', hierarchy_direction='parents', node_id='c')['hierarchy']['items'] == [
        {'node_id': 'p', 'edge_ids': list('abcdef')}]
    assert ask(r, mode='hierarchy', scopes=('private', 'public'), query='总成本')['hierarchy']['items'][-1] == {
        'node_id': 'd', 'edge_ids': ['g']}
    assert ask(r, query='总成本的直接下级有哪些？')['hierarchy']['items'] == out['hierarchy']['items']
    ambiguous = ask(r, query='服务成本的直接上级有哪些？')
    assert ambiguous['hierarchy']['status'] == 'ambiguous' and ambiguous['edges'] == []
    assert {(c['node_id'], c['display_name'], c['identity']['code']) for c in ambiguous['slots'][0]['candidates']} == {
        ('c', '服务成本', 'c'), ('d', '服务成本', 'd')}
    assert ask(r, mode='hierarchy', node_id='missing')['hierarchy']['status'] == 'no_visible_seed'
    for limit in [{'max_edges': 1}, {'max_nodes': 1}]:
        partial = ask(r, mode='hierarchy', node_id='p', **limit)
        assert partial['hierarchy']['status'] == 'incomplete'
        assert len(partial['nodes']) <= limit.get('max_nodes', 80) and len(partial['edges']) <= limit.get('max_edges', 150)
    from lightgraph.retrieval.agent_context import agent_context
    assert agent_context(out)['hierarchy']['items'] == out['hierarchy']['items']
    assert 's' in {e['id'] for e in ask(r, mode='search', node_id='p', hops=1)['edges']}


def test_name_must_bind_in_compatible_context(tmp_path):
    graph = publication()
    graph['nodes'][0]['supports'] = [support('总成本', dept='M'), support('另一名称', dept='K')]
    for edge in graph['edges']:
        for obs in edge['supports']:
            obs['business_context'] = {'status': 'scoped', 'dimensions': {'dept': 'K'}}
    sync_graph_to_retrieval(graph, tmp_path)
    r = SubgraphRetriever(tmp_path)
    assert ask(r, mode='hierarchy', query='总成本')['edges'] == []
    assert len(ask(r, mode='hierarchy', query='另一名称', business_context={'dept': 'K'})['edges']) == 6


def test_each_returned_context_has_its_own_endpoint_witnesses(tmp_path):
    graph = publication()
    graph['nodes'][0]['supports'] = [support(f'总成本-{dept}', dept=dept) for dept in 'MKX']
    graph['nodes'][1]['supports'] = [support(f'成本-{dept}', dept=dept) for dept in 'MKX']
    edge = next(e for e in graph['edges'] if e['id'] == 'a')
    edge['supports'] = [support(None, dept=dept) for dept in 'MK']
    graph['edges'] = [edge]
    sync_graph_to_retrieval(graph, tmp_path)
    out = ask(SubgraphRetriever(tmp_path), mode='hierarchy', node_id='p')
    assert out['hierarchy']['items'] == [{'node_id': 'c', 'edge_ids': ['a']}]
    for item in [*out['nodes'], *out['edges']]:
        assert {obs['business_context']['dimensions']['dept'] for obs in item['observations']} == {'M', 'K'}


def test_undeclared_relation_needs_verified_column_bindings_and_current_semantics(tmp_path):
    raw = publication()
    edge = raw['edges'][2]
    extra = deepcopy(edge['supports'][0])
    extra['provenance']['source_key_columns'] = ['unrelated']
    edge['supports'].append(extra)
    sync_graph_to_retrieval(raw, tmp_path)
    r = SubgraphRetriever(tmp_path)
    before = ask(r, mode='hierarchy', node_id='p')['hierarchy']['items']
    assert 'b' not in before[0]['edge_ids']
    with sqlite3.connect(r.index.path) as db:
        db.execute('DROP TABLE kg_relation_semantics')
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        ask(r, mode='hierarchy', node_id='p')
    assert ask(r, mode='search', node_id='p')['nodes']

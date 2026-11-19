"""Open-domain relationship chains can join separate records without conflating them."""
import pytest
from pydantic import ValidationError

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.retrieval.traversal import retrieve_traversal


def catalog(*, reverse=False, other_collection=False):
    def observation(label, scope='public', collection=None, row=None):
        obs = {'scope_ref': scope, 'label': label, 'properties': {},
               'business_context': {'status': 'scoped' if collection else 'global',
                                    'dimensions': {'馆藏': collection} if collection else {}}}
        if row:
            obs['row_context'] = {'id': row, 'values': {'record': row}, 'value_types': {'record': 'text'}}
            obs['provenance'] = {'source': {'source_id': 'catalog', 'schema': 'main', 'table': 'references'},
                                 'context_columns': ['record'], 'evidence_refs': [row]}
        return obs

    nodes = [{'id': nid, 'type': kind, 'identity': {'code': nid}, 'supports': [observation(name, grant)]}
             for nid, kind, name, grant in (
                 ('a', '手稿', '折线札记', 'public'), ('b', '文献', '远潮文献', 'public'),
                 ('c', '天图', '冬夜星图', 'public'), ('x', '文献', '云图', 'public'),
                 ('hidden', '天图', '不公开的手稿', 'restricted'))]
    edges = []
    for eid, src, dst, rel, collection in [
            ('citation-1', 'a', 'b', '引述', '北馆'),
            ('citation-2', *(['c', 'b'] if reverse else ['b', 'c']), '收录', '南馆' if other_collection else '北馆'),
            ('citation-3', 'c', 'hidden', '引述', '北馆')]:
        edges.append({'id': eid, 'type': rel, 'source': src, 'target': dst,
                      'supports': [observation('', collection=collection, row=eid)]})
    return {'support_visibility_version': 1, 'definitions': {}, 'nodes': nodes, 'edges': edges}


def reader(tmp_path, *, parser=None, **options):
    sync_graph_to_retrieval(catalog(**options), tmp_path)
    return SubgraphRetriever(tmp_path, mention_extractor=parser)


def query(index, **values):
    return RetrieveSubgraphRequest.model_validate(values).run(index, ['public'])


def test_mixed_directions_and_separate_source_rows_reach_requested_type(tmp_path):
    index = reader(tmp_path, reverse=True)
    answer = query(index, mode='traverse', seed_ids=['a'], target_kind='天图', applicability={'馆藏': '北馆'})
    path, = answer['traverse']['paths']
    assert path['node_ids'] == ['a', 'b', 'c']
    assert path['observed_path'] is False and path['path_semantics'] == 'individually_supported_edges'
    assert [edge['observations'][0]['row_context']['id'] for edge in answer['edges']] == ['citation-1', 'citation-2']
    assert answer['edges'][1]['source'] == 'c' and answer['edges'][1]['target'] == 'b'
    assert 'dimensional' not in answer and 'hidden' not in str(answer)
    assert not query(index, mode='traverse', seed_ids=['a'], target_id='c', direction='outgoing')['edges']


def test_multiple_anchors_on_one_path_and_no_partial_success(tmp_path):
    index = reader(tmp_path)
    result = query(index, mode='traverse', seed_ids=['a', 'c'], direction='outgoing')
    assert result['traverse']['paths'][0]['node_ids'] == ['a', 'b', 'c']
    for options in [{'seed_ids': ['a', 'b', 'x']}, {'seed_ids': ['a', 'hidden']},
                    {'seed_ids': ['a'], 'target_id': 'c', 'relation_types': ['引述']}]:
        missing = query(index, mode='traverse', **options)
        assert missing['traverse']['status'] == 'no_compatible_path' and not missing['edges']
    short = query(index, mode='traverse', seed_ids=['a'], target_id='c', max_depth=1)
    assert short['truncated'] and short['traverse']['status'] == 'incomplete' and not short['nodes']
    assert query(index, mode='traverse', seed_ids=['c'], target_id='a', direction='incoming')['edges']
    assert not query(index, mode='traverse', seed_ids=['c'], target_id='a', direction='outgoing')['edges']


def test_scope_and_order_do_not_leak_through_shared_intermediate(tmp_path):
    index = reader(tmp_path / 'mismatch', other_collection=True)
    assert not query(index, mode='traverse', seed_ids=['a'], target_id='c')['edges']
    for selector in ['北馆', '南馆']:
        assert not query(index, mode='traverse', seed_ids=['a'], target_id='c', applicability={'馆藏': selector})['edges']
    index = reader(tmp_path / 'order')
    common = dict(seed_ids=['a'], start_id='a', target_id='c')
    assert retrieve_traversal(index, '', ['public'], via_ids=['a', 'b'], **common)['edges']
    assert not retrieve_traversal(index, '', ['public'], via_ids=['b', 'a'], **common)['edges']


def test_applicability_alias_selects_auto_and_legacy_modes_keep_contract():
    request = RetrieveSubgraphRequest.model_validate({'query': '折线札记', 'applicability': {'馆藏': '北馆'}})
    assert request.mode == 'auto' and request.model_dump()['business_context'] == {'馆藏': '北馆'}
    assert 'applicability' not in request.model_dump()
    with pytest.raises(ValidationError, match='conflicting_context_selectors'):
        RetrieveSubgraphRequest.model_validate({'query':'A', 'applicability':{'v': True}, 'business_context':{'v': 1}})
    with pytest.raises(ValidationError):
        RetrieveSubgraphRequest.model_validate({'mode': 'drilldown', 'seed_ids': ['a'], 'direction': 'both'})


@pytest.mark.parametrize('mode', ['auto', 'traverse'])
def test_query_only_nonhierarchical_mixed_edges(tmp_path, mode):
    index = reader(tmp_path, reverse=True)
    question = '从折线札记经过远潮文献到冬夜星图的关联路径'
    answer = query(index, query=question, **({} if mode == 'auto' else {'mode': mode}))
    assert answer['plan']['intent'] == 'traverse' and answer['plan']['business_context'] is None
    assert answer['traverse']['paths'][0]['node_ids'] == ['a', 'b', 'c']
    assert len(answer['edges']) == 2 and 'dimensional' not in answer

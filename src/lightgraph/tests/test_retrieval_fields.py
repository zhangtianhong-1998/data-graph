"""Field-specific ranking, scoped ID lookup, and filtered expansion."""
import json

import numpy as np
import httpx
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.sync import active_index
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.retrieval.vector_local import OpenAIEmbedder


def _scoped_graph():
    def support(label, description, project='M', scope='public'):
        return {'scope_ref': scope, 'label': label, 'description': description, 'properties': {},
                'business_context': {'status': 'scoped', 'dimensions': {'project': project}}}

    def node(id_, *supports):
        return {'id': id_, 'type': 'Item', 'identity': {'code': 'code-' + id_}, 'supports': list(supports)}

    nodes = [
        node('m-name', support('季度结余', '资产计算'), support('K绝密', 'K项目描述', 'K')),
        node('m-description', support('运营指标', '季度结余')),
        node('no-desc', support('季度结余目录', None)),
        node('k', support('季度结余', '季度结余', 'K')),
        node('hidden', support('季度结余', '季度结余', scope='private')),
    ]
    edges = [{'id': id_, 'type': rel, 'source': src, 'target': dst,
              'supports': [support('', None, project=project)]}
             for id_, rel, src, dst, project in [
                ('a', 'irrelevant', 'm-name', 'no-desc', 'M'),
                ('b', 'child', 'no-desc', 'm-name', 'M'),
                ('z', 'child', 'm-name', 'm-description', 'M'),
                ('x', 'secret', 'm-name', 'k', 'K'),
             ]]
    return {'support_visibility_version': 1, 'nodes': nodes, 'edges': edges, 'definitions': {}}


class FixtureEmbedder:
    """Fixed vectors for exercising ranking fields and batched index writes."""
    dim = 2

    def embed(self, texts):
        return np.array([[1., 0.] if text == '季度结余' else [0., 1.] for text in texts],
                        dtype=np.float32).reshape(-1, self.dim)

    def embed_queries(self, texts):
        return self.embed(texts)


@pytest.fixture
def search(tmp_path):
    embedder = FixtureEmbedder()
    sync_graph_to_retrieval(_scoped_graph(), tmp_path, embedder=embedder)
    return SubgraphRetriever(tmp_path, embedder=embedder)


@pytest.mark.parametrize('method', ['keyword', 'vector', 'hybrid'])
@pytest.mark.parametrize('field,expected', [('name', 'm-name'), ('description', 'm-description')])
def test_field_and_context_are_selected_before_ranking(search, field, method, expected):
    result = search.retrieve('季度结余', field=field, method=method, top_n=1, top_p_seeds=1, hops=0,
                             business_context={'project': 'M'})
    assert [node['id'] for node in result['nodes']] == [expected]
    hit = result['slots'][0]['candidates'][0]
    assert (hit['vec_rank'] is not None) == (method != 'keyword')
    assert (hit['lex_rank'] is not None) == (method != 'vector')
    assert 'K绝密' not in str(result)


def test_ids_and_keyword_lookup_work_without_embedding_requests(search, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('lookup called the embedding provider')
    monkeypatch.setattr(search.embedder, 'embed_queries', unexpected)
    args = {'business_context': {'project': 'M'}}
    assert search.retrieve('K项目描述', field='description', method='keyword', **args)['nodes'] == []
    assert search.retrieve('code-m-description', field='name', method='keyword', **args)['nodes'] == []
    for lookup in ('k', 'hidden', 'nonexistent'):
        assert search.retrieve(node_id=lookup, **args)['nodes'] == []
    point = search.retrieve(node_id='m-name', hops=0, **args)
    assert [n['id'] for n in point['nodes']] == ['m-name'] and point['edges'] == []
    assert point['slots'][0]['candidates'][0]['match_type'] == 'exact_id'
    assert 'K绝密' not in str(point)
    area = search.retrieve(node_id='m-name', hops=1, **args)
    assert {n['id'] for n in area['nodes']} == {'m-name', 'm-description', 'no-desc'}
    assert {e['id'] for e in area['edges']} == {'a', 'b', 'z'}


def test_description_keywords_match_separate_terms_in_reverse_order(search):
    result = search.retrieve('结余，季度', field='description', method='keyword', hops=0,
                             business_context={'project': 'M'})
    assert [node['id'] for node in result['nodes']] == ['m-description']


def test_direction_and_relation_filter_precede_neighborhood_budget(search):
    cohorts = search.authorized_cohorts(['public'], {'project': 'M'})
    for direction, expected in [('outgoing', 'z'), ('incoming', 'b')]:
        result = search.index.expand_neighborhood(
            ['m-name'], scopes=cohorts, hops=1, max_edges=1, direction=direction, relation_types=['child'])
        assert [edge['id'] for edge in result['edges']] == [expected]
        assert not result['truncated']


def test_known_target_kept_when_both_endpoints_exceed_fanout(tmp_path):
    support = {'scope_ref': 'public', 'business_context': {'status': 'global', 'dimensions': {}}}
    names = ['start', 'target', *[f'node-{i}' for i in range(60)]]
    nodes = [{'id': name, 'type': 'Item', 'identity': {'code': name},
              'supports': [{**support, 'label': name}]} for name in names]
    pairs = [('start', node) for node in names[2:]] + [(node, 'target') for node in names[2:]]
    pairs.append(('start', 'target'))
    edges = [{'id': f'e{i:03}', 'type': 'child', 'source': src, 'target': dst, 'supports': [support]}
             for i, (src, dst) in enumerate(pairs)]
    sync_graph_to_retrieval({'support_visibility_version': 1, 'nodes': nodes,
                             'edges': edges, 'definitions': {}}, tmp_path)
    reader = SubgraphRetriever(tmp_path)
    result = RetrieveSubgraphRequest(mode='drilldown', seed_ids=['start'], target_id='target',
                                     max_depth=1).run(reader, ['public'])
    assert [node['id'] for node in result['nodes']] == ['start', 'target']
    assert result['drilldown']['paths'][0]['stop_reason'] == 'target_reached'


def test_old_mixed_only_index_requires_explicit_rebuild(search, tmp_path):
    pointer = tmp_path / 'current.json'
    metadata = json.loads(pointer.read_text())
    metadata['format'] = 3
    pointer.write_text(json.dumps(metadata))
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        active_index(tmp_path)
    sync_graph_to_retrieval(_scoped_graph(), tmp_path)
    assert active_index(tmp_path)[1]['format'] == 7


def test_remote_index_allows_keyless_keyword_and_id_queries_after_sync(tmp_path, monkeypatch):
    embedder = OpenAIEmbedder(base_url='https://example.invalid/v1', model='example-model', api_key='build-key')

    def reply(url, *, json, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer build-key'
        data = [{'index': i, 'embedding': [1., 0.]} for i, text in enumerate(json['input'])]
        return httpx.Response(200, request=httpx.Request('POST', url), json={'data': data})

    monkeypatch.setattr(httpx, 'post', reply)
    sync_graph_to_retrieval(_scoped_graph(), tmp_path, embedder=embedder)
    monkeypatch.setattr(httpx, 'post', lambda *args, **kwargs: pytest.fail('unexpected network request'))
    reader = SubgraphRetriever(tmp_path, embedder=OpenAIEmbedder(base_url=embedder.base_url, model=embedder.model))
    for request in [
        {'query': '季度结余', 'field': 'description', 'method': 'keyword', 'hops': 0},
        {'node_id': 'm-description'},
        {'mode': 'drilldown', 'query': '季度结余', 'field': 'description', 'method': 'keyword'},
    ]:
        result = RetrieveSubgraphRequest(**request, business_context={'project': 'M'}).run(reader, ['public'])
        assert [node['id'] for node in result['nodes'] if node['is_seed']] == ['m-description']


def test_cli_retrieval_options_apply_same_field_and_context_filters(search, monkeypatch, capsys):
    from types import SimpleNamespace
    from lightgraph import cli

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(scopes=['public']))
    monkeypatch.setattr('lightgraph.web.construction_graph.project_retriever', lambda config, refresh: (search, None))
    assert cli.main(['retrieve-subgraph', 'example.yaml', '季度结余', '--field', 'description',
                     '--method', 'keyword', '--business-context', '{"project":"M"}', '--hops', '0']) == 0
    assert [n['id'] for n in json.loads(capsys.readouterr().out)['nodes']] == ['m-description']
    assert cli.main(['retrieve-subgraph', 'example.yaml', '--mode', 'drilldown', '--seed-id', 'm-name',
                     '--relation-type', 'child', '--business-context', '{"project":"M"}']) == 0
    assert [e['id'] for e in json.loads(capsys.readouterr().out)['edges']] == ['z']
    assert cli.main(['retrieve-subgraph', 'example.yaml', '--node-id', 'foo', '--field', 'name']) == 1

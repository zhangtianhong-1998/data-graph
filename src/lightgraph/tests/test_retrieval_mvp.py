"""Tests for local SQLite + local NumPy vector subgraph retrieval MVP (permissions, RRF, faithful bridges)."""
from fastapi.testclient import TestClient

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.web.app import create_app


def _sample_graph():
    def support(scope, table, col_cn, code, ref):
        return {
            'scope_ref': scope,
            'label': col_cn,
            'aliases': [],
            'properties': {},
            'provenance': {
                'source': {'source_id': 'gauss', 'schema': 'dm', 'table': table},
                'key_columns': ['code'],
                'label_column': 'name',
                'evidence_refs': [ref],
            },
        }

    nodes = [
        {
            'id': 'n_rev', 'type': 'Indicator', 'kind': 'Indicator', 'label': '营业收入',
            'display_label': '营业收入', 'identity': {'code': 'IND_REV'}, 'aliases': ['营收'],
            'label_origin': 'source',
            'supports': [support('public', 'income_f', '营业收入', 'IND_REV', 'e1')],
        },
        {
            'id': 'n_ord', 'type': 'Indicator', 'kind': 'Indicator', 'label': '政企订单额',
            'display_label': '政企订单额', 'identity': {'code': 'IND_ORD'}, 'aliases': ['订单'],
            'label_origin': 'source',
            'supports': [support('public', 'order_f', '政企订单额', 'IND_ORD', 'e2')],
        },
        {
            'id': 'n_cn', 'type': 'Region', 'kind': 'Region', 'label': '中国区',
            'display_label': '中国区', 'identity': {'code': 'GEO_CN'},
            'aliases': ['机密项目X'],
            'label_origin': 'source',
            'supports': [
                support('public', 'dim_geo', '中国区', 'GEO_CN', 'e3'),
                {
                    'scope_ref': 'private_dept',
                    'label': '中国区',
                    'aliases': ['机密项目X'],
                    'properties': {},
                    'provenance': {
                        'source': {'source_id': 'gauss', 'schema': 'dm', 'table': 'secret_geo'},
                        'evidence_refs': ['e99'],
                    },
                },
            ],
        },
        {
            'id': 'n_iso', 'type': 'Measure', 'kind': 'Measure', 'label': '独立孤立度量',
            'display_label': '独立孤立度量', 'identity': {'code': 'MSR_ISO'}, 'aliases': [],
            'label_origin': 'source',
            'supports': [support('public', 'other_f', '独立孤立度量', 'MSR_ISO', 'e4')],
        },
    ]
    edges = [
        {
            'id': 'e_rev_cn', 'type': 'has_dimension', 'relation': 'has_dimension',
            'source': 'n_rev', 'target': 'n_cn', 'properties': {},
            'supports': [{'scope_ref': 'public', 'provenance': {
                'source': {'source_id': 'gauss', 'schema': 'dm', 'table': 'income_f'},
                'evidence_refs': ['e10'],
            }}],
        },
        {
            'id': 'e_ord_cn', 'type': 'has_dimension', 'relation': 'has_dimension',
            'source': 'n_ord', 'target': 'n_cn', 'properties': {},
            'supports': [{'scope_ref': 'public', 'provenance': {
                'source': {'source_id': 'gauss', 'schema': 'dm', 'table': 'order_f'},
                'evidence_refs': ['e20'],
            }}],
        },
    ]
    return {'nodes': nodes, 'edges': edges, 'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []}}


def test_retrieval_enforces_pre_ranking_scope_isolation_and_faithful_bridge_overlay(tmp_path):
    graph = _sample_graph()
    rdir = tmp_path / 'retrieval'
    sync_summary = sync_graph_to_retrieval(graph, rdir)
    assert sync_summary['nodes_indexed'] == 4
    assert sync_summary['edges_indexed'] == 2

    retriever = SubgraphRetriever(rdir)

    # 1. Restricted alias MUST NOT match or influence rank under public-only scope!
    pub_secret = retriever.retrieve('机密项目X', scopes=['public'])
    assert not any(c['node_id'] == 'n_cn' and c['match_type'] is not None for s in pub_secret['slots'] for c in s['candidates'])
    assert all('机密项目X' not in str(n) for n in pub_secret['nodes'])

    # Yet querying with private_dept scope explicitly finds n_cn via exact alias!
    priv_secret = retriever.retrieve('机密项目X', scopes=['public', 'private_dept'])
    assert any(c['node_id'] == 'n_cn' and c['match_type'] == 'exact_name' for s in priv_secret['slots'] for c in s['candidates'])

    # 2. Multi-entity query ("营业收入 和 政企订单额") highlights common bridge node "中国区" without fabricating direct edge!
    res = retriever.retrieve('营业收入和政企订单额', scopes=['public'], top_p_seeds=1, hops=1)
    slot_surfaces = [s['surface'] for s in res['slots']]
    assert '营业收入' in slot_surfaces and '政企订单额' in slot_surfaces
    assert [b['node_id'] for b in res['bridge_nodes']] == ['n_cn']
    bridge = res['bridge_nodes'][0]
    assert bridge['bridge_role'] == 'intermediate_bridge'
    assert set(bridge['matched_slots']) == {'营业收入', '政企订单额'}
    # Must contain ONLY the 2 real edges, never a fake n_rev <-> n_ord edge!
    assert {(e['source'], e['target'], e['relation']) for e in res['edges']} == {
        ('n_rev', 'n_cn', 'has_dimension'),
        ('n_ord', 'n_cn', 'has_dimension'),
    }
    assert len(res['connected_components']) == 1

    # With hops=2, intermediate bridge n_cn stays ranked first among bridges.
    res2 = retriever.retrieve('营业收入和政企订单额', scopes=['public'], top_p_seeds=1, hops=2)
    assert res2['bridge_nodes'][0]['node_id'] == 'n_cn'
    assert res2['bridge_nodes'][0]['bridge_role'] == 'intermediate_bridge'

    # 3. Disconnected query entities stay faithfully disconnected across components.
    disc = retriever.retrieve('营业收入和独立孤立度量', scopes=['public'], top_p_seeds=1, hops=1)
    assert len(disc['connected_components']) >= 2


def test_retrieval_enforces_per_relation_and_hub_caps(tmp_path):
    # Create a hub node with 10 outgoing edges and check max_per_rel=3 bounds expansion cleanly.
    nodes = [
        {'id': 'n_seed', 'type': 'Indicator', 'label': '核心指标', 'identity': {'code': 'CORE'},
         'label_origin': 'source', 'supports': [{'scope_ref': 'public', 'label': '核心指标', 'provenance': {'source': {'table': 't'}}}]},
    ] + [
        {'id': f'n_leaf_{i}', 'type': 'Dimension', 'label': f'维度_{i}', 'identity': {'code': f'D{i}'},
         'label_origin': 'source', 'supports': [{'scope_ref': 'public', 'label': f'维度_{i}', 'provenance': {'source': {'table': 't'}}}]}
        for i in range(10)
    ]
    edges = [
        {'id': f'e_{i}', 'type': 'has_dimension', 'source': 'n_seed', 'target': f'n_leaf_{i}',
         'supports': [{'scope_ref': 'public', 'provenance': {'source': {'table': 't'}}}]}
        for i in range(10)
    ]
    graph = {'nodes': nodes, 'edges': edges, 'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []}}
    rdir = tmp_path / 'hub_ret'
    sync_graph_to_retrieval(graph, rdir)
    res = SubgraphRetriever(rdir).retrieve('核心指标', scopes=['public'], top_p_seeds=1, hops=1, max_per_rel=3)
    assert res['truncated'] is True
    assert 'per_relation_limit_reached' in res['truncation_reasons']
    assert len(res['edges']) == 3


def test_retrieval_web_endpoints_and_cli(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'workspace')
    with TestClient(app) as client:
        client.post('/api/projects', json={'name': 'RetProj', 'project_id': 'ret_p'})
        project_config = app.state.workspace.get_config('ret_p')
        assert project_config['embedding']['model'] == 'Qwen/Qwen3-Embedding-0.6B'
        app.state.workspace.save_config('ret_p', {**project_config, 'embedding': {'provider': 'local_hash'}})
        out_dir = app.state.workspace.project_path('ret_p') / 'output' / 'construction'
        # Save a simple published graph using ConstructionTools directly
        from lightgraph.contracts import ColumnSpec, TableSpec
        from lightgraph.construction.tools import ConstructionTools
        from lightgraph.construction.workspace import dump_yaml
        from lightgraph.sources import MemorySource
        tbl = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
        cfg = app.state.workspace.resolved_config('ret_p')
        cfg.model.mode = 'in_environment'
        with MemorySource([tbl], {tbl.qualified_name: [('IND_1', '营业收入'), ('IND_2', '净利润')]}) as src:
            tools = ConstructionTools(cfg, out_dir, adapters=[src])
            try:
                draft = {'draft_id': 'd', 'entities': [{'type': 'Indicator', 'key_names': ['code'],
                    'mappings': [{'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'label_column': 'name'}]}]}
                tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
                assert tools.call('validate_graph_draft', draft_id='d')['passed']
                assert tools.call('publish_graph_draft', draft_id='d')['status'] == 'ok'
            finally:
                tools.close()

        sync_resp = client.post('/api/projects/ret_p/retrieval/sync')
        assert sync_resp.status_code == 200 and sync_resp.json()['nodes_indexed'] == 2
        q_resp = client.post('/api/projects/ret_p/retrieval/query', json={'query': '营业收入和净利润'})
        assert q_resp.status_code == 200
        body = q_resp.json()
        assert {n['display_name'] for n in body['nodes']} == {'营业收入', '净利润'}
        route = '/api/projects/ret_p/retrieval/query'
        revenue = client.post(route, json={'query': '营业收入', 'field': 'name', 'method': 'keyword', 'hops': 0})
        assert revenue.status_code == 200
        nid, = [n['id'] for n in revenue.json()['nodes']]
        exact = client.post(route, json={'node_id': nid, 'hops': 0})
        assert exact.status_code == 200 and exact.json()['nodes'][0]['display_name'] == '营业收入'
        assert not client.post(route, json={'query': '营业收入', 'field': 'description', 'hops': 0}).json()['nodes']
        assert client.post(route, json={}).status_code == 422

        # A key is needed only when contacting an embedding service. After an
        # authorized build, local index reads work even if the key is removed.
        import httpx
        def embeddings(url, *, headers, json, **kwargs):
            assert headers['Authorization'] == 'Bearer build-only'
            data = [{'index': i, 'embedding': [1.0, 0.0]} for i, _ in enumerate(json['input'])]
            return httpx.Response(200, request=httpx.Request('POST', url), json={'data': data})
        monkeypatch.setenv('TEST_INDEX_EMBED_KEY', 'build-only')
        monkeypatch.setattr(httpx, 'post', embeddings)
        app.state.workspace.save_config('ret_p', {**app.state.workspace.get_config('ret_p'), 'embedding': {
            'provider': 'openai_compatible', 'base_url': 'https://example.invalid/v1', 'model': 'test',
            'api_key_env': 'TEST_INDEX_EMBED_KEY'}})
        client.post('/api/projects/ret_p/retrieval/sync').raise_for_status()
        monkeypatch.delenv('TEST_INDEX_EMBED_KEY')
        def offline(*args, **kwargs):
            raise AssertionError('unexpected embedding request')
        monkeypatch.setattr(httpx, 'post', offline)
        for lookup in ({'node_id': nid}, {'query': '营业收入', 'method': 'keyword', 'hops': 0}):
            response = client.post(route, json=lookup)
            assert response.status_code == 200 and [n['id'] for n in response.json()['nodes']] == [nid]


def test_edge_supports_require_every_scope_and_degree_uses_visible_edges(tmp_path):
    graph = _sample_graph()
    restricted = {
        'scope_ref': 'public', 'required_scope_refs': ['finance'],
        'properties': {'private_tag': ['budget']},
        'provenance': {'evidence_refs': ['secret_e9'], 'source': {'table': 'secret_fact'}},
    }
    graph['edges'][0]['supports'].append(restricted)
    graph['edges'].append({
        'id': 'e_private', 'source': 'n_rev', 'target': 'n_iso', 'type': 'private_relation',
        'supports': [restricted],
    })
    graph['nodes'][3]['supports'][0]['required_scope_refs'] = ['finance']
    sync_graph_to_retrieval(graph, tmp_path)
    search = SubgraphRetriever(tmp_path)
    public = search.retrieve('营业收入', scopes=['public'], top_p_seeds=1, hops=1)
    assert [edge['id'] for edge in public['edges']] == ['e_rev_cn']
    assert public['edges'][0]['evidence_refs'] == ['e10']
    assert public['edges'][0]['qualifiers'] == {}
    assert 'secret_fact' not in str(public)
    assert next(n for n in public['nodes'] if n['id'] == 'n_rev')['degree'] == 1

    both = search.retrieve('营业收入', scopes=['public', 'finance'], top_p_seeds=1, hops=1)
    assert {e['id'] for e in both['edges']} == {'e_rev_cn', 'e_private'}
    assert 'secret_e9' in next(e for e in both['edges'] if e['id'] == 'e_rev_cn')['evidence_refs']
    assert next(n for n in both['nodes'] if n['id'] == 'n_rev')['degree'] == 2
    assert not search.retrieve('营业收入', scopes=['finance'])['nodes']


def test_web_query_requires_manual_build_after_publication_and_scope_change(tmp_path):
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.sources import MemorySource
    from lightgraph.tests.test_construction_scoped_supports import fixture

    app = create_app(tmp_path / 'workspace')
    workspace = app.state.workspace
    table, rows, draft, bindings = fixture()
    with TestClient(app) as client:
        pid = 'scope_refresh'
        client.post('/api/projects', json={'name': pid, 'project_id': pid}).raise_for_status()
        endpoint = f'/api/projects/{pid}/retrieval/query'
        def query(term):
            response = client.post(endpoint, json={'query': term})
            response.raise_for_status()
            return response.json()

        config = workspace.get_config(pid)
        config.update(scopes=['A', 'B'], scope_bindings=bindings, embedding={'provider': 'local_hash'})
        workspace.save_config(pid, config)
        def assert_manual_build_required():
            for request in [{'query': 'A名称'}, {'query': 'A名称', 'method': 'keyword'}, {'node_id': 'old-id'}]:
                denied = client.post(endpoint, json=request)
                assert denied.status_code == 409 and denied.json()['error']['code'] == 'retrieval_index_stale'

        def build():
            client.post(f'/api/projects/{pid}/retrieval/sync').raise_for_status()

        assert_manual_build_required()
        run = workspace.resolved_config(pid)
        run.model.mode = 'in_environment'
        with MemorySource([table], {table.qualified_name: rows}) as source:
            tools = ConstructionTools(run, workspace.project_path(pid) / 'output' / 'construction', adapters=[source])
            try:
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
                assert tools.call('validate_graph_draft', draft_id='objects')['passed']
                assert tools.call('publish_graph_draft', draft_id='objects')['status'] == 'ok'
            finally:
                tools.close()
        assert_manual_build_required()
        build()
        assert 'B上游' in {n['display_name'] for n in query('B上游')['nodes']}
        config['scopes'] = ['A']
        workspace.save_config(pid, config)
        assert_manual_build_required()
        build()
        result = query('A名称')
        assert 'A名称' in {n['display_name'] for n in result['nodes']}
        assert all(name not in str(result) for name in ('B名称', 'B上游', 'B别名'))


def test_generation_failure_and_same_size_replacement_never_mix_graphs(tmp_path):
    import pytest
    from lightgraph.errors import LightGraphError
    from lightgraph.retrieval import LocalHashEmbedder

    graph = _sample_graph()
    before = sync_graph_to_retrieval(graph, tmp_path)['generation']
    old = SubgraphRetriever(tmp_path, expected_generation=before)

    class FailingEmbedder(LocalHashEmbedder):
        def embed(self, texts):
            raise ValueError('unavailable')

    graph['nodes'][0]['supports'][0]['label'] = '新标签'
    with pytest.raises(ValueError, match='unavailable'):
        sync_graph_to_retrieval(graph, tmp_path, embedder=FailingEmbedder())
    assert SubgraphRetriever(tmp_path, expected_generation=before).generation == before
    after = sync_graph_to_retrieval(graph, tmp_path)['generation']
    assert before != after
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        SubgraphRetriever(tmp_path, expected_generation=before)
    assert old.retrieve('营业收入', top_p_seeds=1)['slots'][0]['candidates'][0]['match_type'] == 'exact_name'
    assert SubgraphRetriever(tmp_path, expected_generation=after).retrieve('新标签', top_p_seeds=1)['slots'][0]['candidates'][0]['match_type'] == 'exact_name'


def test_subgraph_seed_limit_and_shared_candidate_without_path(tmp_path):
    sync_graph_to_retrieval(_sample_graph(), tmp_path)
    search = SubgraphRetriever(tmp_path)
    result = search.retrieve('营业收入和政企订单额', max_nodes=1, hops=0, top_p_seeds=2)
    assert len(result['nodes']) == 1
    assert not result['edges'] and not result['bridge_nodes']
    assert not result['nodes'][0]['is_bridge']
    assert result['truncated']


def test_vector_model_binding_and_repeated_sync_publish_separate_builds(tmp_path):
    import pytest
    from lightgraph.errors import LightGraphError
    from lightgraph.retrieval import LocalHashEmbedder

    graph = _sample_graph()
    initial = sync_graph_to_retrieval(graph, tmp_path)
    old_reader = SubgraphRetriever(tmp_path)

    class OtherModel(LocalHashEmbedder):
        pass

    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        SubgraphRetriever(tmp_path, embedder=OtherModel())
    rebuilt = sync_graph_to_retrieval(graph, tmp_path)
    assert initial['generation'] == rebuilt['generation']
    assert initial['build'] != rebuilt['build']
    assert len(list((tmp_path / 'builds').iterdir())) == 2
    assert old_reader.retrieve('营业收入')['nodes']


def test_edge_can_bridge_nodes_with_different_independent_scope_grants(tmp_path):
    graph = _sample_graph()
    graph['nodes'][0]['supports'][0]['scope_ref'] = 'A'
    graph['nodes'][2]['supports'] = [dict(graph['nodes'][2]['supports'][0], scope_ref='B')]
    graph['edges'] = [dict(graph['edges'][0], supports=[
        {'scope_ref': 'C', 'provenance': {'evidence_refs': ['relation-e1']}},
    ])]
    sync_graph_to_retrieval(graph, tmp_path)
    query = SubgraphRetriever(tmp_path)
    assert not query.retrieve('营业收入', scopes=['A', 'C'], top_p_seeds=1)['edges']
    result = query.retrieve('营业收入', scopes=['A', 'B', 'C'], top_p_seeds=1)
    assert result['edges'][0]['id'] == 'e_rev_cn'
    assert result['edges'][0]['evidence_refs'] == ['relation-e1']

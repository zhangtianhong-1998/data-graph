"""A path is evidence for an interpretation, including candidates outside the display shortlist."""
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest


def _fixture(tmp_path):
    nodes, edges = [], []

    def support(label, project='甲馆', scope='read'):
        return {'label': label, 'scope_ref': scope,
                'business_context': {'status': 'scoped', 'dimensions': {'project': project}}}

    def node(nid, kind, name, **kwargs):
        nodes.append({'id': nid, 'type': kind, 'identity': {'code': nid},
                      'supports': [support(name, **kwargs)]})

    def edge(one, two, project='甲馆', scope='read'):
        edges.append({'id': f'{one}-{two}', 'source': one, 'target': two, 'type': '馆藏关联',
                      'supports': [support('', project, scope)]})

    # Neither a node id nor the display position determines which sibling has catalog relationships.
    for i in range(12):
        node(f'catalog-{i:02}', f'分类{i}', '近代')
        node(f'metric-{i:02}', f'统计{i}', '馆藏数')
    node('site', '区域', '北馆')
    node('other-site', '其它目录', '北馆')
    node('A', '目录', '年代标准目录')
    node('B', '年代目录成员', '文艺复兴')
    node('wrong', '目录', '年代目录乙', project='乙馆')
    node('hidden', '目录', '年代私有', scope='private')
    edge('metric-09', 'A')
    edge('metric-10', 'B')
    edge('metric-10', 'wrong', project='乙馆')
    edge('metric-10', 'hidden', scope='private')
    edge('site', 'metric-09')
    edge('metric-09', 'catalog-09')
    edge('catalog-11', 'metric-09')
    # Shared visible labels but incompatible observations must not make one chain.
    edge('metric-09', 'catalog-10', project='乙馆')
    sync_graph_to_retrieval({'support_visibility_version': 1, 'definitions': {},
                             'nodes': nodes, 'edges': edges}, tmp_path)
    return SubgraphRetriever(tmp_path)


def test_grouping_checks_all_exact_subjects_and_keeps_disconnected_catalogs(tmp_path):
    retriever = _fixture(tmp_path)
    ans = RetrieveSubgraphRequest(query='分年代的馆藏数各是多少').run(retriever, ['read'])
    assert ans['plan']['candidate_limit']
    assert {p['subject_id'] for p in ans['grouping']['paths']} == {'metric-09', 'metric-10'}
    assert {e['id'] for e in ans['edges']} == {'metric-09-A', 'metric-10-B'}
    assert {tuple(c) for c in ans['connected_components']} == {('A', 'metric-09'), ('B', 'metric-10')}
    assert {p['match'] for p in ans['grouping']['paths']} == {'published_type', 'published_name'}
    assert all(not p['observed_path'] for p in ans['grouping']['paths'])


def test_compact_question_surfaces_complete_compatible_path_before_name_cutoff(tmp_path):
    retriever = _fixture(tmp_path)
    answer = RetrieveSubgraphRequest(query='北馆近代馆藏数').run(retriever, ['read'])
    assert answer['plan']['candidate_limit']
    paths = answer['association_paths']
    matches = [p for p in paths if len(p['node_ids']) == 3]
    assert {frozenset(p['node_ids']) for p in matches} == {
        frozenset(('site', 'catalog-09', 'metric-09')), frozenset(('site', 'catalog-11', 'metric-09'))}
    assert all(set(p['matched_terms']) == {'北馆', '近代', '馆藏数'} and not p['observed_path'] for p in matches)
    assert {e['id'] for e in answer['edges']} == {'metric-09-catalog-09', 'catalog-11-metric-09', 'site-metric-09'}
    assert 'hidden' not in str(answer)
    limited = RetrieveSubgraphRequest(query='北馆近代馆藏数', max_nodes=2, max_edges=1).run(retriever, ['read'])
    assert len(limited['nodes']) <= 2 and len(limited['edges']) <= 1
    assert all(len(p['node_ids']) <= 2 for p in limited['association_paths'])
    assert limited['truncated']


def test_large_observation_carries_complete_source_binding_or_only_its_reference():
    from lightgraph.contracts import canonical
    from lightgraph.retrieval.path_evidence import evidence_preview

    source = {'source_id': 'catalog', 'schema': 'public', 'table': 'holdings'}
    mapping = {'source': source, 'key_columns': ['item'], 'scope_columns': ['site'],
               'predicate': {'column': 'released', 'value': True}, 'property_columns': {'count': 'copies'}}
    witness = {'properties': {'long_note': '旧卷' * 2000}, 'label': '馆藏',
               'provenance': {**mapping, 'unrelated_notes': 'internal'}, 'observation_id': 'record-1'}
    preview = evidence_preview(witness)
    assert preview['projection'] == 'source_binding' and preview['provenance'] == mapping
    assert len(canonical(preview).encode()) <= 2048
    crowded = evidence_preview({**witness, 'provenance': {**mapping, 'predicate': ['限定'] * 1000}})
    assert crowded['projection'] == 'reference_only' and 'provenance' not in crowded
    assert crowded['observation_id'] == 'record-1'


def test_expensive_candidate_queries_return_a_bounded_partial_result(tmp_path, monkeypatch):
    retriever = _fixture(tmp_path)
    monkeypatch.setattr('lightgraph.retrieval.store_sqlite._ROUTE_SQL_STEPS', 2)
    for question in ('北馆近代馆藏数', '分年代的馆藏数各是多少'):
        ans = RetrieveSubgraphRequest(query=question).run(retriever, ['read'])
        assert ans['truncated'] and 'association_sql_work_limit' in ans['truncation_reasons']
        assert not ans['edges']

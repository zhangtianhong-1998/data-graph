"""Context selection filters observations before ranking, traversal and evidence."""
import json

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.sync import active_index


def _context(status, **dimensions):
    return {'version': 1, 'status': status, 'dimensions': dimensions}


def _support(label, context=None, *, source='common', **kwargs):
    result = {'scope_ref': 'public', 'label': label, 'aliases': [],
              'properties': {'observed': [source]},
              'provenance': {'source': {'table': source}, 'evidence_refs': [source + '_ref']}, **kwargs}
    if context is not None:
        result['business_context'] = context
    return result


def _graph():
    common = _context('global')
    m, k = _context('scoped', project='M'), _context('scoped', project='K')
    def node(nid, *supports):
        return {'id': nid, 'type': 'Entity', 'identity': {'code': nid}, 'supports': list(supports)}
    def edge(eid, source, target, *supports):
        return {'id': eid, 'type': 'links', 'source': source, 'target': target, 'supports': list(supports)}
    nodes = [
        node('m', _support('M起点', m, source='m_source')),
        node('common', _support('通用知识', common),
             _support('M名称', m, source='m_fact'),
             _support('K机密名称', k, source='K_SECRET', aliases=['K机密别名'])),
        node('global2', _support('通用终点', common)),
        node('k', _support('K终点', k, source='K_SECRET')),
        node('unknown', _support('范围未知', source='UNKNOWN_SECRET')),
    ]
    edges = [
        edge('m_common', 'm', 'common', _support('', m, source='m_edge')),
        edge('common_fact', 'common', 'global2',
             _support('', common), _support('', k, source='K_SECRET')),
        edge('k_bridge', 'common', 'k', _support('', k, source='K_SECRET')),
        edge('unknown_bridge', 'common', 'unknown', _support('', source='UNKNOWN_SECRET')),
        # Even a global relationship cannot make a K-only endpoint applicable.
        edge('bad_global_bridge', 'global2', 'k', _support('', common)),
    ]
    return {'support_visibility_version': 1, 'nodes': nodes, 'edges': edges,
            'definitions': {'entities': [], 'relations': []}}


def test_projection_filters_properties_sources_and_edges_without_using_permission_scope():
    graph = _graph()
    m = filter_graph_scopes(graph, ['public'], business_context={'project': 'M'})
    assert {n['id'] for n in m['nodes']} == {'m', 'common', 'global2'}
    assert {e['id'] for e in m['edges']} == {'m_common', 'common_fact'}
    common = next(n for n in m['nodes'] if n['id'] == 'common')
    assert common['properties']['observed'] == ['common', 'm_fact']
    assert 'K_SECRET' not in str(m) and 'UNKNOWN_SECRET' not in str(m)
    assert {n['id'] for n in filter_graph_scopes(graph, ['public'])['nodes']} == {
        'm', 'common', 'global2', 'k', 'unknown'}
    assert {n['id'] for n in filter_graph_scopes(graph, ['public'], business_context={})['nodes']} == {
        'common', 'global2'}
    # A scope bearing a project-like name remains only an authorization grant.
    assert not filter_graph_scopes(graph, ['M'], business_context={'project': 'M'})['nodes']
    assert graph == _graph()


def test_retrieval_does_not_rank_or_traverse_other_context_and_filters_all_evidence(tmp_path):
    sync_graph_to_retrieval(_graph(), tmp_path)
    retriever = SubgraphRetriever(tmp_path)
    m = retriever.retrieve('M起点', business_context={'project': 'M'}, top_p_seeds=1, hops=4)
    assert {n['id'] for n in m['nodes']} == {'m', 'common', 'global2'}
    assert {e['id'] for e in m['edges']} == {'m_common', 'common_fact'}
    assert next(n for n in m['nodes'] if n['id'] == 'common')['degree'] == 2
    assert next(e for e in m['edges'] if e['id'] == 'common_fact')['evidence_refs'] == ['common_ref']
    assert 'K_SECRET' not in str(m) and 'UNKNOWN_SECRET' not in str(m)
    assert m['business_context'] == {'project': 'M'}
    secret = retriever.retrieve('K机密别名', business_context={'project': 'M'}, hops=0)
    assert not any(c['match_type'] == 'exact_name' for slot in secret['slots'] for c in slot['candidates'])
    assert all('K机密' not in str(node) for node in secret['nodes'])
    k = retriever.retrieve('K终点', business_context={'project': 'K'}, top_p_seeds=1, hops=4)
    assert {n['id'] for n in k['nodes']} == {'k', 'common', 'global2'}
    assert 'K_SECRET_ref' in str(k)
    assert 'm_edge_ref' not in str(k)
    all_contexts = retriever.retrieve('M起点', top_p_seeds=1, hops=4)
    assert {n['id'] for n in all_contexts['nodes']} == {'m', 'common', 'global2', 'k', 'unknown'}


def test_query_limits_apply_after_context_selection_and_global_only_is_explicit(tmp_path):
    graph = _graph()
    # The disallowed exact lexical match must not consume the single seed slot.
    graph['nodes'][0]['supports'][0]['label'] = '指标查询 M'
    graph['nodes'][3]['supports'][0]['label'] = '指标查询'
    sync_graph_to_retrieval(graph, tmp_path)
    retriever = SubgraphRetriever(tmp_path)
    result = retriever.retrieve('指标查询', business_context={'project': 'M'}, top_n=1, top_p_seeds=1, hops=0)
    assert [n['id'] for n in result['nodes']] == ['m']
    global_only = retriever.retrieve('通用知识', business_context={}, top_p_seeds=1, hops=4)
    assert {n['id'] for n in global_only['nodes']} == {'common', 'global2'}
    assert 'm_fact' not in str(global_only) and 'K_SECRET' not in str(global_only)


def test_permission_conjunction_and_multi_dimension_context_remain_independent(tmp_path):
    graph = _graph()
    node = graph['nodes'][0]
    node['supports'][0]['business_context'] = _context('scoped', project='M', period=2025)
    node['supports'][0]['required_scope_refs'] = ['finance']
    sync_graph_to_retrieval(graph, tmp_path)
    search = SubgraphRetriever(tmp_path)
    def ids(scopes, context):
        return {n['id'] for n in search.retrieve('M起点', scopes=scopes, business_context=context, hops=0)['nodes']}
    assert 'm' not in ids(['public'], {'project': 'M', 'period': 2025})
    assert 'm' in ids(['public', 'finance'], {'project': 'M', 'period': 2025})
    assert 'm' not in ids(['public', 'finance'], {'project': 'M', 'period': 2024})
    assert 'm' not in ids(['public', 'finance'], {'project': 'M', 'period': '2025'})
    assert 'm' in ids(['public', 'finance'], {'project': 'M'})


def test_visible_candidate_diagnostics_exclude_hidden_endpoints_and_payload():
    graph = _graph()
    graph['candidate_identity_merges'] = [
        {'node_ids': ['m', 'common'], 'status': 'candidate_only', 'reason': 'same_name',
         'source_dump': 'K_SECRET'},
        {'node_ids': ['m', 'k'], 'status': 'candidate_only', 'reason': 'same_name'},
    ]
    graph['definition_conflicts'] = [
        {'node_ids': ['m', 'common'], 'edge_ids': ['m_common'], 'definition_ids': ['a', 'b'],
         'status': 'unresolved', 'reason': 'incompatible_definition_contracts', 'payload': 'K_SECRET'},
    ]
    view = filter_graph_scopes(graph, ['public'], business_context={'project': 'M'})
    assert len(view['candidate_identity_merges']) == 1
    assert len(view['definition_conflicts']) == 1
    assert 'K_SECRET' not in str(view)


def test_old_index_cannot_silently_claim_context_isolation(tmp_path):
    sync_graph_to_retrieval(_graph(), tmp_path)
    path = tmp_path / 'current.json'
    manifest = json.loads(path.read_text())
    manifest['format'] = 2
    manifest.pop('cohort_contexts')
    path.write_text(json.dumps(manifest))
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        active_index(tmp_path)


def test_adjacency_across_incompatible_months_is_not_a_path_proof(tmp_path):
    graph = _graph()
    graph['nodes'] = [node for node in graph['nodes'] if node['id'] in {'m', 'common', 'k'}]
    graph['edges'] = [edge for edge in graph['edges'] if edge['id'] in {'m_common', 'k_bridge'}]
    for item in graph['nodes'] + graph['edges']:
        if item['id'] == 'common':
            item['supports'] = [_support('通用知识', _context('global'))]
            continue
        context = _context('scoped', project='M', period='Q1' if item['id'] in {'m', 'm_common'} else 'Q2')
        item['supports'][0]['business_context'] = context
    sync_graph_to_retrieval(graph, tmp_path)
    result = SubgraphRetriever(tmp_path).retrieve('M起点和K终点', business_context={'project': 'M'}, top_p_seeds=1)
    assert len(result['nodes']) == 3 and len(result['edges']) == 2
    assert result['bridge_nodes'] == []
    assert result['connected_components_semantics'] == 'visible_fact_union_not_context_proof'
    assert {edge['observations'][0]['business_context']['dimensions']['period'] for edge in result['edges']} == {'Q1', 'Q2'}

    # The later Q1 observation provides a distinct compatible witness; Q2
    # remains readable but cannot be incorporated into that proof.
    for item in graph['nodes'] + graph['edges']:
        if item['id'] in {'k', 'k_bridge'}:
            item['supports'].append(_support('K终点' if item['id'] == 'k' else '',
                _context('scoped', project='M', period='Q1'), source='q1_witness'))
    sync_graph_to_retrieval(graph, tmp_path)
    result = SubgraphRetriever(tmp_path).retrieve('M起点和K终点', business_context={'project': 'M'}, top_p_seeds=1)
    bridge = next(node for node in result['bridge_nodes'] if node['node_id'] == 'common')
    assert bridge['context_status'] == 'compatible'
    for group in bridge['path_groups']:
        assert group['business_context']['dimensions'] == {'period': 'Q1', 'project': 'M'}
        for steps in group['paths_by_slot'].values():
            for step in steps:
                assert all(obs['business_context']['dimensions']['period'] == 'Q1' for obs in step['observations'])


def test_unresolved_relations_filter_supports_and_never_appear_as_edges():
    graph = _graph()
    graph['unresolved_relations'] = [
        {'id': 'pending-m', 'status': 'unresolved', 'reason': 'definition_endpoint_ambiguous',
         'node_ids': ['m', 'common'], 'candidate_endpoints': {'source': ['m'], 'target': ['common']},
         'original_endpoints': {'source': 'OLD_PRIVATE_ID'},
         'supports': [_support('M待对齐', _context('scoped', project='M')),
                      _support('PRIVATE_RAW', _context('scoped', project='M'), scope_ref='private')]},
        {'id': 'pending-k', 'status': 'unresolved', 'reason': 'definition_endpoint_ambiguous',
         'node_ids': ['m', 'common'], 'candidate_endpoints': {'source': ['m'], 'target': ['common']},
         'supports': [_support('K_SECRET', _context('scoped', project='K'))]},
    ]
    view = filter_graph_scopes(graph, ['public'], business_context={'project': 'M'})
    assert [item['id'] for item in view['unresolved_relations']] == ['pending-m']
    assert 'PRIVATE' not in str(view) and 'K_SECRET' not in str(view)
    pending = view['unresolved_relations'][0]
    assert len(pending['supports']) == 1 and pending['supports'][0]['label'] == 'M待对齐'
    assert pending['properties'] == {'observed': ['common']}
    assert 'pending-m' not in {edge['id'] for edge in view['edges']}


def test_index_slices_process_each_support_once_for_many_contexts(tmp_path, monkeypatch):
    import lightgraph.retrieval.sync as sync
    count = 500
    node = {'id': 'shared', 'type': 'Item', 'identity': {'code': 'shared'}, 'supports': [
        _support(f'月份{i}', _context('scoped', project='M', period=i), source=f'source_{i}')
        for i in range(count)]}
    common = {'id': 'common', 'type': 'Item', 'identity': {'code': 'common'},
              'supports': [_support('通用', _context('global'))]}
    edges = [{'id': f'edge_{i}', 'type': 'relates', 'source': 'shared', 'target': 'common',
              'supports': [_support('', _context('scoped', project='M', period=i))]} for i in range(count)]
    graph = {'nodes': [node, common], 'edges': edges, 'definitions': {}, 'support_visibility_version': 1}
    processed, endpoint_sets = [], []
    original = sync.filter_graph_scopes
    def measure(shard, *args, **kwargs):
        processed.append(sum(len(item['supports']) for name in ('nodes', 'edges') for item in shard[name]))
        endpoint_sets.append(id(kwargs['external_visible_ids']))
        return original(shard, *args, **kwargs)
    monkeypatch.setattr(sync, 'filter_graph_scopes', measure)
    summary = sync.sync_graph_to_retrieval(graph, tmp_path)
    assert sum(processed) == count * 2 + 1
    assert max(processed) == 2 and len(processed) == count + 1
    assert len(set(endpoint_sets)) == 1
    assert summary['vector_cards'] == count + 1
    result = SubgraphRetriever(tmp_path).retrieve('月份42', business_context={'project': 'M', 'period': 42}, top_p_seeds=1)
    assert {edge['id'] for edge in result['edges']} == {'edge_42'}
    shared = next(item for item in result['nodes'] if item['id'] == 'shared')
    assert shared['source_tables'] == ['public.source_42']


def test_distinct_same_source_rows_do_not_become_one_observed_path(tmp_path):
    def row_support(row_id, source='C'):
        support = _support('', _context('scoped', project='M', period='Q1'))
        support.update(row_context={'id': row_id, 'values': {'route': row_id}},
            provenance={'source': {'source_id': source, 'schema': 'public', 'table': 'paths'},
                        'context_columns': ['route'], 'evidence_refs': [row_id]},
            execution_origins=[{'contributor_id': 'extract_A'}, {'contributor_id': 'extract_B'}],
            lookup_matches=[{'endpoint': 'target', 'source': {'source_id': 'catalog', 'table': 'items'},
                             'row_context': {'id': 'lookup_1', 'values': {'code': 'hub'}}}],
            endpoint_origins={'target': 'catalog_item'}, identity_origins=[{'identity': {'code': 'hub'}}])
        return support
    graph = {'support_visibility_version': 1, 'definitions': {}, 'nodes': [
        {'id': nid, 'type': 'Entity', 'identity': {'code': nid},
         'supports': [_support(label, _context('global'))]}
        for nid, label in [('a', '甲端'), ('hub', '公共节点'), ('b', '乙端')]],
        'edges': [
            {'id': 'left', 'type': 'links', 'source': 'a', 'target': 'hub', 'supports': [row_support('row_1')]},
            {'id': 'right', 'type': 'links', 'source': 'hub', 'target': 'b', 'supports': [row_support('row_2')]},
        ]}
    def retrieve():
        sync_graph_to_retrieval(graph, tmp_path)
        return SubgraphRetriever(tmp_path).retrieve('甲端和乙端', business_context={'project': 'M'}, top_p_seeds=1)
    incompatible = retrieve()
    assert len(incompatible['edges']) == 2 and len(incompatible['connected_components']) == 1
    assert incompatible['bridge_nodes'] == []
    observed = incompatible['edges'][0]['observations'][0]
    assert observed['execution_origins'] == [{'contributor_id': 'extract_A'}, {'contributor_id': 'extract_B'}]
    assert observed['lookup_matches'][0]['row_context']['id'] == 'lookup_1'
    assert observed['endpoint_origins'] == {'target': 'catalog_item'}
    assert observed['identity_origins'] == [{'identity': {'code': 'hub'}}]

    # Independent tables can form a conditional chain, but that is explicitly
    # not a claim that an entire path was observed in one physical source row.
    graph['edges'][1]['supports'] = [row_support('row_2', source='D')]
    across_tables = retrieve()
    bridge = next(item for item in across_tables['bridge_nodes'] if item['node_id'] == 'hub')
    assert bridge['observed_path'] is False
    assert bridge['path_semantics'] == 'condition_compatible_evidence_chain'
    assert all(group['observed_path'] is False for group in bridge['path_groups'])

    # Two declarations with different row grains are not silently equated.
    other_grain = row_support('other_row')
    other_grain['provenance']['context_columns'] = ['other_route']
    other_grain['row_context']['values'] = {'other_route': 'other_row'}
    graph['edges'][1]['supports'] = [other_grain]
    assert any(item['node_id'] == 'hub' for item in retrieve()['bridge_nodes'])

    # A matching row witness remains a separate usable alternative; adding it
    # cannot convert the incompatible row_2 witness into that same path.
    graph['edges'][1]['supports'] = [row_support('row_2'), row_support('row_1')]
    same_row = retrieve()
    bridge = next(item for item in same_row['bridge_nodes'] if item['node_id'] == 'hub')
    for group in bridge['path_groups']:
        assert set(group['row_witnesses'].values()) == {'row_1'}
        for path in group['paths_by_slot'].values():
            for step in path:
                assert {item['row_context']['id'] for item in step['observations']} == {'row_1'}


def test_large_observations_and_alternate_paths_have_a_query_wide_witness_budget(tmp_path):
    payload = 'large-source-evidence-' * 1500
    nodes = [{'id': nid, 'type': 'Entity', 'identity': {'code': nid},
              'supports': [_support(label, _context('global'))]}
             for nid, label in [('a', '甲目标'), ('b', '乙目标'),
                                *[(f'hub{i}', f'中间节点{i}') for i in range(12)]]]
    edges = []
    for index in range(12):
        for source, target in [('a', f'hub{index}'), (f'hub{index}', 'b')]:
            support = _support('', _context('scoped', project='M'),
                               properties={'original_evidence': [payload]})
            edges.append({'id': f'{source}_{target}', 'type': 'links',
                          'source': source, 'target': target, 'supports': [support]})
    graph = {'nodes': nodes, 'edges': edges, 'definitions': {}, 'support_visibility_version': 1}
    sync_graph_to_retrieval(graph, tmp_path)
    result = SubgraphRetriever(tmp_path).retrieve('甲目标和乙目标', business_context={'project': 'M'},
        top_p_seeds=1, hops=2, max_per_rel=30)
    assert result['truncated'] and 'path_budget_reached' in result['truncation_reasons']
    assert len(json.dumps(result['bridge_nodes'], ensure_ascii=False, separators=(',', ':')).encode()) <= 512 * 1024
    assert len(result['edges']) == len(edges)
    assert all(edge['observations'][0]['properties']['original_evidence'] == [payload] for edge in result['edges'])

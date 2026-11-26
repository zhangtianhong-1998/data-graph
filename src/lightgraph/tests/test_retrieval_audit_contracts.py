"""Published semantics, selected applicability and partial answers at the request boundary."""

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.agent_context import agent_context
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.sources import MemorySource
from lightgraph.tests.test_dimensional_retrieval import graph
from lightgraph.tests.test_retrieval_identity_and_row_proofs import entity, index, query, relation, table


@pytest.mark.parametrize('selected', [{}, {'project': 'K'}])
def test_question_cannot_replace_explicit_scope(tmp_path, selected):
    sync_graph_to_retrieval(graph(), tmp_path)
    reader = SubgraphRetriever(tmp_path)
    answer = query(reader, query='M 项目中查询华东的销售收入', business_context=selected)
    assert answer['plan']['business_context'] == selected
    assert answer['plan']['scope_unresolved'] and answer['plan']['intent'] == 'clarify'
    assert answer['nodes'] == [] and answer['edges'] == []
    assert agent_context(answer)['bindings'] == []


def test_default_query_recalls_one_character_codes_and_preserves_partial_citations(tmp_path):
    data = graph()
    data['nodes'][0]['identity'] = {'code': '7'}
    scope = {'project': 'M'}
    east = next(n for n in data['nodes'] if n['id'] == 'east')['supports'][0]
    east.update(observation_id='obs_east', properties={'制度条款': ['发生晶罅时需留存单据']})
    sync_graph_to_retrieval(data, tmp_path)
    reader = SubgraphRetriever(tmp_path)
    for code, nid in [('E', 'east'), ('7', 'fact')]:
        hit = query(reader, query=code, business_context=scope)
        assert hit['plan']['conditions'][0]['candidates'][0]['node_id'] == nid
        assert nid in [node['id'] for node in hit['nodes']]
    partial = query(reader, query='华东的未见项', business_context=scope)
    compact = agent_context(partial)
    assert partial['nodes'] == [] and compact['unbound_mentions']
    assert any(r['ref'] == 'east' and r['source']['table'] == 'income' and r['observation_id'] == 'obs_east'
               for r in compact['bindings'])
    passage = agent_context(query(reader, query='晶罅', business_context=scope))['source_passages'][0]
    assert passage['observation_id'] == 'obs_east' and passage['property'] == '制度条款'


def test_ranked_source_labels_are_selected_only_across_visible_cohorts(tmp_path):
    item = {'id': 'a', 'type': 'Item', 'identity': {'code': '1'}, 'supports': [
        {'scope_ref': 'public', 'label': 'A', 'label_priority': 5},
        {'scope_ref': 'public', 'label': 'Z', 'label_priority': 1},
        {'scope_ref': 'finance', 'label': 'C', 'label_priority': 0}]}
    graph = {'support_visibility_version': 1, 'nodes': [item], 'edges': []}
    sync_graph_to_retrieval(graph, tmp_path)
    reader = SubgraphRetriever(tmp_path)
    for scopes, winner in [(['public'], 'Z'), (['finance','public'], 'C')]:
        expected = filter_graph_scopes(graph, scopes)['nodes'][0]['display_label']
        assert expected == winner
        cohorts = reader.authorized_cohorts(scopes, None)
        assert reader.index.authorized_labels(['a'], scopes=cohorts) == {'a': winner}
        answer = RetrieveSubgraphRequest(query='A').run(reader, scopes)
        assert answer['nodes'][0]['display_name'] == expected
        assert agent_context(answer)['nodes'][0]['display_name'] == expected


def test_published_type_text_is_cited_distinctly_from_instance_and_relation_observations(tmp_path):
    material = table('billing', ['id', 'name', 'unit'])
    mapping = {'source_id': 's', 'table': 'billing', 'business_context': {'status': 'global'}}
    definition = entity('指标', mapping, ['id'], label='name')
    definition['description'] = '仅计已开票且未冲销的净额'
    rel = relation('计量于', '指标', '单位', mapping, ['id'], ['unit'])
    rel['description'] = '使用经批准的计量尺度'
    draft = {'draft_id':'bill', 'entities': [definition, entity('单位', mapping, ['unit'])], 'relations':[rel]}
    src = MemorySource([material], {material.qualified_name: [('A1','销售收入','元')]})
    graph, reader = index(tmp_path, src, draft)
    anchor = next(n['id'] for n in graph['nodes'] if n['type']=='指标')
    for request, expected in [({'query':'销售收入'}, definition), ({'query':definition['description']}, definition),
                              ({'node_id':anchor,'hops':1}, rel), ({'query':rel['description']}, rel)]:
        response = query(reader, **request)
        found = next(d for d in agent_context(response)['type_definitions'] if d['type'] == expected['type'])
        assert found['description'] == expected['description']
        assert found['source'] == 'published_type_definition'
        assert found['example_binding']['source']['table'] == 'billing'
        assert found['example_binding']['business_context']['status'] == 'global'
        if request.get('query') in {definition['description'], rel['description']}:
            assert found['text_match']
        assert 'type_definitions' not in RetrieveSubgraphRequest.model_validate(request).run(reader, ['finance'])


def test_definition_of_relation_needs_two_visible_endpoints(tmp_path):
    data = graph()
    data['definitions']['relations'] = [{'type':'observes','description':'隐去终点时不可见'}]
    next(n for n in data['nodes'] if n['id']=='rev')['supports'][0]['required_scope_refs'] = ['finance']
    sync_graph_to_retrieval(data, tmp_path)
    reader = SubgraphRetriever(tmp_path)
    req = RetrieveSubgraphRequest(query='隐去终点时不可见')
    assert 'type_definitions' not in req.run(reader, ['public'])
    response = req.run(reader, ['public','finance'])
    assert response['type_definitions'][0]['example_binding']['edge_id'] == 'fact-rev'


def test_bridge_budget_counts_incompatible_context_pairs(monkeypatch):
    from lightgraph.retrieval import retriever
    count = 0
    join = retriever._join_contexts
    def counted(left, right):
        nonlocal count
        count += 1
        return join(left, right)
    monkeypatch.setattr(retriever, '_PATH_STATE_LIMIT', 48)
    monkeypatch.setattr(retriever, '_join_contexts', counted)
    def observations(offset):
        return [{'business_context':{'status':'scoped', 'dimensions':{'period':i}}}
                for i in range(offset, offset + 16)]
    nodes = {'a': {'observations':[{'business_context':{'status':'global'}}]}, 'b': {'observations':observations(16)}}
    edges = {'a':[{'id':'e', 'source':'a', 'target':'b', 'relation':'r', 'observations':observations(0)}]}
    paths, _, truncated = retriever._context_paths(nodes, edges, {'start':['a']}, 1)
    assert 0 < count <= 48 and truncated
    assert 'b' not in paths['start']


def test_old_index_without_definition_and_priority_columns_still_reads(tmp_path):
    import sqlite3
    from lightgraph.retrieval.sync import active_index

    data = graph()
    sync_graph_to_retrieval(data, tmp_path)
    build, _ = active_index(tmp_path)
    # Recreate the schema deployed before ranking and definition metadata.
    with sqlite3.connect(build / 'retrieval.sqlite') as db:
        db.execute('ALTER TABLE kg_node_scope_views DROP COLUMN label_priority')
        db.execute('DROP TABLE kg_type_definitions')
        db.execute("UPDATE kg_node_scope_views SET chinese_name='旧索引的次名' WHERE node_id='east'")
    reader = SubgraphRetriever(tmp_path)
    answer = query(reader, node_id='east', hops=0)
    assert answer['nodes'][0]['display_name'] == '华东'
    assert 'type_definitions' not in answer


def test_partial_authorized_type_cannot_publish_unattributed_description(tmp_path):
    data = graph()
    text = '保密目录中的口径'
    data['definitions']['entities'] = [{'type':'指标', 'description':text}]
    next(n for n in data['nodes'] if n['id']=='k')['supports'][0]['required_scope_refs'] = ['finance']
    public = filter_graph_scopes(data, ['public'])
    assert not public['definitions']['entities'][0].get('description')
    sync_graph_to_retrieval(data, tmp_path)
    req = RetrieveSubgraphRequest(query=text)
    reader = SubgraphRetriever(tmp_path)
    assert 'type_definitions' not in req.run(reader, ['public'])
    assert req.run(reader, ['public','finance'])['type_definitions'][0]['description'] == text


def test_model_scope_respects_explicit_global_selection(tmp_path):
    sync_graph_to_retrieval(graph(), tmp_path)
    def parse(question):
        return {'intent': 'search', 'mentions': ['华东'],
                'scopes': [{'text': 'M 项目', 'dimension':'项目', 'value':'M'}]}
    reader = SubgraphRetriever(tmp_path, mention_extractor=parse)
    answer = query(reader, query='M 项目的华东', business_context={})
    assert answer['plan']['business_context'] == {} and answer['plan']['scope_unresolved']
    assert not answer['nodes'] and agent_context(answer)['bindings'] == []

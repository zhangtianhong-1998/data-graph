"""Follow complementary records from independent branches through publication-style fusion and lookup."""
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest


def contribution(unit, entity, table, key, field, value, alias):
    support = {'scope_ref': 'public', 'label': '云专线', 'aliases': [alias], 'properties': {field: [value]},
               'provenance': {'source': {'source_id': 'sales', 'schema': 'dm', 'table': table},
                              'key_columns': [key], 'label_column': 'name', 'alias_columns': ['nickname'],
                              'property_columns': {field: field}}}
    graph = {'status': 'published', 'support_visibility_version': 1,
             'definitions': {'entities': [{'type': entity, 'key_names': [key]}], 'relations': []},
             'nodes': [{'id': unit + '-P01', 'type': entity, 'identity': {key: 'P01'},
                        'identity_types': {key: 'text'}, 'label': support['label'],
                        'properties': {field: [value]}, 'supports': [support]}],
             'edges': []}
    return {'kind': 'unit', 'contributor_id': unit, 'graph': graph}


def test_two_branch_aliases_return_one_object_only_with_confirmed_key_mapping(tmp_path):
    branches = [
        contribution('a', 'OrderProduct', 'orders', 'sku', 'unit', '条', '专线服务'),
        contribution('b', 'CatalogItem', 'catalog', 'item_code', 'category', '通信', '网络专线'),
    ]
    rules = [{'entity_type': entity, 'canonical_type': 'Product', 'identity_domain': 'product_master',
              'key_mapping': {'code': key}, 'source_bindings': [
                  {'source_id': 'sales', 'schema': 'dm', 'table': table, 'key_columns': {key: key}}]}
             for entity, key, table in [('OrderProduct', 'sku', 'orders'), ('CatalogItem', 'item_code', 'catalog')]]
    for directory, policy, count in [('unconfirmed', {}, 2), ('confirmed', {'entity_rules': rules}, 1)]:
        graph = merge_construction_graphs(branches, merge_policy={'fusion_policy': policy})
        assert len(graph['nodes']) == count
        index = tmp_path / directory
        sync_graph_to_retrieval(graph, index)
        reader = SubgraphRetriever(index)

        def lookup(term):
            answer = RetrieveSubgraphRequest(mode='search', query=term, hops=0, method='keyword').run(reader, ['public'])
            return {c['node_id'] for slot in answer['slots'] for c in slot['candidates']}

        assert lookup('P01') == {n['id'] for n in graph['nodes']}
        if count == 2:
            by_kind = {n['type']: n['id'] for n in graph['nodes']}
            assert lookup('专线服务') == {by_kind['OrderProduct']}
            assert lookup('网络专线') == {by_kind['CatalogItem']}
            continue
        node, = graph['nodes']
        assert lookup('专线服务') == lookup('网络专线') == {node['id']}
        assert node['properties'] == {'unit': ['条'], 'category': ['通信']}
        assert {(s['provenance']['source']['table'], tuple(s['provenance']['property_columns'].values()))
                for s in node['supports']} == {('orders', ('unit',)), ('catalog', ('category',))}
        reversed_graph = merge_construction_graphs(list(reversed(branches)), merge_policy={'fusion_policy': policy})
        assert reversed_graph['nodes'] == graph['nodes']

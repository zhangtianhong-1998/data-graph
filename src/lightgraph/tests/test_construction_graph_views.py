"""Published YAML is readable through the same bounded workbench contract."""
from types import SimpleNamespace

import pytest

from lightgraph.web import construction_graph as views


def graph():
    nodes = [{"id": str(i), "kind": "设备", "type": "设备", "label": f"设备{i}",
              "properties": {"产地": ["上海"]}, "aliases": [f"machine{i}"],
              "provenance": [{"source_id": "local", "table": "设备档案", "definition": "设备"}]}
             for i in range(4)]
    edges = [{"id": f"e{i}", "source": str(i), "target": str(i + 1),
              "relation": "连接", "type": "连接", "properties": {}, "provenance": []}
             for i in range(3)]
    return {"nodes": nodes, "edges": edges, "definitions": {
        "entities": [{"type": "设备", "description": "设备档案"}],
        "relations": [{"type": "连接", "description": "实际连接"}]}}


@pytest.fixture
def data(monkeypatch):
    value = graph()
    monkeypatch.setattr(views, "load_graph", lambda config: value)
    return value


def test_pages_omit_support_payloads(data):
    data['nodes'][0]['supports'] = [{'scope_ref': 'public', 'provenance': {'secret': 'HIDDEN-SUPPORT'}}]
    data['nodes'][0]['provenance'] = [{'secret': 'HIDDEN-ORIGIN'}]
    data['edges'][0]['supports'] = [{'scope_ref': 'public', 'provenance': {'secret': 'HIDDEN-EDGE'}}]
    result = views.graph_page(None, limit=10)
    assert 'supports' not in result['nodes'][0] and 'provenance' not in result['nodes'][0]
    assert 'supports' not in result['edges'][0] and 'provenance' not in result['edges'][0]
    assert 'HIDDEN-SUPPORT' not in str(result) and 'HIDDEN-ORIGIN' not in str(result) and 'HIDDEN-EDGE' not in str(result)
    assert result['nodes'][0]['label'] == '设备0'


def test_two_hop_view_preserves_paths_and_reports_excluded_neighbor(data):
    result = views.graph_page(None, focus="0", hops=2, limit=100)
    assert [n["id"] for n in result["nodes"]] == ["0", "1", "2"]
    assert [e["id"] for e in result["edges"]] == ["e0", "e1"]
    assert result["boundary_edges"] == 1
    assert result["focus_neighbor_count"] == 2
    assert result["semantic_origin"] == "agent_authored"


def test_dense_graph_edge_limit_is_explicit_even_when_nodes_fit(data):
    data["edges"] = [{**data["edges"][0], "id": f"edge{i}"} for i in range(3100)]
    result = views.graph_page(None, focus="0", limit=100)
    assert len(result["edges"]) == 3000
    assert result["matched_edges"] == 3100
    assert result["total_edges"] == 3100
    assert result["edges_truncated"] and result["neighborhood_truncated"] and result["truncated"]


def test_every_displayed_fact_can_be_opened_by_its_id(data):
    config = SimpleNamespace(isolation_domain="default")
    result = views.graph_details(config, entity_id="0")
    assert {f["fact_kind"] for f in result["facts"]} == {"NodeExistence", "PropertyValue", "Edge"}
    for fact in result["facts"]:
        detail = views.graph_details(config, fact_id=fact["id"])
        assert detail["facts"] == [fact]
        assert detail["nodes"][0]["label"] == "设备0"
        assert all(s["fact_id"] == fact["id"] for s in detail["supports"])
    assert result["display_schema"]["relations"][0]["description"] == "实际连接"


def test_missing_fact_is_not_an_empty_success(data):
    from lightgraph.errors import LightGraphError
    with pytest.raises(LightGraphError, match="graph_item_not_found"):
        views.graph_details(SimpleNamespace(isolation_domain="default"), fact_id="missing")


def test_search_includes_identity_and_aliases_but_not_provenance(data):
    data['nodes'][0].update(identity={'机构': '华东分部', '设备编号': 'EQ-102'},
                            aliases=['Assembly Robot'], display_label='装配机器人')
    data['nodes'][0]['provenance'][0]['internal_marker'] = 'PRIVATE-MARKER'
    for query in ('华东分部', 'eq-102', 'assembly robot', '装配机器人'):
        assert [node['id'] for node in views.graph_page(None, q=query)['nodes']] == ['0']
    assert views.graph_page(None, q='PRIVATE-MARKER')['matched_nodes'] == 0


def test_details_explain_actual_visible_mapping_without_inventing_revision(data):
    origin = {'definition': '设备', 'mapping': 2,
              'source': {'source_id': 'factory', 'schema': 'public', 'table': 'equipment'},
              'columns': ['org', 'code', 'name'], 'key_columns': ['org', 'code'],
              'scope_columns': ['org'], 'label_column': 'name', 'scope_ref': 'east'}
    data['nodes'][0]['provenance'] = [origin]
    result = views.graph_details(None, entity_id='0')
    support = next(s for s in result['supports'] if s['fact_id'] == '0')
    assert support['status'] == 'accepted' and support['origin'] == 'source_query'
    assert support['id'] == support['support_id']
    assert support['source_binding'] == 'factory.public.equipment'
    assert 'rule_revision' not in support
    mapping = result['rules'][0]
    assert mapping['identity_columns'] == ['org', 'code']
    assert mapping['label_column'] == 'name'
    assert mapping['source']['table'] == 'equipment'
    assert mapping['description'] == '设备档案'


def test_focused_search_does_not_filter_neighbors_and_keeps_center_first(data):
    data['nodes'] = [{'id': str(i), 'kind': '维度', 'type': '维度', 'label': '邻居'} for i in range(5)] + [
        {'id': 'center', 'kind': '指标', 'type': '指标', 'label': '目标', 'identity': {'编码': 'EBG_PL_92200'}}]
    data['edges'] = [{'id': 'e'+str(i), 'source': 'center', 'target': str(i), 'relation': '关联'} for i in range(5)]
    result = views.graph_page(None, q='EBG_PL_92200', kind='指标', focus='center', hops=2, limit=100)
    assert len(result['nodes']) == 6 and len(result['edges']) == 5
    assert result['nodes'][0]['id'] == 'center'
    assert result['focus_filters_ignored']
    assert result['focus_total_edges'] == result['focus_loaded_edges'] == 5
    assert not result['neighborhood_truncated']


def test_focus_batches_keep_center_and_paths_to_second_hop(data):
    visited, cursor = set(), 0
    while cursor is not None:
        result = views.graph_page(None, focus='1', hops=2, offset=cursor, limit=3)
        ids = {n['id'] for n in result['nodes']}
        assert result['nodes'][0]['id'] == '1'
        assert len(ids) <= 3
        reached = {'1'}
        for _ in range(2):
            reached |= {endpoint for e in result['edges'] if e['source'] in reached or e['target'] in reached
                        for endpoint in (e['source'], e['target'])}
        assert ids == reached
        visited |= ids
        following = result['next_offset']
        assert following is None or following > cursor
        cursor = following
    assert visited == {'0', '1', '2', '3'}


def test_edge_cap_preserves_connecting_edges_and_reports_missing_edges(data):
    data['edges'] = [{**data['edges'][0], 'id': f'parallel{i}'} for i in range(3100)] + [
        {'id': 'late', 'source': '0', 'target': '2', 'relation': '连接'}]
    result = views.graph_page(None, focus='0', hops=1, limit=10)
    assert len(result['edges']) == 3000
    assert any(edge['id'] == 'late' for edge in result['edges'])
    assert result['focus_total_edges'] == 3101 and result['focus_loaded_edges'] == 3000
    assert result['edges_truncated'] and result['neighborhood_truncated']


def test_scope_filtered_nodes_never_reappear_during_focus(data):
    # load_graph has already applied authorization; hidden endpoints cannot be
    # supplied by search, kind overrides, or the neighborhood traversal.
    data['nodes'] = data['nodes'][:2]
    data['edges'] = data['edges'][:1]
    result = views.graph_page(None, focus='0', q='设备3', kind='其他', hops=2)
    assert {node['id'] for node in result['nodes']} == {'0', '1'}
    from lightgraph.errors import LightGraphError
    with pytest.raises(LightGraphError, match='graph_item_not_found'):
        views.graph_page(None, focus='3', hops=2)


def test_details_keep_same_row_context_and_mapping_position(data):
    origin = {'definition': '设备', 'mapping': 0, 'source': {'table': '设备档案'},
              'constant_properties': {'position': 'slot_b'}, 'context_columns': ['path_id', 'code']}
    data['nodes'][0]['supports'] = [{'scope_ref': 'public', 'provenance': origin,
        'row_context': {'id': 'row-1', 'values': {'path_id': 'p1', 'code': 'A'}},
        'properties': {}, 'label': '设备0', 'aliases': []}]
    result = views.graph_details(None, entity_id='0')
    support = next(s for s in result['supports'] if s['fact_id'] == '0')
    assert support['binding']['row_context']['values']['path_id'] == 'p1'
    assert result['rules'][0]['constant_properties'] == {'position': 'slot_b'}
    assert result['rules'][0]['context_columns'] == ['path_id', 'code']
    assert result['total_incident_edges'] == 1

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

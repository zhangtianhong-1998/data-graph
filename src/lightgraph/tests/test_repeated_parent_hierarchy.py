"""Repeated parents and trailing fills must not mix labels across path positions.

Anonymous topology-equivalent data exercises the reported nine-row/five-slot case;
no customer source rows or labels are retained in this fixture.
"""
from copy import deepcopy

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.discovery.engine import _column_id, _compile_model, _source
from lightgraph.discovery.hierarchy_plan import compile_hierarchy_plan
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


PATHS = [
    ("A", "A", "A", "A", "A"),
    ("B", "C", "C", "C", "C"),
    ("B", "D", "D", "D", "D"),
    ("B", "E", "F", "F", "F"),
    ("B", "E", "G", "G", "G"),
    ("B", "E", "H", "H", "H"),
    ("B", "I", "J", "K", "K"),
    ("B", "I", "J", "L", "L"),
    ("B", "I", "M", "M", "M"),
]


def hierarchy_fixture():
    columns = ["responsibility_center"] + [
        f"REPORT_ITEM_L{level}_{suffix}"
        for level in range(1, 6) for suffix in ("CODE", "CN_NAME", "EN_NAME")
    ]
    table = TableSpec(source_id="s", name="report_items",
        columns=[ColumnSpec(name=column) for column in columns])
    ids = {column: _column_id(table, column) for column in columns}
    packet = {"task": "T0_discovery", "scope": _source(table).model_dump(by_alias=True),
        "metadata_digest": {"columns": [{"name": name, "id": value} for name, value in ids.items()]},
        "probe_digest": [{"probe_id": "probe_structure"}]}
    plan = {"plan_id": "items", "entity_type": "ReportItem", "identity_scope": "source",
        "namespace_columns": [ids["responsibility_center"]],
        "positions": [{"position": level, "code": ids[f"REPORT_ITEM_L{level}_CODE"],
            "label": ids[f"REPORT_ITEM_L{level}_CN_NAME"],
            "aliases": [ids[f"REPORT_ITEM_L{level}_EN_NAME"]]} for level in range(1, 6)],
        "position_meaning": "relative_ordinal", "allow_multiple_parents": False,
        "repeat_is_fill": True, "relation_type": "parent_of", "evidence": ["probe_structure"]}
    rows = []
    for path in PATHS:
        row = {"responsibility_center": "scope_a"}
        for level, code in enumerate(path, 1):
            row.update({f"REPORT_ITEM_L{level}_CODE": code,
                f"REPORT_ITEM_L{level}_CN_NAME": "项目" + code,
                f"REPORT_ITEM_L{level}_EN_NAME": "Item " + code})
        rows.append(row)
    # The review is a fixture input, not an assertion that automatic discovery
    # or a real semantic reviewer has accepted customer data.
    batch = compile_hierarchy_plan(plan, packet)
    rule = _compile_model(batch, table, ExtractionGoal(), [], [],
        semantic_evidence=["semantic_review:data_inference", "probe_structure"])[0]
    return table, rule, rows


def execute(table, rule, rows):
    with MemorySource([table], {table.qualified_name: rows}) as source, GraphStore() as store:
        outcome = RuleEngine(store).execute(source, rule)
        graph = store.view({"scope_a"})
    return outcome, graph


def test_repeated_parent_and_trailing_fill_keep_local_labels_and_all_positions():
    table, rule, rows = hierarchy_fixture()
    assert [(level.code, level.label, level.aliases) for level in rule.hierarchy.levels] == [
        (f"REPORT_ITEM_L{level}_CODE", f"REPORT_ITEM_L{level}_CN_NAME", [f"REPORT_ITEM_L{level}_EN_NAME"])
        for level in range(1, 6)]
    outcome, graph = execute(table, rule, rows)
    assert outcome.published, outcome.codes
    assert not outcome.codes
    nodes = {node["properties"]["business_code"][0]: node for node in graph["nodes"]}
    assert len(nodes) == 13
    assert all(node["label"] == "项目" + code and node["aliases"] == ["Item " + code]
        for code, node in nodes.items())
    code_for_id = {node["id"]: code for code, node in nodes.items()}
    pairs = {(code_for_id[edge["source"]], code_for_id[edge["target"]]) for edge in graph["edges"]}
    assert len(graph["edges"]) == 11
    assert pairs == {("B", "C"), ("B", "D"), ("B", "E"), ("B", "I"),
        ("E", "F"), ("E", "G"), ("E", "H"), ("I", "J"), ("I", "M"),
        ("J", "K"), ("J", "L")}
    assert all(source != target for source, target in pairs)
    positions = [fact for fact in graph["facts"] if fact["fact_kind"] == "PropertyValue"
        and fact["payload"]["name"] == "source_position"]
    assert len(positions) == 33
    assert {fact["payload"]["value"] for fact in positions if fact["payload"]["entity_id"] == nodes["A"]["id"]} == {1, 2, 3, 4, 5}
    # Different physical columns independently support the same A entity.
    position_ids = {fact["id"] for fact in positions if fact["payload"]["entity_id"] == nodes["A"]["id"]}
    assert len({support["source_binding"] for support in graph["supports"]
        if support["fact_id"] in position_ids}) == 5


def test_actual_same_code_name_change_still_blocks_publication():
    table, rule, rows = hierarchy_fixture()
    rows = deepcopy(rows)
    rows[2]["REPORT_ITEM_L1_CN_NAME"] = "另一名称"
    outcome, graph = execute(table, rule, rows)
    assert not outcome.published
    assert "same_code_diff_label" in outcome.codes
    assert not graph["nodes"] and not graph["edges"]


def test_wrong_parent_code_to_child_label_mapping_reproduces_reported_conflict():
    table, rule, rows = hierarchy_fixture()
    rule.hierarchy.levels[0].label = "REPORT_ITEM_L2_CN_NAME"
    outcome, graph = execute(table, rule, rows)
    assert not outcome.published
    assert "same_code_diff_label" in outcome.codes
    assert not graph["nodes"] and not graph["edges"]

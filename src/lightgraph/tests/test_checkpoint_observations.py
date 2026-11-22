"""The checkpoint audit reports observed overlap without conferring shared identity."""
from pathlib import Path
import json
import runpy


review = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_checkpoint_merge.py"))[
    "typed_row_identity_review"]


def row(unit, table, value, label, *, typ="member"):
    return {"type": typ, "identity": {"code": value}, "identity_types": {"code": "text"},
            "supports": [{"contributor_id": unit, "label": label, "scope_ref": "public",
                          "business_context": {"status": "scoped", "dimensions": {"project": unit}},
                          "provenance": {"source": {"source_id": "store", "schema": "public", "table": table},
                                         "key_columns": ["physical_key"], "label_column": "source_label"}}]}


def test_committed_evidence_does_not_require_constraints_or_emitted_candidates():
    nodes = [row("left", "a", "confidential-code", "left-only-label"),
             row("right", "b", "confidential-code", "right-only-label"),
             row("right", "b", "confidential-code", "left-only-label", typ="other_type"),
             row("left", "a", "second", "second-label"), row("right", "b", "second", None)]
    nodes[4]["supports"][0]["business_context"] = {}
    nodes[4]["supports"][0]["provenance"].pop("key_columns")
    nodes.append({**nodes[2], "identity_types": {}})
    nodes.append({**nodes[0], "identity_origin": "physical_column"})
    graph = {"nodes": nodes, "definitions": {"entities": [
        {"type": t, "key_names": ["code"]} for t in ("member", "other_type")]}}
    result = review(graph, example_limit=1)
    assert result["multiple_contributor_groups"] == 2
    assert result["excluded_nodes"] == {"non_row_identity": 1, "incomplete_typed_identity": 1}
    assert result["comparison_counts"] == {
        "physical_tables": {"disjoint": 2}, "ordered_key_column_names": {"identical_sets": 1, "incomplete": 1},
        "source_label_values": {"multiple": 1, "one": 1},
        "source_label_observations": {"complete": 1, "incomplete": 1},
        "access_scopes": {"identical_sets": 2}, "business_contexts": {"different_sets": 1, "incomplete": 1},
    }
    assert len(result["examples"]) == 1 and result["identity_verdict"] == "unverified"
    assert "confidential-code" not in json.dumps(result) and "left-only-label" not in json.dumps(result)


def test_key_order_and_partial_table_overlap_keep_their_meaning():
    nodes = [row(unit, table, "value", "label") for unit, table in [("a", "first"), ("b", "first"), ("c", "last")]]
    for node in nodes:
        node["identity"]["second"] = "another"
        node["identity_types"]["second"] = "text"
        node["supports"][0]["provenance"]["key_columns"] = ["k1", "k2"]
    nodes[2]["supports"][0]["provenance"]["key_columns"].reverse()
    graph = {"nodes": nodes, "definitions": {"entities": [{"type": "member", "key_names": ["code", "second"]}]}}
    sample = review(graph)["examples"][0]
    assert sample["physical_tables"] == "partial_overlap" and sample["ordered_key_column_names"] == "different_sets"
    # Missing executor provenance makes cross-unit attribution unverifiable.
    del nodes[0]["supports"][0]["contributor_id"]
    assert review(graph)["groups_by_contributors"] == {"unknown_contributors": 1}

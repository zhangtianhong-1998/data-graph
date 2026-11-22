import json
from pathlib import Path
import runpy

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_merge_candidate_evidence import candidate_unit


physical_key_overlap = runpy.run_path(str(
    Path(__file__).resolve().parents[3] / "scripts" / "audit_checkpoint_merge.py"
))["physical_key_overlap"]


def test_overlap_independent_of_logical_type_or_candidate_generation():
    inputs = [candidate_unit("a", [("table1", ["id"])]),
              candidate_unit("b", [("table1", ["id"])], entity_type="OtherRole"),
              candidate_unit("c", [("table2", ["id"])])]
    merged = merge_construction_graphs(inputs, recognize_source_replays=False)
    merged.pop("candidate_identity_merges", None)
    report = physical_key_overlap(merged)
    assert len(merged["nodes"]) == 3
    assert report["shared_keys_on_separate_nodes"] == report["shared_keys_on_separate_type_families"] == 1
    assert report["shared_keys_on_one_node"] == 0
    example, = report["separate_node_examples"]
    assert example["contributor_count"] == example["node_count"] == 2
    assert "OtherRole" not in json.dumps(report) and "table1" not in json.dumps(report)


def test_overlap_after_replay_counts_one_published_node_with_two_contributors():
    inputs = [candidate_unit(owner, [("table", ["id"])]) for owner in ("a", "b")]
    merged = merge_construction_graphs(inputs)
    report = physical_key_overlap(merged)
    assert len(merged["nodes"]) == report["shared_keys_on_one_node"] == 1
    assert report["shared_keys_on_separate_nodes"] == 0


def test_overlap_rejects_normalized_or_untyped_keys_and_different_typed_values():
    inputs = [candidate_unit(owner, [("table", ["id"])]) for owner in ("a", "b", "c", "d")]
    graph = merge_construction_graphs(inputs, recognize_source_replays=False)
    changed, untyped, numeric, _ = graph["nodes"]
    changed["supports"][0]["original_key_values"] = {"id": "before-normalization"}
    untyped["identity_types"] = {}
    key, = numeric["identity"]
    numeric["identity"][key] = 1
    numeric["identity_types"][key] = "integer"
    report = physical_key_overlap(graph)
    assert report["shared_keys_on_separate_nodes"] == 0
    assert report["skipped"] == {"nodes_without_complete_typed_key": 1,
                                 "supports_without_verified_key_binding": 1}

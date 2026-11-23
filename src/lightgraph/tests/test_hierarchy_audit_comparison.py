from pathlib import Path
import json
import runpy

import pytest


compare = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_checkpoint_merge.py"))["compare_hierarchy_audits"]


def test_run_summary_reads_only_the_requested_generation_and_parquet_footers(tmp_path, monkeypatch):
    from lightgraph.construction.checkpoints import commit_checkpoint
    from lightgraph.construction.workflow import save_workflow_state

    old, new = "run_" + "a" * 24, "run_" + "b" * 24
    original = tmp_path / "output"
    current = original / "generations" / new
    parent = commit_checkpoint(original, old, "extract_1", {"graph": {"nodes": [{"id": "a"}, {"id": "b"}]}},
                               input_digest="p")
    result = commit_checkpoint(current, new, "merge_1", {"graph": {"nodes": [{"id": "a"}]}},
                               input_digest="m", upstream_artifacts=[parent["artifact_id"]])
    state = {"parent_run_id": new, "resumed_from_run_id": old, "rerun_nodes": ["merge_1"], "reused_nodes": ["extract_1"],
             "merge_nodes": {"merge_1": {"status": "completed", "merge_mode": "deterministic", "nodes": 1, "edges": 0}},
             "artifacts": {"extract_1": parent, "merge_1": result}, "artifact_outputs": {"extract_1": "output"}}
    save_workflow_state(current, state)
    (tmp_path / "runs").mkdir()
    path = tmp_path / "runs" / (new + ".json")
    path.write_text(json.dumps({"run_id": new, "output_path": current.relative_to(tmp_path).as_posix()}))
    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_checkpoint_merge.py"))["audit"]
    monkeypatch.setitem(audit.__globals__, "load_checkpoint", lambda *_: pytest.fail("decoded checkpoint graph"))
    summary = audit(path, "merge_1", run_summary=True)
    assert summary["inputs"][0]["run_id"] == old and summary["inputs"][0]["nodes"] == 2
    assert summary["output"]["run_id"] == new and summary["output"]["artifact_id"] == result["artifact_id"]
    assert summary["reported_nodes"] == summary["output"]["nodes"] == 1
    (original / parent["relative_path"] / "nodes.parquet").unlink()
    damaged = audit(path, "merge_1", run_summary=True)
    assert damaged["inputs"][0]["read_error"] == "workflow_checkpoint_invalid" and damaged["output"]["nodes"] == 1
    del state["artifacts"]["merge_1"]
    state["merge_nodes"]["merge_1"] = {"status": "failed", "error_code": "workflow_checkpoint_invalid"}
    save_workflow_state(current, state)
    failed = audit(path, "merge_1", run_summary=True)
    assert failed["output"] is None and failed["error_code"] == "workflow_checkpoint_invalid"


def report(groups, nodes):
    removed = sum(len(ids) - 1 for ids in groups.values())
    return {"run_id": "r", "node_id": "merge", "artifact_id": "artifact", "upstream_artifacts": ["a", "b"],
            "with_source_replay": {"output_nodes": nodes, "hierarchy_replay_collapsed_nodes": removed,
                                   "hierarchy_membership": {"groups_by_output_id": groups,
                                                            "represented_collapsed_nodes": removed,
                                                            "agrees_with_recorded_count": True}}}


def test_same_count_can_hide_a_split_pair_or_a_different_winner():
    before = report({"a": ["a", "b"], "c": ["c", "d"]}, 4)
    after = report({"b": ["b", "a"], "d": ["d", "e"]}, 4)
    result = compare(before, after, {"b", "c", "d", "f"})
    assert result["net_node_reduction"] == result["additional_hierarchy_collapses"] == 0
    assert result["previous_groups"] == {"unchanged": 1, "expanded": 0, "split": 1, "unverified": 0}
    assert result["previous_member_pairs"] == {"retained": 1, "split": 1, "unverified": 0}


def test_unresolved_original_is_not_counted_as_split():
    before = report({"a": ["a", "b"], "d": ["d", "e", "f"]}, 3)
    after = report({"c": ["a", "b", "c"]}, 3)
    result = compare(before, after, {"c", "d", "e"})
    assert result["previous_groups"] == {"unchanged": 0, "expanded": 1, "split": 0, "unverified": 1}
    assert result["previous_member_pairs"] == {"retained": 1, "split": 1, "unverified": 2}


def test_comparison_requires_same_artifacts_and_complete_disjoint_groups():
    baseline = report({"a": ["a", "b"]}, 1)
    for wrong in [{"artifact_id": "other"}, {"upstream_artifacts": ["a", "c"]}, {"with_source_replay": {}}]:
        with pytest.raises(ValueError):
            compare({**baseline, **wrong}, baseline, {"a"})
    with pytest.raises(ValueError):
        compare(report({"a": ["a", "b"], "c": ["c", "b"]}, 2), baseline, {"a"})

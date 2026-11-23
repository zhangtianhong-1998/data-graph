from pathlib import Path
import runpy

import pytest


compare = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_checkpoint_merge.py"))["compare_hierarchy_audits"]


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

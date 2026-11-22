"""Compare a committed deterministic merge with two read-only replays of its upstream checkpoints."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from lightgraph.construction import executor, fusion, merger, metadata_columns, observed_identity, workspace
from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.workflow import load_workflow_state, run_output_path
from lightgraph.errors import LightGraphError


def graph_counts(graph):
    physical = sum(node.get("identity_origin") == "physical_column" for node in graph["nodes"])
    return {"nodes": len(graph["nodes"]), "physical_column_nodes": physical,
            "row_nodes": len(graph["nodes"]) - physical, "edges": len(graph["edges"]),
            **alignment_counts(graph)}


def alignment_counts(graph):
    """Summarize recorded suggestions and pending relations separately from edge integrity."""
    identities = graph.get("candidate_identity_merges")
    columns = graph.get("candidate_column_alignments")
    unresolved = graph.get("unresolved_relations")

    def histogram(records, field):
        if records is None:
            return None
        return dict(Counter(record.get(field) or "not_recorded" for record in records))

    # The historical reason identifies these groups, but gives no verdict
    # on whether the contributors shared a dictionary or a physical key.
    exact_keys = None if identities is None else [
        group for group in identities if group.get("match_basis") == "same_typed_identity"
        or group.get("reason") == "cross_unit_unverified_identity_domain"]
    return {
        "candidate_identity_groups": len(identities) if identities is not None else None,
        **(merger.identity_candidate_node_counts(graph) if identities is not None else {}),
        "candidate_groups_by_reason": histogram(identities, "reason"),
        "candidate_groups_by_match_basis": histogram(identities, "match_basis"),
        "same_identity_groups_by_source_key_comparison": histogram(exact_keys, "source_key_comparison"),
        "candidate_column_alignments": len(columns) if columns is not None else None,
        "column_alignment_groups_by_reason": histogram(columns, "reason"),
        "unresolved_relations": len(unresolved) if unresolved is not None else None,
    }


def output_checks(graph):
    """Check the complete output's references. Matching labels do not establish identity."""
    node_ids = {node["id"] for node in graph["nodes"]}
    return {
        "duplicate_node_ids": len(graph["nodes"]) - len(node_ids),
        "duplicate_edge_ids": len(graph["edges"]) - len({e["id"] for e in graph["edges"]}),
        "edges_with_missing_endpoints": sum(e["source"] not in node_ids or e["target"] not in node_ids
                                            for e in graph["edges"]),
    }


def audit(run_file, node_id):
    """Use the run's committed inputs and policy, even when the current project has been edited."""
    run_file = Path(run_file)
    run = json.loads(run_file.read_text(encoding="utf-8"))
    project = run_file.parent.parent
    if run_file.parent.name != "runs" or run.get("run_id") != run_file.stem:
        raise ValueError("请选择项目 runs 目录中的父运行 JSON 文件。")
    output = run_output_path(project, run["output_path"])
    state = load_workflow_state(output, run["run_id"])
    if not state or state.get("parent_run_id") != run["run_id"]:
        raise ValueError("缺少这次运行的工作流状态。")
    step = state.get("merge_nodes", {}).get(node_id, {})
    if step.get("merge_mode") != "deterministic":
        raise ValueError("请选择一个已提交的确定性合并节点。")
    artifacts = state["artifacts"]
    target = artifacts[node_id]
    if target["run_id"] != run["run_id"]:
        raise ValueError("此节点复用了旧合并结果，请选择实际提交该结果的原运行。")

    def read(record):
        location = state.get("artifact_outputs", {}).get(record["node_id"], run["output_path"])
        return load_checkpoint(run_output_path(project, location), record)

    upstream = {r["artifact_id"]: r for r in artifacts.values()}
    parents = [upstream[key] for key in target["upstream_artifacts"]]
    if (not parents or target.get("inherited_grants") or
            any(r["contributor_kind"] != "unit" or r.get("inherited_grants") for r in parents)):
        raise ValueError("此核查适用于直接合并抽取单元的节点。该节点还依赖其他合并或继承图。")

    committed = read(target)["graph"]
    counts = graph_counts(committed)
    counts["integrity"] = output_checks(committed)
    if "fusion_diagnostics" in committed:
        counts["merge_counts"] = committed["fusion_diagnostics"]
    fused = committed.get("fusion_policy")
    if fused is None:
        raise ValueError("提交图未记录实际生效的 fusion_policy，无法重放原策略。")
    plan = state["plan"]
    node = next((entry for entry in plan["merge_nodes"] if entry["node_id"] == node_id), None)
    if node is None:
        raise ValueError("历史工作流缺少该合并节点的配置。")
    policy = {**(node.get("merge_policy") or plan["merge_policy"]), "fusion_policy": fused}
    del committed

    inputs = [read(r) for r in parents]
    saved_counts = step.get("merge_counts") or {}
    totals = {f"input_{kind}": sum(len(c["graph"][kind]) for c in inputs) for kind in ("nodes", "edges")}
    if any(key in saved_counts and saved_counts[key] != count for key, count in totals.items()):
        raise ValueError("抽取检查点合计与原运行记录不符，可能还有其他输入。")
    modules = [merger, observed_identity, executor, fusion, metadata_columns, workspace]
    # A standalone copy of this script can also audit checkpoints using the
    # older backend, which did not retain catalog identity references.
    catalog_module = sys.modules.get('lightgraph.construction.catalog_identity')
    if catalog_module is not None:
        modules.append(catalog_module)
    report = {
        "run_id": run["run_id"], "node_id": node_id,
        "explicit_entity_rules": len(fused.get("entity_rules", [])),
        "source_sha256": {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                          for m in modules}, "merger_path": str(Path(merger.__file__).resolve()),
        "inputs": {c["contributor_id"]: graph_counts(c["graph"]) for c in inputs}, "committed": counts,
        "historical_input_totals_verified": totals.keys() <= saved_counts.keys(),
    }
    for enabled, label in [(False, "without_source_replay"), (True, "with_source_replay")]:
        graph = merger.merge_construction_graphs(inputs, target_access_fingerprint=target["access_fingerprint"],
                                                merge_policy=policy, recognize_source_replays=enabled)
        report[label] = {**merger.merge_accounting(inputs, graph), **alignment_counts(graph),
                         "integrity": output_checks(graph)}
        del graph
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_file", type=Path, help="原项目 runs/run_….json")
    parser.add_argument("--node", default="merge_1", help="要核对的确定性合并节点 ID")
    args = parser.parse_args()
    try:
        report = audit(args.run_file, args.node)
    except LightGraphError as exc:
        parser.exit(1, f"检查点或合并未通过校验：{exc.code}\n")
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"无法核对该运行：{exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

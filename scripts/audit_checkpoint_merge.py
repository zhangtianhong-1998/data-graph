"""Inspect a committed graph's identity evidence or compare two read-only replays of its merge."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

from lightgraph.construction import executor, fusion, merger, metadata_columns, observed_identity, workspace
from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.contexts import business_context_identity
from lightgraph.construction.workflow import load_workflow_state, run_output_path
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import identity_cell, persisted_identity_key


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
        **(merger.identity_candidate_node_counts(graph)
           if identities is not None and hasattr(merger, "identity_candidate_node_counts") else {}),
        "candidate_groups_by_reason": histogram(identities, "reason"),
        "candidate_groups_by_match_basis": histogram(identities, "match_basis"),
        "same_identity_groups_by_source_key_comparison": histogram(exact_keys, "source_key_comparison"),
        "candidate_column_alignments": len(columns) if columns is not None else None,
        "column_alignment_groups_by_reason": histogram(columns, "reason"),
        "unresolved_relations": len(unresolved) if unresolved is not None else None,
    }


def output_checks(graph):
    """Check the complete output's references. Matching labels do not establish identity."""
    node_types = {node["id"]: node["type"] for node in graph["nodes"]}
    relation_types = {r["type"]: (r.get("source_type"), r.get("target_type"))
                      for r in graph.get("definitions", {}).get("relations", [])}
    edges = graph["edges"]
    return {
        "duplicate_node_ids": len(graph["nodes"]) - len(node_types),
        "duplicate_edge_ids": len(edges) - len({e["id"] for e in edges}),
        "edges_with_missing_endpoints": sum(e["source"] not in node_types or e["target"] not in node_types for e in edges),
        "edges_with_missing_definition": sum(e["type"] not in relation_types for e in edges),
        "edges_with_wrong_endpoint_types": sum(
            (node_types[e["source"]], node_types[e["target"]]) != relation_types[e["type"]]
            for e in edges if e["source"] in node_types and e["target"] in node_types and e["type"] in relation_types),
        "self_links": sum(e["source"] == e["target"] for e in edges),
    }


def _compare_sets(sets):
    if any(None in values or not values for values in sets):
        return "incomplete"
    if all(values == sets[0] for values in sets):
        return "identical_sets"
    return "different_sets"


def _observations(node):
    """Compare recorded facts; keep database locations, labels and access scopes local."""
    facts = {key: set() for key in ("tables", "key_columns", "labels", "access", "business", "units")}
    for support in node.get("supports") or []:
        prov = support.get("provenance") or {}
        source = prov.get("source") or {}
        table = tuple(source.get(key) for key in ("source_id", "schema", "table"))
        local = not support.get("inherited_from")
        facts["tables"].add(table if local and all(table) else None)
        columns = prov.get("key_columns") or []
        complete = (isinstance(columns, list) and len(columns) == len(node["identity"])
                    and all(isinstance(c, str) and c for c in columns) and len(set(columns)) == len(columns))
        facts["key_columns"].add(tuple(columns) if complete and local else None)
        # Primary labels can be chosen at publication. Only compare labels backed by a source column.
        label = support.get("observed_label") or support.get("label")
        facts["labels"].add(label if isinstance(label, str) and label.strip() and prov.get("label_column") else None)
        scope = support.get("scope_ref")
        access = (scope, tuple(sorted(set(support.get("required_scope_refs") or [])))) if scope else None
        facts["access"].add(access)
        context = business_context_identity(support)
        facts["business"].add(canonical(context) if context["status"] != "unknown" else None)
        units = {origin.get("contributor_id") for origin in support.get("execution_origins") or []}
        units.add(support.get("contributor_id"))
        facts["units"].update(units - {None, ""} or {node.get("unit_id") or None})
    return facts


def typed_row_identity_review(graph, *, example_limit=8):
    """Independently group persisted rows by exact published type and complete typed identity."""
    definitions = {d["type"]: d for d in graph.get("definitions", {}).get("entities", [])}
    buckets = defaultdict(list)
    excluded = Counter()
    for offset, node in enumerate(graph["nodes"]):
        definition = definitions.get(node.get("type"), {})
        if node.get("identity_origin", definition.get("identity_origin", "row")) != "row":
            excluded["non_row_identity"] += 1
            continue
        names = set(definition.get("key_names") or [])
        identity, types = node.get("identity") or {}, node.get("identity_types") or {}
        if (not names or names != set(identity) or names != set(types)
                or any(identity[k] in (None, "") or types[k] != identity_cell(node, k)[0] for k in names)):
            excluded["incomplete_typed_identity"] += 1
            continue
        buckets[node["type"], persisted_identity_key(node)].append(offset)

    comparisons = defaultdict(Counter)
    unit_counts = Counter()
    examples = []
    for offsets in buckets.values():
        if len(offsets) < 2:
            continue
        facts = [_observations(graph["nodes"][i]) for i in offsets]
        unit_sets = [f["units"] for f in facts]
        if any(None in units or not units for units in unit_sets):
            unit_counts["unknown_contributors"] += 1
            continue
        if len(set.union(*unit_sets)) == 1:
            unit_counts["single_contributor"] += 1
            continue
        unit_counts["multiple_contributors"] += 1
        table_sets = [f["tables"] for f in facts]
        if _compare_sets(table_sets) == "incomplete":
            table_overlap = "incomplete"
        elif set.intersection(*table_sets):
            table_overlap = "table_common_to_all"
        elif len(set.union(*table_sets)) == sum(map(len, table_sets)):
            table_overlap = "disjoint"
        else:
            table_overlap = "partial_overlap"
        labels = set.union(*(f["labels"] for f in facts)) - {None}
        labels_complete = all(f["labels"] and None not in f["labels"] for f in facts)
        assessments = {
            "physical_tables": table_overlap,
            "ordered_key_column_names": _compare_sets([f["key_columns"] for f in facts]),
            "source_label_values": "multiple" if len(labels) > 1 else "one" if labels else "none",
            "source_label_observations": "complete" if labels_complete else "incomplete",
            "access_scopes": _compare_sets([f["access"] for f in facts]),
            "business_contexts": _compare_sets([f["business"] for f in facts]),
        }
        for field, value in assessments.items():
            comparisons[field][value] += 1
        if len(examples) < example_limit:
            examples.append({"node_count": len(offsets), "contributor_count": len(set.union(*unit_sets)),
                             "members": [{"node_offset": i, "observed_table_count": len(f["tables"] - {None})}
                                         for i, f in zip(offsets[:8], facts[:8])], **assessments})
    return {
        "basis": "same_published_type_and_complete_typed_row_identity",
        "identity_verdict": "unverified", "excluded_nodes": dict(excluded),
        "eligible_row_nodes": sum(map(len, buckets.values())),
        "groups_by_contributors": dict(unit_counts),
        "multiple_contributor_groups": unit_counts["multiple_contributors"],
        "comparison_counts": dict(comparisons), "examples": examples,
    }


def _physical_binding(support, node, names):
    prov = support.get("provenance") or {}
    source = prov.get("source") or {}
    address = tuple(source.get(k) for k in ("source_id", "schema", "table"))
    columns = prov.get("key_columns")
    if (not all(isinstance(part, str) and part for part in address)
            or not isinstance(columns, list) or len(columns) != len(names)
            or not all(isinstance(col, str) and col for col in columns)
            or len(set(columns)) != len(columns)):
        return None
    # A fused output can carry normalized values or reordered logical keys.
    # Only count a projection when the original positional values still agree.
    original = support.get("original_key_values")
    if original is not None and (not isinstance(original, dict) or set(original) != set(columns)
            or any(canonical(original[col]) != canonical(node["identity"][key])
                   for col, key in zip(columns, names))):
        return None
    if support.get("identity_origins") and original is None:
        return None
    return address, tuple(columns)


def physical_key_overlap(graph):
    """Count shared source addresses across units, including different logical type names.

    The same source key can denote several roles or belong to distinct scopes.
    No candidate, node or edge is rewritten. Counts refer to physical key
    groups, and one node can occur in more than one such group.
    """
    definitions = {d["type"]: d for d in graph.get("definitions", {}).get("entities", [])}
    groups = defaultdict(dict)
    skipped = Counter()
    eligible = 0
    for node in graph.get("nodes", []):
        definition = definitions.get(node["type"], {})
        if node.get("identity_origin", definition.get("identity_origin", "row")) != "row":
            continue
        names = definition.get("key_names", [])
        identity = node.get("identity", {})
        types = node.get("identity_types", {})
        if (not names or len(set(names)) != len(names) or set(names) != set(identity)
                or set(names) != set(types) or any(identity[k] in (None, "") for k in names)):
            skipped["nodes_without_complete_typed_key"] += 1
            continue
        cells = tuple(identity_cell(node, k) for k in names)
        if any(cell[0] != types[k] for k, cell in zip(names, cells)):
            skipped["nodes_with_inconsistent_key_types"] += 1
            continue
        if not node.get("supports"):
            skipped["nodes_without_supports"] += 1
        for support in node.get("supports", []):
            owners = {origin["contributor_id"] for origin in support.get("execution_origins", [])
                      if origin.get("contributor_id")}
            if support.get("contributor_id"):
                owners.add(support["contributor_id"])
            if not owners or support.get("inherited_from"):
                skipped["supports_without_direct_contributor"] += 1
                continue
            binding = _physical_binding(support, node, names)
            if binding is None:
                skipped["supports_without_verified_key_binding"] += 1
                continue
            eligible += 1
            members = groups[binding, cells]
            members.setdefault(node["id"], set()).update(owners)

    node_offsets = {n["id"]: i for i, n in enumerate(graph.get("nodes", []))}
    together = separate = cross_family = 0
    examples = []
    for members in groups.values():
        contributors = set().union(*members.values())
        if len(contributors) < 2:
            continue
        if len(members) == 1:
            together += 1
            continue
        separate += 1
        types = {graph["nodes"][node_offsets[nid]]["type"] for nid in members}
        family_count = len({definitions[t].get("original_type") or t for t in types})
        cross_family += family_count > 1
        if len(examples) < 5:
            examples.append({
                "type_family_count": family_count, "node_count": len(members),
                "contributor_count": len(contributors),
                "node_offsets": sorted(node_offsets[nid] for nid in members)[:6],
            })
    return {"eligible_supports": eligible, "skipped": dict(skipped),
            "shared_keys_on_one_node": together, "shared_keys_on_separate_nodes": separate,
            "shared_keys_on_separate_type_families": cross_family, "separate_node_examples": examples}


def inspect_observations(graph):
    definitions = graph.get("definitions", {})
    catalog = graph.get("evidence_catalog", {})
    kinds = Counter()
    for definition in definitions.get("entities", []):
        if definition.get("identity_scope") == "global":
            refs = definition.get("identity_evidence_refs") or []
            evidence = sorted({catalog.get(ref, {}).get("kind", "unknown") for ref in refs})
            kinds[",".join(evidence) or "no_reference"] += 1
    directions = Counter(rel.get("hierarchy_orientation") or "undeclared" for rel in definitions.get("relations", []))
    return {"typed_row_identity": typed_row_identity_review(graph), "physical_key_overlap": physical_key_overlap(graph),
            "global_definitions_by_evidence_kind": dict(kinds), "relation_definitions_by_hierarchy_orientation": dict(directions)}


def audit(run_file, node_id, *, observations_only=False):
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

    if observations_only:
        graph = read(target)["graph"]
        return {"run_id": run["run_id"], "node_id": node_id, "mode": "committed_observations_only",
                "artifact_id": target["artifact_id"], "graph_digest": target["graph_digest"],
                "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                "integrity": output_checks(graph), "recorded_candidates": alignment_counts(graph),
                **inspect_observations(graph)}

    upstream = {r["artifact_id"]: r for r in artifacts.values()}
    parents = [upstream[key] for key in target["upstream_artifacts"]]
    if (not parents or target.get("inherited_grants") or
            any(r["contributor_kind"] != "unit" or r.get("inherited_grants") for r in parents)):
        raise ValueError("此核查适用于直接合并抽取单元的节点。该节点还依赖其他合并或继承图。")

    committed = read(target)["graph"]
    counts = graph_counts(committed)
    counts["integrity"] = output_checks(committed)
    counts.update(inspect_observations(committed))
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
    for name in ('catalog_identity', 'hierarchy_replay', 'hierarchy_observations', 'hierarchy_captions'):
        module = sys.modules.get('lightgraph.construction.' + name)
        if module is not None:
            modules.append(module)
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
    parser.add_argument("--observations-only", action="store_true", help="只核查已提交图的实际观测，跳过合并重放")
    args = parser.parse_args()
    try:
        report = audit(args.run_file, args.node, observations_only=args.observations_only)
    except LightGraphError as exc:
        parser.exit(1, f"检查点或合并未通过校验：{exc.code}\n")
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"无法核对该运行：{exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

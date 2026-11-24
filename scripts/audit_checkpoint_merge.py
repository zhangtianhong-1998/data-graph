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
            "hierarchy_input_evidence": hierarchy_input_evidence(graph),
            **alignment_counts(graph)}


def hierarchy_input_evidence(graph):
    """Count recorded evidence on the inputs, independently of any later identity verdict."""
    hierarchy_types = {r["type"] for r in graph.get("definitions", {}).get("relations", [])
                       if r.get("source_type") == r.get("target_type") and r.get("hierarchy_orientation") != "none"}
    edges = witnessed = 0
    for edge in graph["edges"]:
        if edge["type"] in hierarchy_types:
            for support in edge.get("supports", []):
                edges += 1
                witnessed += (support.get("source_row_witness") or {}).get("kind") == "excel_rows_v1"
    return {"same_type_potential_hierarchy_edge_supports": edges,
            "with_recorded_excel_row_witness": witnessed}


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


def hierarchy_membership(graph, control_ids):
    """Report original replay IDs, so a later audit can check the same members pair by pair."""
    output_ids = {node["id"] for node in graph["nodes"]}
    groups = {}
    for node in graph["nodes"]:
        winner = node["id"]
        if winner not in control_ids:
            continue
        originals = {origin["id"] for s in node.get("supports", [])
                     if s.get("identity_basis") == "observed_hierarchy_projection_v1"
                     for origin in s.get("identity_origins", []) if origin.get("id") in control_ids}
        if winner in originals and len(originals) > 1 and (originals & output_ids) == {winner}:
            groups[winner] = sorted(originals)
    removed = [original for winner, originals in groups.items() for original in originals if original != winner]
    represented = len(set(removed))
    recorded = graph.get("fusion_diagnostics", {}).get("hierarchy_replay_collapsed_nodes")
    return {"groups_by_output_id": groups, "represented_collapsed_nodes": represented,
            "agrees_with_recorded_count": len(removed) == represented == recorded if recorded is not None else None}


def _edge_key(edge, id_field):
    return tuple(edge.get(k) for k in (id_field, 'type', 'source', 'target'))


def _rows_hash(rows):
    h = hashlib.sha256()
    for row in sorted(canonical(row) for row in rows):
        h.update(row.encode('utf-8') + b'\n')
    return h.hexdigest()


def edge_lineage(inputs, graph, stage):
    """Count input edge occurrences by contributor. Separate checkpoint and in-memory addresses."""
    owners = defaultdict(set)
    id_owners = defaultdict(set)
    counts = Counter()
    for item in inputs:
        owner = (item.get('kind', 'unit'), item['contributor_id'])
        for edge in item['graph']['edges']:
            key = _edge_key(edge, 'id')
            owners[key].add(owner)
            id_owners[key[0]].add(owner)
            counts[owner, key] += 1
    matches = defaultdict(lambda: defaultdict(set))
    uncertain = defaultdict(set)
    qualified = set()
    qualified_results = Counter()
    stage_results = Counter()
    identities, contracts, support_bindings = [], [], []
    declarations = {r['type']: hashlib.sha256(canonical(fusion.definition_contract(r)).encode('utf-8')).hexdigest()
                    for r in graph.get('definitions', {}).get('relations', ())}

    def match_support(output, key, support_owners, declared, basis):
        possible = {o for o in owners.get(key, ()) if o[1] not in declared}
        remaining = support_owners - declared - {None}
        attributed = {o for o in possible if o[1] in remaining}
        if len(remaining) == len(attributed) == 1:
            matches[next(iter(attributed)), key][output].add(basis)
        elif not declared or remaining:
            for owner in attributed or possible:
                ambiguous = len(remaining) > 1 or len(attributed or possible) > 1
                uncertain[owner, key].add('ambiguous_owner' if ambiguous else 'unattributed')

    for edge in graph['edges']:
        output = _edge_key(edge, 'id')
        identities.append(output)
        contracts.append((output, (edge.get('definition_variant') or {}).get('contract_digest'),
                          declarations.get(edge['type'])))
        seen_stage = set()
        for support in edge.get('supports') or [{}]:
            support_owners = {s.get('contributor_id') for s in support.get('execution_origins', ())}
            support_owners.add(support.get('contributor_id'))
            origins = support.get('endpoint_origins') or []
            if isinstance(origins, dict):
                origins = [origins]
            if edge.get('supports'):
                observation = support.get('observation_id')
                if not observation:
                    evidence = {k: v for k, v in support.items() if k not in ('observation_id', 'endpoint_origins')}
                    observation = 'support_sha256:' + _rows_hash([evidence])
                support_bindings.append((output, observation, _rows_hash(origins)))
            declared = {o.get('contributor_id') for o in origins if o.get('input_kind') == 'unit'}
            # A contributor with an explicit origin has already identified its
            # actual input. Coinciding intermediate or output IDs add no evidence.
            match_support(output, output, support_owners, declared, 'unchanged')
            for origin in origins:
                key = _edge_key(origin, 'edge_id')
                if 'input_kind' in origin:
                    owner = (origin['input_kind'], origin.get('contributor_id'))
                    if origin['input_kind'] != 'unit':
                        continue  # This audit accepts direct unit checkpoints only.
                    claim = output, owner, key
                    if claim not in qualified:
                        qualified.add(claim)
                        verified = owner in owners.get(key, ())
                        qualified_results['verified' if verified else 'not_in_named_checkpoint'] += 1
                        if verified:
                            matches[owner, key][output].add('qualified_origin')
                elif origin.get('stage') == 'before_hierarchy_replay':
                    verified = stage is not None and key in stage
                    if key not in seen_stage:
                        seen_stage.add(key)
                        status = 'not_captured' if stage is None else 'verified' if verified else 'not_in_stage'
                        stage_results[status] += 1
                    if verified:
                        match_support(output, key, support_owners, declared, 'stage_tuple_and_owner_match_checkpoint')
                elif not origin.get('stage'):
                    match_support(output, key, support_owners, declared, 'legacy_tuple_and_owner_match_checkpoint')

    by_owner = {item['contributor_id']: Counter() for item in inputs}
    for (owner, key), count in counts.items():
        outputs = matches.get((owner, key), {})
        if count > 1:
            status = 'duplicate_input_tuple'
        elif len(outputs) > 1:
            status = 'multiple_matching_outputs'
        elif outputs:
            bases = next(iter(outputs.values()))
            status = next(b for b in ('unchanged', 'qualified_origin', 'stage_tuple_and_owner_match_checkpoint',
                                      'legacy_tuple_and_owner_match_checkpoint') if b in bases)
        else:
            unresolved = uncertain.get((owner, key), ())
            status = 'ambiguous_owner' if 'ambiguous_owner' in unresolved else 'unattributed' if unresolved else 'unverified'
        by_owner[owner[1]][status] += count
    return {'input_edge_counts_by_contributor': by_owner,
            'input_ids_shared_across_contributors': sum(len(v) > 1 for v in id_owners.values()),
            'input_tuples_shared_across_contributors': sum(len(v) > 1 for v in owners.values()),
            'qualified_origin_bindings': dict(qualified_results),
            'before_hierarchy_replay': {'captured_edges': None if stage is None else len(stage),
                                        'identity_sha256': None if stage is None else _rows_hash(stage),
                                        'origin_bindings': dict(stage_results)},
            'output_edge_identity_sha256': _rows_hash(identities), 'output_edge_contract_sha256': _rows_hash(contracts),
            'qualified_origin_bindings_sha256': _rows_hash(qualified),
            'output_support_endpoint_origins_sha256': _rows_hash(support_bindings)}


def _membership_groups(report):
    replay = report.get("with_source_replay") or {}
    membership = replay.get("hierarchy_membership") if isinstance(replay, dict) else None
    if not isinstance(membership, dict):
        raise ValueError("对照报告缺少层级归并成员记录。")
    groups = membership.get("groups_by_output_id")
    if not isinstance(groups, dict) or membership.get("agrees_with_recorded_count") is not True:
        raise ValueError("对照报告缺少已核实的逐成员映射。请使用带 --include-hierarchy-membership 的审计输出。")
    owners = {}
    for winner, members in groups.items():
        if (not isinstance(members, list) or len(members) < 2 or winner not in members
                or not all(isinstance(n, str) and n for n in members)):
            raise ValueError("层级归并成员记录无效。")
        for nid in members:
            if nid in owners:
                raise ValueError("同一原节点在层级归并映射中重复出现。")
            owners[nid] = winner
    collapsed = len(owners) - len(groups)
    if collapsed != membership.get("represented_collapsed_nodes") or collapsed != replay.get("hierarchy_replay_collapsed_nodes"):
        raise ValueError("逐成员记录与层级归并计数不一致。")
    if type(replay.get("output_nodes")) is not int or replay["output_nodes"] < len(groups):
        raise ValueError("审计报告的输出节点数无效。")
    return groups, owners


def compare_hierarchy_audits(previous, current, output_ids):
    """Trace previously joined original IDs, including groups whose winning ID changed."""
    for key in ("run_id", "node_id", "artifact_id", "upstream_artifacts"):
        if not previous.get(key) or previous[key] != current[key]:
            raise ValueError(f"对照报告与本轮检查点不一致：{key}")
    if previous.get("explicit_entity_rules") != current.get("explicit_entity_rules"):
        raise ValueError("两轮的显式实体规则数量不一致。")
    if "merge_policy_sha256" in previous and previous["merge_policy_sha256"] != current.get("merge_policy_sha256"):
        raise ValueError("两轮重放采用的合并策略不一致。")
    old_groups, _ = _membership_groups(previous)
    new_groups, owners = _membership_groups(current)
    group_counts = Counter()
    pair_counts = Counter()
    for members in old_groups.values():
        targets = Counter()
        for original in members:
            if original in owners:
                targets[owners[original]] += 1
            elif original in output_ids:
                targets[original] += 1
        known = sum(targets.values())
        pairs = len(members) * (len(members) - 1) // 2
        resolved = known * (known - 1) // 2
        retained = sum(count * (count - 1) // 2 for count in targets.values())
        pair_counts.update(retained=retained, split=resolved - retained, unverified=pairs - resolved)
        if known < len(members):
            outcome = "unverified"
        elif len(targets) > 1:
            outcome = "split"
        elif len(new_groups[next(iter(targets))]) > known:
            outcome = "expanded"
        else:
            outcome = "unchanged"
        group_counts[outcome] += 1
    before, after = previous["with_source_replay"], current["with_source_replay"]
    return {"policy_digest_comparison": "matched" if "merge_policy_sha256" in previous else "previous_digest_not_recorded",
            "net_node_reduction": before["output_nodes"] - after["output_nodes"],
            "additional_hierarchy_collapses": after["hierarchy_replay_collapsed_nodes"] - before["hierarchy_replay_collapsed_nodes"],
            "previous_groups": {k: group_counts[k] for k in ("unchanged", "expanded", "split", "unverified")},
            "previous_member_pairs": {k: pair_counts[k] for k in ("retained", "split", "unverified")}}


def _run_summary(run, state, project, node_id):
    """Inspect committed markers and Parquet footers without materializing or replaying a graph."""
    from lightgraph.construction.checkpoints import verify_checkpoint_files
    import pyarrow.parquet as pq

    step = state.get('merge_nodes', {}).get(node_id)
    if step is None:
        raise ValueError("所选运行中没有这个合并节点。")
    artifacts = state.get('artifacts', {})
    target = artifacts.get(node_id)

    def footer(record):
        identity = {k: record.get(k) for k in ('run_id', 'node_id', 'artifact_id', 'graph_digest', 'input_digest')}
        try:
            location = state.get('artifact_outputs', {}).get(record['node_id'], run['output_path'])
            path = verify_checkpoint_files(run_output_path(project, location), record)
            counts = {name: pq.read_metadata(path / (name + '.parquet')).num_rows for name in ('nodes', 'edges')}
        except LightGraphError as exc:
            return {**identity, 'read_error': exc.code}
        except (OSError, ValueError):
            return {**identity, 'read_error': 'parquet_footer_unreadable'}
        return {**identity, **counts}

    upstream = {record['artifact_id']: record for record in artifacts.values()}
    inputs = [footer(upstream[key]) if key in upstream else {'artifact_id': key, 'status': 'not_in_run_state'}
              for key in target['upstream_artifacts']] if target else []
    return {
        'mode': 'recorded_run_and_parquet_footers', 'run_id': run['run_id'], 'run_status': run.get('status'),
        'resumed_from_run_id': state.get('resumed_from_run_id'), 'resume_action': state.get('resume_action'),
        'rerun_nodes': state.get('rerun_nodes'), 'reused_nodes': state.get('reused_nodes'),
        'node_id': node_id, 'node_status': step.get('status'), 'artifact_status': step.get('artifact_status'),
        'error_code': step.get('error_code') or run.get('error_code'),
        'merge_mode': step.get('merge_mode'), 'plan_mode': state.get('plan_mode'), 'terminal_ids': state.get('terminal_ids'),
        'configured_inputs': [e['source'] for e in (state.get('plan') or {}).get('edges', []) if e['target'] == node_id],
        'reported_nodes': step.get('nodes'), 'reported_edges': step.get('edges'),
        'merge_counts': step.get('merge_counts'), 'delta_merge_counts': step.get('delta_merge_counts'),
        'inputs': inputs,
        'output': footer(target) if target else None,
        'publication': {k: v for k, v in (state.get('merge') or {}).items()
                        if k in ('status', 'mode', 'publication_status', 'published_artifact_id', 'nodes', 'edges', 'error_code')},
    }


def audit(run_file, node_id, *, observations_only=False, include_hierarchy_membership=False, compare_to=None,
          run_summary=False, include_edge_lineage=False):
    """Use the run's committed inputs and policy, even when the current project has been edited."""
    if include_edge_lineage and (observations_only or run_summary):
        raise ValueError("--include-edge-lineage 需要执行合并重放")
    run_file = Path(run_file)
    run = json.loads(run_file.read_text(encoding="utf-8"))
    project = run_file.parent.parent
    if run_file.parent.name != "runs" or run.get("run_id") != run_file.stem:
        raise ValueError("请选择项目 runs 目录中的父运行 JSON 文件。")
    output = run_output_path(project, run["output_path"])
    state = load_workflow_state(output, run["run_id"])
    if not state or state.get("parent_run_id") != run["run_id"]:
        raise ValueError("缺少这次运行的工作流状态。")
    if run_summary:
        return _run_summary(run, state, project, node_id)
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
    report = {
        "run_id": run["run_id"], "node_id": node_id,
        "artifact_id": target["artifact_id"], "upstream_artifacts": target["upstream_artifacts"],
        "explicit_entity_rules": len(fused.get("entity_rules", [])),
        "merge_policy_sha256": hashlib.sha256(canonical(policy).encode("utf-8")).hexdigest(),
        "merger_path": str(Path(merger.__file__).resolve()),
        "inputs": {c["contributor_id"]: graph_counts(c["graph"]) for c in inputs}, "committed": counts,
        "historical_input_totals_verified": totals.keys() <= saved_counts.keys(),
    }
    if include_edge_lineage:
        report['lineage_input_artifacts'] = {r['node_id']: r['artifact_id'] for r in parents}
    control_ids = set()
    for enabled, label in [(False, "without_source_replay"), (True, "with_source_replay")]:
        stage = None

        def capture_stage(refs):
            nonlocal stage
            stage = {_edge_key(ref, 'edge_id') for ref in refs}

        options = {'observe_before_hierarchy': capture_stage} if include_edge_lineage else {}
        graph = merger.merge_construction_graphs(inputs, target_access_fingerprint=target["access_fingerprint"],
                                                merge_policy=policy, recognize_source_replays=enabled, **options)
        report[label] = {**merger.merge_accounting(inputs, graph), **alignment_counts(graph),
                         "integrity": output_checks(graph)}
        if include_edge_lineage:
            report[label]['edge_lineage'] = edge_lineage(inputs, graph, stage)
        if include_hierarchy_membership or compare_to is not None:
            if enabled:
                report[label]["hierarchy_membership"] = hierarchy_membership(graph, control_ids)
                if compare_to is not None:
                    previous = json.loads(Path(compare_to).read_text(encoding="utf-8-sig"))
                    if not isinstance(previous, dict):
                        raise ValueError("请选择上一轮完整的 audit.json。")
                    report["hierarchy_comparison"] = compare_hierarchy_audits(
                        previous, report, {n["id"] for n in graph["nodes"]})
            else:
                control_ids = {n["id"] for n in graph["nodes"]}
        del graph
    # Some modules are loaded only by branches exercised during the replay.
    modules = [merger, observed_identity, executor, fusion, metadata_columns, workspace]
    for name in ('catalog_identity', 'hierarchy_replay', 'hierarchy_observations', 'hierarchy_captions',
                 'hierarchy_role_witnesses', 'hierarchy_neighborhood', 'hierarchy_worksheet_properties', 'hierarchy_stage',
                 'identity_authority', 'source_namespaces'):
        module = sys.modules.get('lightgraph.construction.' + name)
        if module is not None:
            modules.append(module)
    source_files = {m.__name__: Path(m.__file__).read_bytes() for m in modules}
    report["source_sha256"] = {name: hashlib.sha256(content).hexdigest() for name, content in source_files.items()}
    source_files["audit_checkpoint_merge.py"] = Path(__file__).read_bytes()
    report["source_sha256_lf"] = {name: hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
                                  for name, content in source_files.items()}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_file", type=Path, help="原项目 runs/run_….json")
    parser.add_argument("--node", default="merge_1", help="要核对的确定性合并节点 ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-summary", action="store_true", help="只读正式运行记录、检查点标记和 Parquet 行数，不加载图或重放合并")
    mode.add_argument("--observations-only", action="store_true", help="只核查已提交图的实际观测，跳过合并重放")
    mode.add_argument("--include-hierarchy-membership", action="store_true", help="在重放报告中列出层级归并前后的节点 ID")
    parser.add_argument("--include-edge-lineage", action="store_true", help="逐单元核对原边来源和层级归并前的边，输出计数与摘要")
    parser.add_argument("--compare-hierarchy-audit", type=Path, metavar="AUDIT_JSON",
                        help="与同一批检查点的上一轮完整审计报告逐组比较，并输出本轮成员映射")
    args = parser.parse_args()
    if (args.observations_only or args.run_summary) and args.compare_hierarchy_audit:
        parser.error("--compare-hierarchy-audit 需要执行合并重放")
    try:
        report = audit(args.run_file, args.node, observations_only=args.observations_only,
                       include_hierarchy_membership=args.include_hierarchy_membership, compare_to=args.compare_hierarchy_audit,
                       run_summary=args.run_summary, include_edge_lineage=args.include_edge_lineage)
    except LightGraphError as exc:
        parser.exit(1, f"检查点或合并未通过校验：{exc.code}\n")
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"无法核对该运行：{exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

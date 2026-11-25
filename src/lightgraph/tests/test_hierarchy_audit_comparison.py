from pathlib import Path
import json
import runpy

import pytest


compare = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_checkpoint_merge.py"))["compare_hierarchy_audits"]


def test_review_evidence_distinguishes_different_contract_fields_and_missing_access():
    from lightgraph.construction.overlap_index import overlap_identity_contract
    from lightgraph.contracts import digest

    explain = compare.__globals__['_overlap_eligibility_evidence']
    members = [{'contributor_id': owner, 'type': 'Member'} for owner in ('a', 'b')]
    review = {'decisions': [{'pattern_id': 'p', 'action': 'review'}], 'patterns': [{
        'pattern_id': 'p', 'kind': 'entity', 'basis': 'same_physical_key', 'members': members,
        'eligible_actions': ['retain'], 'reasons': ['different_identity_contracts', 'unverified_access_fingerprint']}]}
    indexes, definitions = {}, {}
    for owner in ('a', 'b'):
        definition = {'type': 'Member', 'key_names': ['id'], 'identity_scope': 'global', 'identity_domain': owner}
        definitions[owner, 'Member'] = definition
        indexes[owner] = {'access_fingerprint': None if owner == 'a' else 'abc', 'families': [{
            'type': 'Member', 'contract_fingerprint': digest(overlap_identity_contract(definition)),
            'problems': [], 'sources': [], 'witness_count': 0}]}
    row, = explain(review, indexes, definitions)
    assert row['different_contract_fields'] == ['identity_domain']
    assert row['access_missing_from'] == ['a'] and row['distinct_recorded_access_fingerprints'] == 1
    assert row['decision'] == 'review' and row['eligible_actions'] == ['retain']
    definitions['b', 'Member']['identity_domain'] = 'other'
    with pytest.raises(ValueError, match='契约摘要'):
        explain(review, indexes, definitions)


def test_graph_row_digests_compare_payloads_and_ignore_storage_order():
    row_digests = compare.__globals__['_graph_row_digests']
    nodes = [{'id': 'a', 'supports': [{'label': 'original'}]}, {'id': 'b'}]
    edges = [{'id': 'r', 'source': 'a', 'target': 'b'}]
    saved = row_digests({'nodes': nodes, 'edges': edges})
    assert row_digests({'nodes': list(reversed(nodes)), 'edges': edges}) == saved
    nodes[0]['supports'][0]['label'] = 'changed'
    changed = row_digests({'nodes': nodes, 'edges': edges})
    assert changed['nodes'] != saved['nodes'] and changed['edges'] == saved['edges']
    assert row_digests({'nodes': nodes, 'edges': edges * 2})['edges'] != saved['edges']


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


def test_edge_lineage_qualifies_shared_input_ids_and_validates_actual_hierarchy_stage():
    from lightgraph.construction.fusion import edge_reference
    review = compare.__globals__['edge_lineage']
    raw = {'id': 'same', 'source': 'a', 'target': 'b', 'type': 'base'}
    inputs = [{'contributor_id': c, 'graph': {'edges': [raw]}} for c in ('one', 'two', 'three')]
    interim = {**raw, 'id': 'intermediate', 'source': 'qualified-a'}
    final = {**interim, 'id': 'final', 'type': 'new'}
    stage = {('intermediate', 'base', 'qualified-a', 'b')}
    support = {'contributor_id': 'one', 'endpoint_origins': [
        {**edge_reference(raw), 'input_kind': 'unit', 'contributor_id': 'one'},
        {**edge_reference(raw), 'input_kind': 'unit', 'contributor_id': 'nonexistent'},
        {**edge_reference(interim), 'stage': 'before_hierarchy_replay'},
        {**edge_reference(raw), 'stage': 'before_hierarchy_replay'}]}
    # The unchanged legacy address has no owner. It cannot account for either remaining contributor.
    unknown = {**raw, 'id': 'other', 'supports': [{'endpoint_origins': edge_reference(raw)}]}
    graph = {'edges': [{**final, 'supports': [support, support]}, unknown]}
    result = review(inputs, graph, stage)
    assert result['input_ids_shared_across_contributors'] == result['input_tuples_shared_across_contributors'] == 1
    assert result['input_edge_counts_by_contributor'] == {
        'one': {'qualified_origin': 1}, 'two': {'ambiguous_owner': 1}, 'three': {'ambiguous_owner': 1}}
    assert result['qualified_origin_bindings'] == {'verified': 1, 'not_in_named_checkpoint': 1}
    assert result['before_hierarchy_replay']['captured_edges'] == 1
    assert result['before_hierarchy_replay']['origin_bindings'] == {'verified': 1, 'not_in_stage': 1}
    assert review(inputs[::-1], {'edges': graph['edges'][::-1]}, stage) == result
    assert review(inputs, graph, None)['before_hierarchy_replay']['origin_bindings'] == {'not_captured': 2}
    graph['edges'][0]['definition_variant'] = {'contract_digest': 'revised'}
    changed = review(inputs, graph, stage)
    assert changed['output_edge_contract_sha256'] != result['output_edge_contract_sha256']
    assert changed['output_edge_identity_sha256'] == result['output_edge_identity_sha256']
    # Swapping the same origins between two observations cannot be seen at edge granularity.
    edge = graph['edges'][0]
    edge['supports'] = [{'observation_id': 'obs_1', 'endpoint_origins': support['endpoint_origins']},
                        {'properties': {'row': ['another']}}]
    before = review(inputs, graph, stage)
    edge['supports'][1]['endpoint_origins'] = edge['supports'][0].pop('endpoint_origins')
    after = review(inputs, graph, stage)
    assert before['qualified_origin_bindings_sha256'] == after['qualified_origin_bindings_sha256']
    assert before['output_support_endpoint_origins_sha256'] != after['output_support_endpoint_origins_sha256']


def test_actual_partial_fold_reconciles_retyped_edges_to_units_and_the_captured_intermediate():
    from lightgraph.construction.fusion import relation_identity
    from lightgraph.construction.merger import merge_construction_graphs
    from lightgraph.tests.test_hierarchy_projection_variants import _projection, contributor

    units = []
    for i in range(3):
        g = _projection(i, conflict=True)
        for r in g['definitions']['relations']:
            r['type'] = 'contains'
        for e in g['edges']:
            e['type'] = 'contains'
            e['id'] = relation_identity(e)
        units.append(contributor(str(i), g))
    captured = []
    graph = merge_construction_graphs(units, observe_before_hierarchy=captured.extend)
    stage = {compare.__globals__['_edge_key'](e, 'edge_id') for e in captured}
    result = compare.__globals__['edge_lineage'](units, graph, stage)
    assert result['input_edge_counts_by_contributor'] == {str(i): {'qualified_origin': 3} for i in range(3)}
    results = result['before_hierarchy_replay']['origin_bindings']
    assert results.get('verified', 0) > 0 and set(results) == {'verified'}
    assert len(stage) == result['before_hierarchy_replay']['captured_edges'] == 9

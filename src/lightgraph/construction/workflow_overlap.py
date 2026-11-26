"""Review overlaps between sibling checkpoints from the same extraction run."""
from __future__ import annotations

import time

from lightgraph.errors import LightGraphError
from .access import access_fingerprint
from .merger import merge_construction_graphs
from .overlap_patterns import analyze_indexed_overlap_patterns, apply_overlap_decisions
from .overlap_review import review_overlap_patterns
from .relation_proposals import annotate_relation_meanings, verify_relation_proposals
from .source_namespaces import project_checkpoint_inputs


def overlap_review_skip_reason(inputs, input_ids, artifacts, inherited):
    if (inherited or any(c.get('graph', {}).get('extends_base_graph') for c in inputs)
            or any(isinstance(i, dict) and i.get('extends_base_graph') for i in (c.get('overlap_index') for c in inputs))):
        return 'inherited_graph'
    if len(set(input_ids)) < 2:
        return 'insufficient_extract_units'
    if ({c['contributor_id'] for c in inputs} != set(input_ids)
            or any(c['kind'] != 'unit' for c in inputs)):
        return 'requires_direct_extract_inputs'
    records = [artifacts[node_id] for node_id in input_ids]
    if any(r.get('inherited_grants') for r in records):
        return 'inherited_graph'
    if any(r.get('artifact_status') != 'committed' or r.get('graph_kind') != 'node_delta'
           or r.get('status') not in {'completed', 'completed_with_gaps'}
           or r.get('coverage') == 'incomplete' for r in records):
        return 'incomplete_extract_inputs'
    attempts = {r.get('source_attempt') for r in records}
    if None in attempts or len(attempts) != 1:
        return 'mixed_extraction_batches'
    return None


def merge_with_overlap_review(config, node, policy, inputs, input_ids, artifacts, *, inherited,
                              source_namespace, model_api_key, check_cancelled, deadline, on_model_call=None,
                              load_overlap_index=None):
    if node.max_run_seconds is not None:
        deadline = min(deadline, time.monotonic() + node.max_run_seconds)
    effective = policy
    local_inputs = inputs
    reason = overlap_review_skip_reason(inputs, input_ids, artifacts, inherited)
    fallback = None
    patterns = []
    pattern_summary = {}
    proposal_checks = []
    result = {'status': 'skipped', 'reason': reason, 'model_calls': 0,
              'selected_pattern_ids': [], 'decisions': [], 'reviewed_patterns': 0, 'omitted_patterns': 0}
    check_cancelled()
    if reason is None:
        # The reviewer sees projection descriptors. Proposal checks and output
        # materialization use the frozen instances locally.
        indexed_inputs = [{'contributor_id': c['contributor_id'], 'kind': c['kind'],
                           'source_namespace': source_namespace,
                           'overlap_index': load_overlap_index(c) if load_overlap_index else c.get('overlap_index')}
                          for c in inputs]
        analysis = analyze_indexed_overlap_patterns(indexed_inputs, policy)
        patterns = annotate_relation_meanings(analysis.patterns, inputs)
        pattern_summary = analysis.summary
        check_cancelled()
        if not patterns and pattern_summary.get('reason'):
            result['reason'] = pattern_summary['reason']
        else:
            result = review_overlap_patterns(config, patterns, api_key=model_api_key,
                                             check_cancelled=check_cancelled, on_model_call=on_model_call,
                                             max_calls=node.max_calls,
                                             remaining_seconds=deadline - time.monotonic())
            check_cancelled()
            try:
                effective = apply_overlap_decisions(policy, analysis, result['selected_pattern_ids'])
                if any(d['action'] == 'propose' for d in result['decisions']):
                    from .projection_proposals import attach_runtime_grants, verify_projection_proposals
                    local_inputs = attach_runtime_grants(project_checkpoint_inputs(inputs, source_namespace),
                                                         config, artifacts)
                    effective, proposal_checks = verify_projection_proposals(
                        effective, local_inputs, patterns, result['decisions'])
                    check_cancelled()
                    from .overlap_feedback import reconsider_source_selections
                    effective, proposal_checks = reconsider_source_selections(
                        config, node, local_inputs, patterns, result, effective, proposal_checks,
                        model_api_key=model_api_key, check_cancelled=check_cancelled, on_model_call=on_model_call,
                        deadline=deadline)
            except LightGraphError as exc:
                if exc.code not in {'invalid_overlap_pattern_decision', 'overlap_pattern_policy_conflict'}:
                    raise
                fallback = exc.code
            except ValueError:
                fallback = 'overlap_pattern_policy_conflict'

    access = access_fingerprint(config)
    try:
        materialized_inputs = local_inputs if effective.verified_projection_rules else inputs
        merged = merge_construction_graphs(materialized_inputs, target_access_fingerprint=access, merge_policy=effective)
    except LightGraphError as exc:
        if effective == policy or exc.code in {'run_cancelled', 'input_dependency_changed'}:
            raise
        fallback = exc.code
    else:
        if effective != policy and merged.get('conflict_isolation', {}).get('coverage') == 'incomplete':
            fallback = 'merge_requires_isolation'
    if fallback and effective != policy:
        # A proposed rule cannot quarantine source data or override endpoint contracts.
        # Re-run the configured policy, which must itself pass the normal merger checks.
        check_cancelled()
        effective = policy
        merged = merge_construction_graphs(inputs, target_access_fingerprint=access, merge_policy=effective)
    check_cancelled()
    applied = merged.get('fusion_diagnostics', {}).get('model_verified_projections', {})
    relation_checks = verify_relation_proposals(merged, patterns, result['decisions'])
    check_cancelled()
    applied_by_pattern = applied.get('applied_by_pattern', {}) if not fallback else {}
    for row in proposal_checks:
        impact = applied_by_pattern.get(row['pattern_id'], {})
        row['applied_groups'] = impact.get('applied_groups', 0)
        row['collapsed_nodes'] = impact.get('collapsed_nodes', 0)
        row['not_applied_by_reason'] = (applied.get('not_applied_by_pattern', {}).get(row['pattern_id'], {})
                                        if not fallback else {'merge_fallback': row['verified_groups']})
    summary = {
        'status': 'fallback' if fallback else result['status'],
        'reason': fallback or result.get('reason'),
        **{field: pattern_summary[field] for field in ('missing_index_contributors', 'incompatible_index_contributors')
           if pattern_summary.get(field)},
        'pattern_count': len(patterns),
        'eligible_patterns': pattern_summary.get('eligible_patterns', 0),
        'analysis_omitted_patterns': pattern_summary.get('omitted_patterns', 0),
        'model_omitted_patterns': result['omitted_patterns'],
        'oversized_patterns': len(result.get('oversized_pattern_ids', [])),
        'submitted_patterns': result.get('submitted_patterns', result['reviewed_patterns']),
        'failed_patterns': result.get('failed_patterns', 0),
        'batch_count': len(result.get('batches', [])),
        'retry_batches': result.get('retry_batches', 0),
        'expanded_output_retries': result.get('expanded_output_retries', 0),
        'recovered_patterns': result.get('recovered_patterns', 0),
        'truncated_batches': result.get('truncated_batches', 0),
        'reviewed_patterns': result['reviewed_patterns'],
        'omitted_patterns': result['omitted_patterns'] + pattern_summary.get('omitted_patterns', 0),
        'truncated': bool(pattern_summary.get('truncated')
                          or 0 < result.get('submitted_patterns', 0) < len(patterns)
                          or result.get('reason') == 'model_prompt_budget_exhausted'),
        'selected_patterns': len(result['selected_pattern_ids']),
        'proposed_patterns': sum(d['action'] == 'propose' for d in result['decisions']),
        'verified_proposal_groups': sum(row['verified_groups'] for row in proposal_checks),
        'applied_proposal_groups': sum(row['applied_groups'] for row in proposal_checks),
        'proposal_collapsed_nodes': sum(row['collapsed_nodes'] for row in proposal_checks),
        'proposed_relation_patterns': len(relation_checks),
        'verified_relation_facts': sum(row['confirmed_fact_groups'] for row in relation_checks),
        'grouped_relation_edges': sum(row['grouped_output_edges'] for row in relation_checks),
        'added_entity_rules': len(effective.fusion_policy.entity_rules) - len(policy.fusion_policy.entity_rules),
        'model_calls': result['model_calls'],
    }
    if history := result.get('source_selection_review'):
        summary['source_selection_reconsidered_patterns'] = history['review']['reviewed_patterns']
        summary['source_selection_corrected_patterns'] = sum(
            row['pattern_id'] in history['prior'] and row['verified_groups'] > 0 for row in proposal_checks)
    report = {
        'format_version': 1, 'summary': summary, 'patterns': patterns, 'pattern_summary': pattern_summary,
        'model_review': {key: value for key, value in result.items() if key not in {'decisions', 'selected_pattern_ids'}},
        'decisions': result['decisions'], 'selected_pattern_ids': result['selected_pattern_ids'],
        'proposal_checks': proposal_checks,
        'relation_proposal_checks': relation_checks,
        'upstream_artifacts': {key: artifacts[key]['artifact_id'] for key in input_ids},
        'configured_merge_policy': policy.model_dump(mode='json'),
        'effective_merge_policy': effective.model_dump(mode='json'),
    }
    return merged, report

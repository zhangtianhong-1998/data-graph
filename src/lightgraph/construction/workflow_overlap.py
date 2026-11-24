"""Run one bounded overlap review for sibling extract checkpoints in a single batch."""
from __future__ import annotations

import time

from lightgraph.errors import LightGraphError
from .access import access_fingerprint
from .merger import merge_construction_graphs
from .overlap_patterns import analyze_overlap_patterns, apply_overlap_decisions
from .overlap_review import review_overlap_patterns


def overlap_review_skip_reason(inputs, input_ids, artifacts, inherited):
    if inherited or any(c['graph'].get('extends_base_graph') for c in inputs):
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
                              source_namespace, model_api_key, check_cancelled, deadline, on_model_call=None):
    if node.max_run_seconds is not None:
        deadline = min(deadline, time.monotonic() + node.max_run_seconds)
    effective = policy
    reason = overlap_review_skip_reason(inputs, input_ids, artifacts, inherited)
    fallback = None
    patterns = []
    pattern_summary = {}
    result = {'status': 'skipped', 'reason': reason, 'model_calls': 0,
              'selected_pattern_ids': [], 'decisions': [], 'reviewed_patterns': 0, 'omitted_patterns': 0}
    check_cancelled()
    if reason is None:
        analysis = analyze_overlap_patterns([{**c, 'source_namespace': source_namespace} for c in inputs], policy)
        patterns, pattern_summary = analysis.patterns, analysis.summary
        check_cancelled()
        if not patterns and pattern_summary.get('reason'):
            result['reason'] = pattern_summary['reason']
        elif node.max_calls == 0:
            result.update(reason='model_call_budget_exhausted', omitted_patterns=len(patterns))
        else:
            result = review_overlap_patterns(config, patterns, api_key=model_api_key,
                                             check_cancelled=check_cancelled, on_model_call=on_model_call,
                                             remaining_seconds=deadline - time.monotonic())
            check_cancelled()
            try:
                effective = apply_overlap_decisions(policy, analysis, result['selected_pattern_ids'])
            except LightGraphError as exc:
                if exc.code not in {'invalid_overlap_pattern_decision', 'overlap_pattern_policy_conflict'}:
                    raise
                fallback = exc.code
            except ValueError:
                fallback = 'overlap_pattern_policy_conflict'

    access = access_fingerprint(config)
    try:
        merged = merge_construction_graphs(inputs, target_access_fingerprint=access, merge_policy=effective)
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
    summary = {
        'status': 'fallback' if fallback else result['status'],
        'reason': fallback or result.get('reason'),
        'pattern_count': len(patterns),
        'reviewed_patterns': result['reviewed_patterns'],
        'omitted_patterns': result['omitted_patterns'] + pattern_summary.get('omitted_patterns', 0),
        'truncated': bool(pattern_summary.get('truncated')
                          or 0 < result.get('submitted_patterns', 0) < len(patterns)
                          or result.get('reason') == 'model_prompt_budget_exhausted'),
        'selected_patterns': len(result['selected_pattern_ids']),
        'added_entity_rules': len(effective.fusion_policy.entity_rules) - len(policy.fusion_policy.entity_rules),
        'model_calls': result['model_calls'],
    }
    report = {
        'format_version': 1, 'summary': summary, 'patterns': patterns, 'pattern_summary': pattern_summary,
        'model_review': {key: value for key, value in result.items() if key not in {'decisions', 'selected_pattern_ids'}},
        'decisions': result['decisions'], 'selected_pattern_ids': result['selected_pattern_ids'],
        'upstream_artifacts': {key: artifacts[key]['artifact_id'] for key in input_ids},
        'configured_merge_policy': policy.model_dump(mode='json'),
        'effective_merge_policy': effective.model_dump(mode='json'),
    }
    return merged, report

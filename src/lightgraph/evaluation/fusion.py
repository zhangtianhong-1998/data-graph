"""Keep fusion measurements, independent judgments and missing evidence distinct."""
from collections import Counter
import hashlib
from pathlib import Path

from lightgraph.contracts import digest


def analysis_code_fingerprint():
    """Identify the code doing this evaluation, never the archived run's unknown code."""
    package = Path(__file__).resolve().parents[1]
    files = [(str(path.relative_to(package)), path) for path in package.rglob('*.py')
             if 'tests' not in path.relative_to(package).parts]
    scripts = package.parent.parent / 'scripts'
    files.extend(('scripts/' + path.name, path) for path in scripts.glob('*.py')
                 if path.name.startswith(('fusion_', 'audit_fusion_', 'audit_checkpoint_')))
    hashes = {name: hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
              for name, path in sorted(files)}
    return {'basis': 'evaluation_first_party_python_lf', 'sha256': digest(hashes), 'files': len(hashes)}


def pair_metrics(expected_and_verdicts):
    """Unknown truth and unavailable outputs have their own denominators."""
    rows = list(expected_and_verdicts)
    counts = Counter(verdict for _, verdict in rows)
    truth = Counter(expected for expected, _ in rows)
    known = truth['together'] + truth['separate']
    assessed = sum(counts[k] for k in ('tp', 'fp', 'fn', 'tn'))
    if counts['fp'] or counts['fn']:
        status = 'failed'
    elif counts['unverified_output']:
        status = 'incomplete'
    elif not known:
        status = 'not_assessed'
    elif not truth['together']:
        status = 'negative_controls_only'
    else:
        status = 'passed_on_labeled_pairs'
    return {
        'denominators': {'all_pairs': len(rows), 'known_positive_pairs': truth['together'],
                         'known_negative_pairs': truth['separate'], 'unknown_truth_pairs': truth['unknown'],
                         'assessable_known_pairs': assessed, 'unverified_known_outputs': counts['unverified_output'],
                         'precision': counts['tp'] + counts['fp'], 'recall': counts['tp'] + counts['fn']},
        'known_truth_coverage': assessed / known if known else None,
        'positive_output_coverage': (counts['tp'] + counts['fn']) / truth['together'] if truth['together'] else None,
        'confirmed_positive_recall_lower_bound': counts['tp'] / truth['together'] if truth['together'] else None,
        'business_acceptance': {'status': status, 'basis': 'supplied_pairs_only',
                                'known_positive_zero_effect': bool(truth['together'] and not counts['tp']),
                                'end_to_end_acceptance': 'not_measured'},
    }


def repeated_zero_positive_check(pair_results):
    """Two observed zero-positive results require a success-path audit, not a passing score."""
    positives = [row for row in pair_results if row['expected'] == 'together']
    complete = bool(positives) and all(row['before'] in {'together', 'separate'}
                                      and row['after'] in {'together', 'separate'} for row in positives)
    repeated = complete and all(row['before'] == row['after'] == 'separate' for row in positives)
    return {'status': 'required' if repeated else 'not_triggered' if complete else 'insufficient_evidence',
            'basis': 'two_compared_commits_same_labeled_positive_pairs', 'known_positive_pairs': len(positives),
            'before_merged_pairs': sum(row['before'] == 'together' for row in positives),
            'after_merged_pairs': sum(row['after'] == 'together' for row in positives),
            'checks_required': ['candidate_discovery', 'correct_binding', 'identity_validation',
                                'execution', 'committed_membership', 'query_visibility'] if repeated else [],
            'production_path_evidence': 'not_supplied',
            'note': 'Two compared commits do not establish chronological consecutive experiments.'}


def fusion_measurements(report):
    """Describe what an archived-checkpoint census actually measured; preserve legacy fields."""
    stages = report.get('stages', {})
    committed = stages.get(report.get('node_id'), {})
    units = [stage for stage in stages.values() if stage.get('kind') == 'unit']
    upstream = report.get('upstream_artifacts', [])
    complete = (bool(upstream) and len(units) == len(upstream)
                and {s['artifact_id'] for s in units} == set(upstream) and bool(committed.get('rows')))
    totals = {key: sum(stage['rows'][key] for stage in units) for key in ('nodes', 'edges')} if complete else {}
    saved = (report.get('historical_pattern_review') or {}).get('summary') or {}
    fields = {
        'model_suggested_entity_patterns': ('proposed_patterns', 'patterns'),
        'validated_entity_groups': ('verified_proposal_groups', 'groups'),
        'executed_entity_groups': ('applied_proposal_groups', 'groups'),
        'proposal_execution_node_reduction': ('proposal_collapsed_nodes', 'nodes'),
        'model_suggested_relation_patterns': ('proposed_relation_patterns', 'patterns'),
        'validated_relation_facts': ('verified_relation_facts', 'facts'),
        'grouped_relation_edges': ('grouped_relation_edges', 'raw_edges'),
        'type_rules_added_to_policy': ('added_entity_rules', 'rules'),
    }
    funnel = {name: {'value': saved.get(field), 'unit': unit, 'source_field': field,
                     'status': 'recorded' if field in saved else 'not_recorded',
                     'basis': 'archived_merge_review', 'stage': 'merge_review',
                     'denominator': None, 'denominator_status': 'not_recorded_at_matching_unit'}
              for name, (field, unit) in fields.items()}
    return {
        'format_version': 1,
        'provenance': {'run_id': report.get('run_id'), 'artifact_id': report.get('artifact_id'),
                       'analysis_code': analysis_code_fingerprint(), 'archived_execution_code': 'not_recorded',
                       'archived_input_generation': {'draft_origin': 'not_recorded', 'model_origin': 'not_recorded',
                                                     'support_evidence_origin': 'not_recorded'},
                       'input_checkpoints': [{k: s[k] for k in ('node_id', 'artifact_id', 'graph_digest',
                            'checkpoint_run_id', 'input_digest') if k in s} for s in units],
                       'source_snapshot': 'not_established_by_graph_digest'},
        'extraction_effect_this_experiment': {'status': 'not_applicable', 'value': None, 'unit': 'nodes',
                                             'stage': 'extraction', 'reason': 'frozen_checkpoints_no_new_extraction'},
        'committed_input_output_delta': {
            'status': 'measured' if complete else 'incomplete', 'stage': 'committed_merge',
            'basis': 'input_checkpoint_occurrences_minus_committed_records',
            'input_denominators': totals,
            'nodes_removed': totals['nodes'] - committed['rows']['nodes'] if complete else None,
            'raw_edges_removed': totals['edges'] - committed['rows']['edges'] if complete else None,
            'attribution': 'all_committed_merge_operations_not_model_only'},
        'model_funnel': funnel,
        'final_committed_change_vs_control': {'status': 'not_measured', 'value': None,
                                              'reason': 'requires_two_committed_merges_with_same_inputs'},
        'hypothetical_reductions': {name: {'basis': 'unpublished_hypothesis', 'stage': 'census',
                                         'nodes': stage.get('physical_hypothesis', {}),
                                         'denominator': stage.get('rows', {}).get('nodes')}
                                   for name, stage in stages.items()},
        'simulation_deltas': {name: {'basis': 'in_memory_simulation_not_committed', 'stage': 'simulation',
                                    'status': item.get('status'), 'delta': item.get('delta'),
                                    'denominator': committed.get('rows')}
                              for name, item in report.get('scenarios', {}).items()},
        'business_acceptance': {'status': 'not_scored', 'reason': 'independent_positive_negative_truth_required'},
        'limitations': ['Hypothesis variants overlap and must not be added together.',
                        'Model suggestions and validated groups do not prove a committed graph change.',
                        'A completed census or repeatable replay does not establish business correctness.',
                        'Checkpoint contents alone do not prove real-model extraction or production-writer provenance.'],
    }

"""A completed replay must not turn zero positive effect or unknown truth into acceptance."""
from pathlib import Path

from lightgraph.evaluation.fusion import fusion_measurements, pair_metrics


def test_unknown_truth_and_unverified_outputs_keep_distinct_denominators():
    result = pair_metrics([('together', 'tp'), ('together', 'unverified_output'),
                           ('separate', 'tn'), ('unknown', 'unknown_truth')])
    assert result['denominators'] == {
        'all_pairs': 4, 'known_positive_pairs': 2, 'known_negative_pairs': 1, 'unknown_truth_pairs': 1,
        'assessable_known_pairs': 2, 'unverified_known_outputs': 1, 'precision': 1, 'recall': 1}
    assert result['known_truth_coverage'] == 2 / 3
    assert result['positive_output_coverage'] == result['confirmed_positive_recall_lower_bound'] == .5
    assert result['business_acceptance']['status'] == 'incomplete'
    assert pair_metrics([('unknown', 'unknown_truth')])['business_acceptance']['status'] == 'not_assessed'


def test_two_zero_positive_commits_fail_even_when_sentinel_and_unknown_pairs_stay_separate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_census_score import compare_input_pairs

    truth = {'run_id': 'first', 'artifact_id': 'merged', 'pairs': []}
    mapping = {}
    for expected, count, stratum in [('together', 50, 'positive'), ('separate', 1, 'negative'), ('unknown', 22, 'unknown')]:
        for i in range(count):
            left, right = ('a', f'{stratum}-{i}'), ('b', f'{stratum}-{i}')
            truth['pairs'].append({'kind': 'input_entity', 'left': list(left), 'right': list(right),
                                   'expected': expected, 'stratum': stratum, 'evidence_ref': 'independent-source'})
            mapping[left], mapping[right] = '-'.join(left), '-'.join(right)
    result = compare_input_pairs(truth, truth, mapping, mapping)
    for assessment in result['scenario_assessments']:
        assert assessment['denominators']['known_positive_pairs'] == 50
        assert assessment['denominators']['known_negative_pairs'] == 1
        assert assessment['denominators']['unknown_truth_pairs'] == 22
        assert assessment['business_acceptance']['status'] == 'failed'
        assert assessment['business_acceptance']['known_positive_zero_effect']
    assert result['satisfiability_review']['status'] == 'required'
    assert result['satisfiability_review']['production_path_evidence'] == 'not_supplied'
    assert result['transition_counts']['unknown_truth'] == 22
    assert all(row['tn'] == 0 for row in result['strata'] if row['stratum'] == 'unknown')


def test_frozen_census_separates_actual_hypothetical_proposed_verified_and_committed(monkeypatch):
    monkeypatch.setattr('lightgraph.evaluation.fusion.analysis_code_fingerprint', lambda: {'sha256': 'code'})
    report = {'run_id': 'r', 'artifact_id': 'm', 'node_id': 'merge', 'upstream_artifacts': ['a'], 'stages': {
        'unit': {'node_id': 'unit', 'kind': 'unit', 'artifact_id': 'a', 'graph_digest': 'source',
                 'rows': {'nodes': 10, 'edges': 15}, 'physical_hypothesis': {'within_only': 4, 'joint': 4}},
        'merge': {'kind': 'committed', 'artifact_id': 'm', 'rows': {'nodes': 8, 'edges': 14}}},
        'historical_pattern_review': {'summary': {'proposed_patterns': 7, 'verified_proposal_groups': 3,
                                                 'applied_proposal_groups': 1, 'proposal_collapsed_nodes': 1}},
        'scenarios': {'exploratory': {'status': 'completed', 'delta': {'nodes_removed': 4}}}}
    result = fusion_measurements(report)
    assert result['extraction_effect_this_experiment']['status'] == 'not_applicable'
    assert result['extraction_effect_this_experiment']['value'] is None
    assert result['committed_input_output_delta']['nodes_removed'] == 2
    assert result['model_funnel']['model_suggested_entity_patterns']['value'] == 7
    assert result['model_funnel']['validated_entity_groups']['value'] == 3
    assert result['model_funnel']['executed_entity_groups']['value'] == 1
    assert result['final_committed_change_vs_control']['status'] == 'not_measured'
    assert result['hypothetical_reductions']['unit']['nodes']['within_only'] == 4
    assert result['simulation_deltas']['exploratory']['basis'] == 'in_memory_simulation_not_committed'
    assert result['business_acceptance']['status'] == 'not_scored'
    # Legacy reports can still be evaluated, but missing review fields are unknown, never zero.
    del report['historical_pattern_review']
    legacy = fusion_measurements(report)
    assert all(metric['value'] is None and metric['status'] == 'not_recorded'
               for metric in legacy['model_funnel'].values())
    assert legacy['provenance']['archived_execution_code'] == 'not_recorded'

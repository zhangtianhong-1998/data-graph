from copy import deepcopy
from pathlib import Path

import pytest

from lightgraph.construction import merger
from lightgraph.construction.graph_storage import _graph_digest
from lightgraph.construction.reviewed_keys import reviewed_observation_digest
from lightgraph.tests.test_reviewed_key_pairs import _inputs, _policy


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    import fusion_census_replay
    return fusion_census_replay


def setup_case(units=None):
    units = _inputs() if units is None else units
    graph = merger.merge_construction_graphs(units, merge_policy={})
    inputs = [{'node_id': unit['contributor_id'], 'artifact_id': 'art_' + unit['contributor_id'],
               'graph_digest': _graph_digest(unit['graph'])} for unit in units]
    target = {'artifact_id': 'merge_art', 'graph_digest': _graph_digest(graph), 'access_fingerprint': None,
              'upstream_artifacts': [p['artifact_id'] for p in inputs]}
    run = {'project_id': 'p', 'run_id': 'r'}
    case = {'inputs': units, 'policy': {}, 'parents': inputs, 'committed': graph,
            'run': run, 'node_id': 'merge_1', 'target': target}
    files = []
    for code, name in [('W7', 'West'), ('B', 'B')]:
        rule, = _policy(units)['reviewed_key_pairs']
        rule.update(approval_ref='simulation_only', observation_digests=[reviewed_observation_digest('text', code, name)])
        files.append({**run, 'node_id': 'merge_1', 'version': 1, 'mode': 'simulation_only',
                      'merge_artifact_id': 'merge_art', 'upstream_artifacts': target['upstream_artifacts'],
                      'simulated_count': 1, 'simulated_key_pairs': [rule]})
    return case, files


def test_union_replays_with_negative_controls_then_all_observations(module):
    case, docs = setup_case()
    baseline = module.prepare_replay_baseline(**{k: case[k] for k in ('inputs', 'policy', 'target', 'committed')})
    one = module.run_joint_replay(**case, simulation_paths=docs[:1], baseline=baseline)
    assert one['status'] == 'completed'
    assert one['delta']['nodes_removed'] == 1
    original_codes = {n['id']: n['identity']['code'] for n in case['committed']['nodes']}
    assigned = {code: {new for old, new in one['node_mapping'].items() if original_codes[old] == code}
                for code in ('W7', 'B')}
    assert len(assigned['W7']) == 1 and len(assigned['B']) == 2 and None not in assigned['W7'] | assigned['B']
    union = module.run_joint_replay(**case, simulation_paths=docs, baseline=baseline)
    assert union == module.run_joint_replay(**case, simulation_paths=docs[::-1], baseline=baseline)
    assert union['status'] == 'completed'
    assert union['delta']['nodes_removed'] == 2
    assert union['delta']['raw_edges_removed'] == 0
    assert union['delta']['canvas_lines_removed'] == 0
    assert union['simulated']['unverified_input_edge_occurrences'] == 0
    assert union['rows_match_committed'] == {'nodes': True, 'edges': True}


def test_no_scenario_stale_baseline_and_conflicting_claims_are_not_measurements(module):
    case, docs = setup_case()
    assert module.run_joint_replay(**case, simulation_paths=[])['status'] == 'not_run_no_simulations'
    conflict = module.run_joint_replay(**case, simulation_paths=[docs[0], docs[0]])
    assert conflict['status'] == 'blocked_conflicting_claims' and len(conflict['conflicts']) == 2
    with pytest.raises(ValueError):
        module.run_joint_replay(**case, simulation_paths=[{**docs[0], 'upstream_artifacts': ['other']}])
    case['committed'] = deepcopy(case['committed'])
    case['committed']['nodes'][0]['label'] = 'drift'
    mismatch = module.run_joint_replay(**case, simulation_paths=docs)
    assert mismatch['status'] == 'baseline_mismatch' and mismatch['rows_match_committed']['nodes'] is False
    assert 'delta' not in mismatch


def test_preexisting_claim_and_production_limit(module):
    case, docs = setup_case()
    case['policy'] = _policy(case['inputs'])
    assert module.run_joint_replay(**case, simulation_paths=docs)['status'] == 'blocked_conflicting_claims'
    case['policy'] = {}
    batch = [deepcopy(docs[0]) for _ in range(65)]
    for i, doc in enumerate(batch):
        doc['simulated_key_pairs'][0]['observation_digests'] = [reviewed_observation_digest('text', i, 'scope')]
    assert module.run_joint_replay(**case, simulation_paths=batch)['status'] == 'blocked_policy_rule_limit'


def test_baseline_member_ids_are_owner_qualified_splits_and_missing_stay_unknown(module):
    old = {'west_null': {('west', 'shared')}, 'east_null': {('east', 'shared')},
           'prior_fold': {('west', 'one'), ('east', 'two')}, 'missing': {('west', 'gone')}}
    new = {'out1': old['west_null'], 'out2': old['east_null'],
           'out3': {('west', 'one')}, 'out4': {('east', 'two')}}
    mapping, unresolved = module._project_nodes(old, new)
    assert mapping == {'west_null': 'out1', 'east_null': 'out2', 'prior_fold': None, 'missing': None}
    assert unresolved == {'prior_fold': 'split_or_ambiguous_member', 'missing': 'missing_member'}


def test_qualified_inputs_follow_actual_id_changes_and_access_rejection(module):
    units = _inputs()
    for unit in units:
        graph = unit['graph']
        graph['nodes'][0]['id'] = 'shared_in_source'
        graph['edges'][0]['source'] = 'shared_in_source'
    policy = _policy(units)
    target = {'access_fingerprint': 'same-grants'}
    baseline = module._replay(units, {}, target)
    before = baseline.report['input_node_mapping']
    assert before['west']['shared_in_source'] != before['east']['shared_in_source']
    assert all(before[cid]['shared_in_source'] != 'shared_in_source' for cid in ('east', 'west'))
    merged = module._replay(units, policy, target)
    after = merged.report['input_node_mapping']
    assert after['west']['shared_in_source'] == after['east']['shared_in_source'] is not None
    assert merged.report['input_node_mapping_counts'] == {
        'input_occurrences': 4, 'unique_qualified_ids': 4, 'verified': 4, 'unverified': 0,
        'unverified_occurrences': 0, 'duplicate_qualified_ids': 0}
    # Observer does not alter any persisted row.
    raw = merger.merge_construction_graphs(units, merge_policy=policy, target_access_fingerprint='same-grants')
    assert merged.report['row_digests'] == module.audit._graph_row_digests(raw)
    units[1]['graph']['nodes'][0]['supports'][0]['required_scope_refs'] = ['tenant-east']
    invalid = module._replay(units, _policy(units), target).report['input_node_mapping']
    assert invalid['east']['shared_in_source'] != invalid['west']['shared_in_source']


def test_lineage_tracks_explicit_fusion_rekey_prior_to_review(module):
    from lightgraph.tests.test_cross_source_fusion import fusion_rule, source_graph
    one = source_graph('LocalProduct', 'code', 'AB', 'one', 'Example', 'p', 'x')
    two = source_graph('ERPProduct', 'sku', 'AB', 'two', 'Example', 'p', 'x')
    units = [{'kind': 'unit', 'contributor_id': 'one', 'graph': one},
             {'kind': 'unit', 'contributor_id': 'two', 'graph': two}]
    rules = [fusion_rule('LocalProduct', 'code', 'p'), fusion_rule('ERPProduct', 'sku', 'p')]
    result = module._replay(units, {'fusion_policy': {'entity_rules': rules}}, {'access_fingerprint': None})
    mapping = result.report['input_node_mapping']
    assert mapping['one']['one_1'] == mapping['two']['two_1'] is not None
    assert mapping['one']['one_2'] == mapping['two']['two_2'] != mapping['one']['one_1']
    assert result.report['input_node_mapping_counts']['verified'] == 4


def test_member_folds_create_one_verified_relation_assertion_without_dropping_raw_edges(module):
    from lightgraph.tests.test_fusion_census_edges import FAMILIES, _verified_edge
    units = _inputs()
    for unit, relation in zip(units, FAMILIES[0]['types'][:2], strict=True):
        graph = unit['graph']
        before = graph['edges'][0]
        edge = _verified_edge(before['id'], relation, [[1, 1]])
        edge.update(source=before['source'], target=before['target'])
        graph['edges'] = [edge]
        graph['definitions']['relations'][0]['type'] = relation
    case, docs = setup_case(units)
    result = module.run_joint_replay(**case, simulation_paths=docs, relation_families=FAMILIES)
    assert result['status'] == 'completed'
    assert result['delta']['nodes_removed'] == 2
    assert result['delta']['raw_edges_removed'] == 0
    assert result['delta']['distinct_assertions_removed'] == 1
    assert result['simulated']['relation_facts']['confirmed_reduction'] == 1
    assert len(result['simulated']['confirmed_relation_groups'][0]['original_edge_ids']) == 2
    fact, = result['simulated']['confirmed_relation_groups']
    baseline_ids = {e['id'] for e in case['committed']['edges']}
    assert result['unverified_edge_mapping_counts'] == {}
    assert result['relation_scoring']['relation_fact'] == dict.fromkeys(baseline_ids, fact['fact_id'])
    projected = result['relation_scoring']['canvas_line']
    assert set(projected) == baseline_ids and len(set(projected.values())) == 1 and None not in projected.values()
    base = module.prepare_replay_baseline(**{k: case[k] for k in ('inputs', 'policy', 'target', 'committed')})
    with pytest.raises(ValueError):
        module.run_joint_replay(**case, simulation_paths=docs, relation_families=FAMILIES, baseline=base)


def test_raw_edge_mapping_requires_verified_ownership_even_across_raw_id_collisions(module):
    raw = {'id': 'same', 'type': 'R', 'source': 'a', 'target': 'b'}
    units = [{'kind': 'unit', 'contributor_id': owner, 'graph': {'edges': [raw]}} for owner in ('east', 'west')]
    def rewritten(owner, eid):
        origin = {'edge_id': 'same', 'type': 'R', 'source': 'a', 'target': 'b', 'input_kind': 'unit', 'contributor_id': owner}
        return {**raw, 'id': eid, 'supports': [{'endpoint_origins': [origin], 'contributor_id': owner}]}
    edges = [rewritten('east', 'out-east'), rewritten('west', 'out-west')]
    def mapping():
        coverage = module._EdgeLineage({}, set())
        module.audit.edge_lineage(units, {'edges': edges}, set(), observe_input_edge=coverage.capture)
        return coverage
    left = (('unit', 'east'), ('same', 'R', 'a', 'b'))
    right = (('unit', 'west'), ('same', 'R', 'a', 'b'))
    assert mapping().by_input == {left: 'out-east', right: 'out-west'}
    edges.append(rewritten('east', 'out-east-another'))
    ambiguous = mapping()
    assert ambiguous.by_input == {left: None, right: 'out-west'}
    assert ambiguous.uncertain_outputs == {'out-east', 'out-east-another'}
    # A third unqualified occurrence might come from either contributor.
    edges.append({**raw, 'supports': [{}]})
    assert mapping().by_input == {left: None, right: None}


def test_cross_scenario_raw_edge_membership_blocks_unresolved_and_split_inputs(module):
    def refs(values, uncertain=(), missing=0):
        return module._EdgeLineage(values, set(uncertain), missing)
    before = refs({'r1': 'old1', 'r2': 'old1', 'r3': 'old2', 'r4': 'old3', 'r5': 'old4'}, ['old4'])
    after = refs({'r1': 'new1', 'r2': 'new2', 'r3': None, 'r4': 'new3', 'r5': 'new4'})
    ids = ['old1', 'old2', 'old3', 'old4', 'old5']
    projected, gaps = module._project_edges(ids, before, after)
    assert projected == {'old1': None, 'old2': None, 'old3': 'new3', 'old4': None, 'old5': None}
    assert gaps == {'baseline_edge_split': 1, 'missing_or_ambiguous_simulated_member': 1,
                    'ambiguous_baseline_input': 1, 'no_verified_baseline_member': 1}
    before.unlocated_inputs += 1
    assert module._project_edges(ids, before, after) == (dict.fromkeys(ids), {'unlocated_baseline_input': len(ids)})

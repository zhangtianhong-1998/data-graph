"""Collect pair evidence from two committed censuses and their actual checkpoint files."""
from copy import deepcopy
import hashlib
import subprocess
import sys

import pytest

from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.workflow import save_workflow_state
from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_fusion_committed_comparison import SCRIPTS, invoke, load, save


@pytest.fixture(scope='module')
def archived_batch(tmp_path_factory):
    root = tmp_path_factory.mktemp('pair-evidence')
    store = root / 'output'
    store.mkdir()
    (root / 'runs').mkdir()
    run_ids = ['run_' + digit * 24 for digit in 'ab']
    left = unit('sales', 'Item', 'code', table='transactions')
    right = unit('planning', 'ForecastItem', 'code', table='targets')
    # Same code, label and physical table, observed on another axis of the wide table.
    alternate = deepcopy(left['graph']['nodes'][0])
    alternate.update(id='alternate', type='ServiceCategory', identity={'category': alternate['identity']['code']},
                     identity_types={'category': 'text'})
    alternate['supports'][0]['provenance']['key_columns'] = ['category_id']
    left['graph']['nodes'].append(alternate)
    left['graph']['definitions']['entities'].append({'type': 'ServiceCategory', 'key_names': ['category']})
    records = {u['contributor_id']: commit_checkpoint(store, run_ids[0], u['contributor_id'], u,
               input_digest='frozen') for u in [left, right]}
    inputs = [load_checkpoint(store, rec) for rec in records.values()]
    cards = analyze_overlap_patterns(inputs).patterns
    card, = [c for c in cards if c['kind'] == 'entity'
             and {(m['contributor_id'], m['type']) for m in c['members']} == {
                 ('sales', 'Item'), ('planning', 'ForecastItem')}]
    pid = card['pattern_id']
    selection = [{'member_index': i, 'source_index': m['source_indexes'][0],
                  'role_index': next(j for j, role in enumerate(m['roles']) if role['end'] == 'source')}
                 for i, m in enumerate(card['members'])]
    policy = {'fusion_policy': {}}
    graph = merge_construction_graphs(inputs, merge_policy=policy)
    policy = {'fusion_policy': graph['fusion_policy']}
    review = {'effective_merge_policy': policy, 'upstream_artifacts': {k: r['artifact_id'] for k, r in records.items()},
              'model_review': {'status': 'reviewed', 'model_calls': 1, 'submitted_patterns': 1,
                               'batches': [{'attempt': 1, 'status': 'reviewed', 'pattern_ids': [pid]}]},
              'summary': {'analysis_omitted_patterns': 1}, 'patterns': [card],
              'decisions': [{'pattern_id': pid, 'action': 'propose', 'alignment': selection}],
              'proposal_checks': [{'pattern_id': pid, 'reason': 'cross_table_identity_unverified'}]}
    censuses, runs = [], []
    for rid, mode in zip(run_ids, ('deterministic', 'overlap_review'), strict=True):
        merged = {'kind': 'merged', 'graph': graph}
        if mode == 'overlap_review':
            merged['overlap_review'] = review
        result = commit_checkpoint(store, rid, 'merge_1', merged, input_digest=mode,
                                   upstream_artifacts=list(review['upstream_artifacts'].values()))
        save_workflow_state(store, {'parent_run_id': rid, 'artifacts': {**records, 'merge_1': result},
            'merge_nodes': {'merge_1': {'merge_mode': mode}}, 'plan': {
                'merge_nodes': [{'node_id': 'merge_1'}], 'merge_policy': policy}})
        run_file = root / 'runs' / f'{rid}.json'
        save(run_file, {'run_id': rid, 'project_id': 'example', 'output_path': 'output'})
        folder = root / mode
        invoke('run', run_file, '--output', folder, '--workers', 1, '--committed-only')
        runs.append(run_file)
        censuses.append(folder)
    baseline = load(censuses[0] / 'summary.json')
    truth = root / 'truth.json'
    save(truth, {'run_id': baseline['run_id'], 'artifact_id': baseline['artifact_id'], 'pairs': [
        {'kind': 'input_entity', 'left': ['sales', n], 'right': ['planning', 'planning_0'],
         'expected': expected, 'stratum': 'catalogs', 'evidence_ref': 'fixture@1'}
        for n, expected in [('sales_0', 'together'), ('alternate', 'separate')]]})
    return runs, censuses, truth, pid


def collect(run_file, censuses, truth, output, *, check=True):
    return subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPTS / 'collect_fusion_evidence.py'),
                           '--review-run', str(run_file), '--det-census', str(censuses[0]),
                           '--review-census', str(censuses[1]), '--truth', str(truth), '--output', str(output)],
                          capture_output=True, encoding='utf-8', check=check)


def test_collection_joins_only_exact_archived_projections(archived_batch, tmp_path):
    runs, censuses, truth, pid = archived_batch
    output = tmp_path / 'proof'
    collect(runs[1], censuses, truth, output)
    summary = load(output / 'summary.json')
    assert summary['pairs'] == 2 and summary['distinct_input_members'] == 3
    assert summary['truth_sha256'] == hashlib.sha256(truth.read_bytes()).hexdigest()
    assert summary['collection_model_calls'] == 0 and summary['matched_pair_pattern_checks'] == 1
    assert load(output / 'review-stages.json')['analysis_omitted_patterns'] == 1
    route, = load(output / 'routes.json')
    assert route['pattern_id'] == pid and route['proposal_check']['reason'] == 'cross_table_identity_unverified'
    members = load(output / 'members.json')
    assert members['sales']['alternate']['bindings'][0]['key_columns'] == ['category_id']
    matched, other = load(output / 'pairs.json')
    assert matched['physical_route_result'] == 'both_physical_bindings_selected'
    assert matched['route_matches'][0]['pattern_id'] == pid
    assert matched['local_key_observations_by_pattern'][pid]['status'] == 'observed_all_selected_members'
    assert other['physical_route_result'] == 'no_resolvable_pair_in_archived_entity_cards'
    assert other['route_matches'] == [] and 'local_key_observations_by_pattern' not in other
    for pair in (matched, other):
        before = pair['output_ids']['deterministic']
        assert None not in before and len(set(before)) == 2
        assert before == pair['output_ids']['model_review']
    original_bytes = {p.name: p.read_bytes() for p in output.iterdir()}
    error = collect(runs[1], censuses, truth, output, check=False)
    assert error.returncode != 0 and '请使用新的结果目录' in error.stderr
    assert {p.name: p.read_bytes() for p in output.iterdir()} == original_bytes


def test_collection_rejects_an_unrelated_review_run(archived_batch, tmp_path):
    runs, censuses, truth, _ = archived_batch
    output = tmp_path / 'wrong-archive'
    error = collect(runs[0], censuses, truth, output, check=False)
    assert error.returncode != 0 and '审查运行与第二份普查目录不符' in error.stderr
    assert not output.exists()

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workflow import save_workflow_state
from lightgraph.tests.test_reviewed_key_pairs import _inputs, _policy


SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'


def invoke(*args):
    return subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPTS/'audit_fusion_census.py'), *map(str, args)],
                          capture_output=True, encoding='utf-8', check=True)


def load(path):
    return json.loads(path.read_text('utf-8'))


def save(path, data):
    path.write_text(json.dumps(data), 'utf-8')


@pytest.fixture(scope='module')
def committed_results(tmp_path_factory):
    root = tmp_path_factory.mktemp('two-commits')
    store = root/'output'
    store.mkdir()
    (root/'runs').mkdir()
    inputs = _inputs()
    run_ids = ['run_' + digit * 24 for digit in '12']
    units = {u['contributor_id']: commit_checkpoint(store, run_ids[0], u['contributor_id'], u, input_digest='fixed')
             for u in inputs}
    inputs = [load_checkpoint(store, record) for record in units.values()]
    directories = []
    for rid, policy in zip(run_ids, [{}, _policy(inputs)], strict=True):
        policy = {**policy, 'fusion_policy': {}}
        graph = merge_construction_graphs(inputs, merge_policy=policy)
        merged = commit_checkpoint(store, rid, 'merge_1', {'kind': 'merged', 'graph': graph}, input_digest='merge',
                                   upstream_artifacts=[record['artifact_id'] for record in units.values()])
        save_workflow_state(store, {'parent_run_id': rid, 'artifacts': {**units, 'merge_1': merged},
                                    'merge_nodes': {'merge_1': {'merge_mode': 'deterministic'}},
                                    'plan': {'merge_nodes': [{'node_id': 'merge_1'}], 'merge_policy': policy}})
        run_file = root/'runs'/f'{rid}.json'
        save(run_file, {'run_id': rid, 'project_id': 'example', 'output_path': 'output'})
        directory = root/rid
        invoke('run', run_file, '--output', directory, '--workers', 1, '--committed-only')
        assert not (directory/'hypotheses.json').exists() and not (directory/'exploratory.json').exists()
        assert load(directory/'summary.json')['scenarios'] == {}
        directories.append(directory)
    return directories


def truth_at(directory, expected):
    summary = load(directory/'summary.json')
    return {'run_id': summary['run_id'], 'artifact_id': summary['artifact_id'], 'pairs': [
        {'kind': 'input_entity', 'left': ['west', 'west_1'], 'right': ['east', 'east_1'],
         'expected': expected, 'stratum': 'districts', 'evidence_ref': 'directory@1'},
        {'kind': 'input_entity', 'left': ['west', 'west_2'], 'right': ['east', 'east_2'],
         'expected': 'unknown', 'stratum': 'districts', 'evidence_ref': 'insufficient-records'}]}


@pytest.mark.parametrize(('reverse', 'expected', 'change'), [
    (False, 'together', 'correct_merge'), (False, 'separate', 'new_false_merge'),
    (True, 'separate', 'correct_split'), (True, 'together', 'new_false_split')])
def test_two_committed_runs_score_source_members_in_both_directions(committed_results, tmp_path, reverse, expected, change):
    before, after = reversed(committed_results) if reverse else committed_results
    labels, result = tmp_path/'truth.json', tmp_path/'result.json'
    save(labels, truth_at(before, expected))
    invoke('compare-committed', before, after, '--truth', labels, '--result', result)
    report = load(result)
    assert report['model_calls'] == 0 and report['qualified_input_ids'] == 4
    assert report['transition_counts'][change] == report['transition_counts']['unknown_truth'] == 1
    assert sum(report['transition_counts'].values()) == 2
    assert {r['scenario'] for r in report['strata']} == {'first', 'second'}
    effect = report['measurements']
    assert effect['extraction_effect_this_experiment']['status'] == 'not_applicable'
    assert effect['final_committed_change_vs_control']['nodes_removed'] == (-1 if reverse else 1)
    assert effect['final_committed_change_vs_control']['raw_edges_removed'] == 0
    assert [(p['before'], p['after']) for p in report['pair_results']] == [
        ('together', 'separate') if reverse else ('separate', 'together'), ('separate', 'separate')]


@pytest.mark.parametrize(('fault', 'reason'), [
    ('truth', '裁决清单的运行或产物'), ('upstream', '同一批有序上游检查点'),
    ('artifact', '同一批有序上游检查点'),
    ('unit_missing', '原节点映射未覆盖'), ('input_drift', '同一批有序上游检查点'),
    ('replay', '行摘要'), ('two_copies', '两次独立提交'), ('duplicate', '原节点 ID 重复')])
def test_two_commits_reject_different_inputs_and_unverified_reports(committed_results, tmp_path, fault, reason):
    first, second = committed_results
    other = tmp_path/'comparison'
    shutil.copytree(first if fault == 'two_copies' else second, other)
    labels = truth_at(second if fault == 'truth' else first, 'together')
    if fault in ('upstream', 'artifact'):
        summary = load(other/'summary.json')
        if fault == 'upstream':
            summary['stages']['west']['graph_digest'] = '0' * 64
        else:
            summary['stages']['west']['artifact_id'] = summary['upstream_artifacts'][0] = 'other-artifact'
        save(other/'summary.json', summary)
    if fault == 'duplicate':
        save(other/'unit-1'/'node-ids.json', ['west_1', 'west_1'])
    if fault in ('unit_missing', 'input_drift', 'replay'):
        base = load(other/'baseline.json')
        if fault == 'replay':
            base['row_digests']['nodes'] = '0' * 64
        else:
            winner = base['input_node_mapping']['west'].pop('west_2')
            if fault == 'input_drift':
                base['input_node_mapping']['west']['replacement'] = winner
                ids = other/'unit-1'/'node-ids.json'
                save(ids, ['west_1', 'replacement'])
        save(other/'baseline.json', base)
    save(tmp_path/'truth.json', labels)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        invoke('compare-committed', first, other, '--truth', tmp_path/'truth.json', '--result', tmp_path/'bad.json')
    assert reason in exc.value.stderr
    assert not (tmp_path/'bad.json').exists()


def test_full_census_from_prior_format_compares_without_loading_any_simulation(committed_results, tmp_path):
    first, second = committed_results
    whole = tmp_path/'old-full'
    shutil.copytree(first, whole)
    summary = load(whole/'summary.json')
    summary.pop('committed_only')
    for stage in summary['stages'].values():
        stage.pop('checkpoint_run_id')
        stage.pop('artifact_status')
    summary['scenarios'] = {'exploratory': {'status': 'completed'}}
    save(whole/'summary.json', summary)
    (whole/'exploratory.json').write_text('invalid simulation data', 'utf-8')
    save(tmp_path/'truth.json', truth_at(first, 'together'))
    invoke('compare-committed', whole, second, '--truth', tmp_path/'truth.json', '--result', tmp_path/'result.json')
    assert load(tmp_path/'result.json')['transition_counts']['correct_merge'] == 1


def test_missing_lineage_and_unknown_truth_never_count_as_separation(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from fusion_census_score import compare_input_pairs
    first = {'run_id': 'a', 'artifact_id': 'b'}
    rows = [{'kind': 'input_entity', 'left': ['one', 'id'], 'right': ['two', key], 'expected': expected,
             'stratum': 'test', 'evidence_ref': 'fixture'} for key, expected in (
                 ('id', 'together'), ('partial', 'unknown'), ('stable', 'separate'))]
    before = {('one', 'id'): 'x', ('two', 'id'): 'x', ('two', 'partial'): 'y', ('two', 'stable'): 'y'}
    after = {**before, ('two', 'id'): None, ('two', 'partial'): None}
    report = compare_input_pairs({**first, 'pairs': rows}, first, before, after)
    assert [p['change'] for p in report['pair_results']] == ['unverified_output', 'unknown_truth', 'unchanged']
    assert sum(report['transition_counts'].values()) == 3

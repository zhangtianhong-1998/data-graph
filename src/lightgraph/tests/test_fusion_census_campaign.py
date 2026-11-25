import json
from pathlib import Path
import subprocess
import sys

from lightgraph.construction.checkpoints import commit_checkpoint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workflow import save_workflow_state
from lightgraph.tests.test_reviewed_key_pairs import _inputs, _policy


def test_parallel_archived_census_and_scoring_without_new_publication(tmp_path):
    entry = Path(__file__).resolve().parents[3] / 'scripts' / 'audit_fusion_census.py'
    run_id = 'run_' + 'd' * 24
    units = _inputs()
    reference_rule, = _policy(units)['reviewed_key_pairs']
    # A second independent directory gives the union a genuine unseen pattern.
    for item in _inputs():
        graph = item['graph']
        item['contributor_id'] += '_market'
        graph['definitions']['entities'][0]['type'] = 'Market'
        graph['definitions']['relations'][0].update(type='serves', source_type='Market', target_type='Market')
        for n in graph['nodes']:
            n.update(id='m_'+n['id'], type='Market')
        for e in graph['edges']:
            e.update(id='m_'+e['id'], type='serves', source='m_'+e['source'], target='m_'+e['target'])
        units.append(item)
    store = tmp_path / 'output'
    store.mkdir()
    artifacts = {u['contributor_id']: commit_checkpoint(store, run_id, u['contributor_id'], u, input_digest='source')
                 for u in units}
    graph = merge_construction_graphs(units, merge_policy={'fusion_policy': {}})
    merge = commit_checkpoint(store, run_id, 'merge_1', {'kind': 'merged', 'graph': graph}, input_digest='merge',
                              upstream_artifacts=[r['artifact_id'] for r in artifacts.values()])
    artifacts['merge_1'] = merge
    save_workflow_state(store, {'parent_run_id': run_id, 'artifacts': artifacts,
                        'merge_nodes': {'merge_1': {'merge_mode': 'deterministic'}},
                        'plan': {'merge_nodes': [{'node_id': 'merge_1'}], 'merge_policy': {}}})
    run_file = tmp_path/'runs'/(run_id+'.json')
    run_file.parent.mkdir()
    run_file.write_text(json.dumps({'run_id': run_id, 'project_id': 'demo', 'output_path': 'output'}))
    rule = reference_rule
    rule['approval_ref'] = 'simulation_only'
    sim = tmp_path/'known.json'
    sim.write_text(json.dumps({'version': 1, 'mode': 'simulation_only', 'run_id': run_id, 'project_id': 'demo',
                               'node_id': 'merge_1', 'merge_artifact_id': merge['artifact_id'],
                               'upstream_artifacts': merge['upstream_artifacts'], 'simulated_count': 1,
                               'simulated_key_pairs': [rule]}))
    out = tmp_path/'results'
    def call(*args):
        p = subprocess.run([sys.executable, str(entry), *map(str, args)], capture_output=True, text=True)
        assert p.returncode == 0, (p.stdout, p.stderr)
    call('run', run_file, '--output', out, '--simulation', sim, '--workers', 2)
    def data(name):
        return json.loads((out/name).read_text(encoding='utf8'))
    report = data('summary.json')
    assert report['status'] == 'completed' and len(report['stages']) == 5
    assert report['scenarios']['reference']['delta']['nodes_removed'] == 1
    assert report['scenarios']['exploratory']['delta']['nodes_removed'] == 4
    assert data('hypotheses.json')['hypothesized_pairs'] == 3
    joint = data('exploratory.json')
    assert joint['delta']['raw_edges_removed'] == joint['delta']['net_nodes_outside_attributed_groups'] == 0
    assert sorted(r['explained_net_nodes'] for r in joint['rule_attribution']) == [1, 1, 2]
    assert len(list(store.rglob('checkpoint.json'))) == 5
    left, right = [n['id'] for n in graph['nodes'] if n['identity']['code'] == 'W7' and n.get('original_type', n['type']) == 'District']
    baseline = data('baseline.json')
    raw = [[owner, nid] for owner, mapping in baseline['input_node_mapping'].items()
           for nid, winner in mapping.items() if winner in (left, right)]
    assert len(raw) == 2
    edges = list(data('committed/edges.json')['edge_to_canvas_line'])
    truth = tmp_path/'labels.json'
    truth.write_text(json.dumps({'run_id': run_id, 'artifact_id': merge['artifact_id'], 'pairs': [
        {'kind': 'entity', 'left': left, 'right': right, 'expected': 'together',
         'stratum': 'catalog', 'evidence_ref': 'fixture-catalog-v1'},
        {'kind': 'input_entity', 'left': raw[0], 'right': raw[1], 'expected': 'together',
         'stratum': 'upstream', 'evidence_ref': 'fixture-catalog-v1'},
        {'kind': 'relation_fact', 'left': edges[0], 'right': edges[1], 'expected': 'unknown',
         'stratum': 'relations', 'evidence_ref': 'unresolved-fixture'}]}))
    call('score', out, '--truth', truth, '--result', out/'accuracy.json')
    scored = data('accuracy.json')
    stats = {r['scenario']:r for r in scored['strata'] if r['kind'] == 'entity'}
    origins = [r for r in scored['strata'] if r['kind'] == 'input_entity']
    assert {r['scenario'] for r in origins if r['tp']} == {'reference', 'exploratory'}
    assert sum(r['unknown_truth'] for r in scored['strata'] if r['kind'] == 'relation_fact') == 3
    assert any(row.get('candidate_block_found') for row in scored['positive_entity_candidate_coverage'])
    assert stats['reference']['tp'] == stats['exploratory']['tp'] == stats['committed']['fn'] == 1
    # Exercise the saved alias when JSON keys sort exploratory before reference.
    alias_output = tmp_path/'alias'
    call('run', run_file, '--output', alias_output, '--workers', 1, '--simulation', sim,
         '--exclude-text', 'W7', '--exclude-text', 'B')
    assert json.loads((alias_output/'summary.json').read_text())['scenarios']['exploratory']['status'] == 'same_rules_as_reference'
    call('score', alias_output, '--truth', truth, '--result', alias_output/'accuracy.json')
    alias = json.loads((alias_output/'accuracy.json').read_text())
    assert sum(r['unknown_truth'] for r in alias['strata'] if r['kind'] == 'relation_fact') == 3

"""Run-switching reads retain the full API and only hydrate the selected record."""
import json

from lightgraph.construction.workflow import save_workflow_state
from lightgraph.tests.test_web_api import client, create
from lightgraph.web import execution


def record(rid, **fields):
    return {'run_id': rid, 'project_id': 'alpha', 'status': 'completed', 'stage': 'finished',
            'cleanup_status': 'completed', 'created_at': rid, 'output_path': 'output', **fields}


def test_summary_list_and_background_poll_do_not_hydrate_history(client, monkeypatch):
    create(client)
    api = '/api/projects/alpha/runs'
    manager = client.app.state.manager
    _, directory = manager._paths('alpha')
    old, new = ('run_' + n * 24 for n in ('1', '2'))
    old_record = record(old, workflow=True, report={'issues': ['history-report']}, dependencies={'secret': 'omitted'})
    new_record = record(new, workflow=True, status='running', stage='extracting')
    # Old files restored after service startup need neither migration nor sidecars.
    old_path = directory / (old + '.json')
    execution._save(old_path, old_record)
    manager._save_run(directory / (new + '.json'), new_record)
    reads = []
    original = execution._json_file

    def count(path):
        reads.append(path.name)
        return original(path)

    def unexpected(*a, **kw):
        raise AssertionError('read diagnostics or workflow while listing runs')

    monkeypatch.setattr(execution, '_json_file', count)
    monkeypatch.setattr(execution, 'read_worker_log', unexpected)
    monkeypatch.setattr(execution, '_workbench_workflow', unexpected)
    monkeypatch.setattr(execution, 'read_progress', lambda path, rid: {'stage': 'discovery'} if rid == new else unexpected())
    for _ in range(2):
        rows = client.get(api + '?view=summary').json()['runs']
        assert [row['run_id'] for row in rows] == [new, old]
        assert rows[0]['stage'] == 'discovery'
        assert client.get(api + '/' + new + '?view=summary').json() == rows[0]
        assert all(not set(row) & {'report', 'dependencies', 'worker_diagnostics', 'progress', 'workflow_state'} for row in rows)
    assert reads == [old_path.name]
    execution._save(old_path, dict(old_record, status='failed'))
    assert client.get(api + '/' + old + '?view=summary').json()['status'] == 'failed'
    assert reads == [old_path.name, old_path.name]
    execution._save(old_path, dict(old_record, project_id='beta'))
    assert client.get(api + '/' + old + '?view=summary').status_code >= 400


def test_workbench_reads_recorded_state_and_distinguishes_current_workflow(client, monkeypatch):
    create(client)
    api = '/api/projects/alpha/runs/'
    manager = client.app.state.manager
    project, directory = manager._paths('alpha')
    old, new = ('run_' + n * 24 for n in ('1', '2'))
    edge_totals = {'input_edges': 34_954, 'output_edges': 34_954, 'output_directed_endpoint_pairs': 26_780,
                   'output_multi_edge_pairs': 4_346, 'output_endpoint_pair_excess': 8_174, 'output_self_loop_edges': 2_043}
    for rid, count in ((old, 29_462), (new, 23_958)):
        manager._save_run(directory / (rid + '.json'), record(rid, workflow=True, report={'counts': {'business_nodes': count}}))
        state = {'parent_run_id': rid, 'plan': {'heavy': 'hidden from table'}, 'units': {},
                 'merge_nodes': {'merge_1': {'node_id': 'merge_1', 'status': 'completed', 'merge_mode': 'deterministic',
                                             'nodes': count, 'merge_counts': {'output_nodes': count, 'internal': [1, 2]}}},
                 'artifacts': {'merge_1': {'artifact_id': 'artifact_' + rid, 'graph_kind': 'node_full', 'internal': [1, 2]}},
                 'merge': {'nodes': count}, 'terminal_ids': ['merge_1']}
        if rid == new:
            state['merge_nodes']['merge_1']['merge_counts'].update(edge_totals)
        save_workflow_state(project / 'output', state)

    # The default route retains the previous full payload independently.
    assert client.get(api + old).json()['workflow_state']['plan']['heavy'] == 'hidden from table'
    monkeypatch.setattr(execution, 'read_worker_log', lambda *_: (_ for _ in ()).throw(AssertionError('parsed log')))
    for rid, expected in ((old, 29_462), (new, 23_958), (old, 29_462)):
        selected = client.get(api + rid + '?view=workbench').json()
        assert selected['current_workflow_run_id'] == new
        assert selected['report']['counts']['business_nodes'] == expected
        table = selected['workflow_state']
        assert table['parent_run_id'] == rid and 'plan' not in table
        assert table['merge_nodes']['merge_1']['merge_counts'] == {'output_nodes': expected, **(edge_totals if rid == new else {})}
        assert table['artifacts']['merge_1']['artifact_id'] == 'artifact_' + rid
        assert table['merge_nodes']['merge_1']['model_calls'] == 0

    # Current generation may switch even without changing the historical record.
    manager.workspace.select_output_generation('alpha', old, create=True)
    assert client.get(api + old + '?view=workbench').json()['current_workflow_run_id'] is None
    path = project / 'output' / 'workflow-runs' / (old + '.json')
    archive = json.loads(path.read_text())
    assert archive['merge_nodes']['merge_1'].get('model_calls') is None
    archive['merge_nodes']['merge_1']['merge_counts']['output_nodes'] = 42
    save_workflow_state(project / 'output', archive)
    changed = client.get(api + old + '?view=workbench').json()['workflow_state']
    assert changed['merge_nodes']['merge_1']['merge_counts'] == {'output_nodes': 42}

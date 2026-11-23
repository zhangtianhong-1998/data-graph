"""Open a worker's paged trace through its historical workflow node."""
import pytest

from lightgraph.construction.workflow import save_workflow_state
from lightgraph.runtime.trace import RunTrace, trace_path
from lightgraph.tests.test_web_api import client, create


PARENT_A, PARENT_B, CHILD_A, CHILD_B = ('run_' + x * 24 for x in 'abcd')


def save_parent(client, rid, units=None, merges=None, selected=None, **state_fields):
    manager = client.app.state.manager
    project, record_path = manager._paths('alpha', rid)
    record = {'project_id': 'alpha', 'run_id': rid, 'workflow': True, 'output_path': 'output',
              'created_at': rid, 'status': state_fields.get('status', 'completed')}
    if selected is not None:
        record['workflow_units'] = selected
    manager._save_run(record_path, record)
    state = {'parent_run_id': rid, 'units': units or {}, 'merge_nodes': merges or {}, **state_fields}
    save_workflow_state(project / 'output', state)
    return project / 'output'


def url(parent, node):
    return f'/api/projects/alpha/runs/{parent}/nodes/{node}/trace'


def test_node_trace_reads_only_selected_history_and_pages_worker_events(client):
    create(client)
    create(client, 'beta')
    manager = client.app.state.manager
    _, runs = manager._paths('alpha')
    for parent, child in ((PARENT_A, CHILD_A), (PARENT_B, CHILD_B)):
        save_parent(client, parent, units={'extract_1': {'unit_id': 'extract_1', 'run_id': child}})
        writer = RunTrace(trace_path(runs / (child + '.result.json')))
        writer('model_request', {'input': 'first-' + child})
        writer('runtime_event', {'internal': 'runtime'})
        writer('model_response', {'answer': 'last-' + child})

    for parent, child in ((PARENT_A, CHILD_A), (PARENT_B, CHILD_B), (PARENT_A, CHILD_A)):
        route = url(parent, 'extract_1')
        page = client.get(route, params={'view': 'agent', 'limit': 1, 'summary': True}).json()
        assert (page['parent_run_id'], page['node_id'], page['run_id'], page['reused']) == (parent, 'extract_1', child, False)
        assert (page['next_offset'], page['recorded_events'], page['total_events']) == (1, 3, 2)
        assert page['events'][0]['seq'] == 1 and page['events'][0]['data'] == {}
        second = client.get(route + '?view=agent&limit=1&offset=1&summary=true').json()
        assert second['events'][0]['seq'] == 3 and second['next_offset'] is None
        detail = client.get(route + '?view=agent&limit=1&offset=1').json()
        assert detail['run_id'] == child and detail['events'][0]['data']['answer'] == 'last-' + child
        assert client.get(route + '?view=runtime').json()['events'][0]['seq'] == 2
        assert client.get(route + '?limit=51').status_code == 400
        assert client.get(route.replace('alpha', 'beta')).status_code == 404
        assert client.get(f'/api/projects/alpha/runs/{child}/trace').status_code == 404
        assert client.get(route + '/export').status_code == 404
        assert client.delete(route).status_code in {404, 405}
        assert not (runs / (child + '.json')).exists()


def test_worker_binding_respects_reuse_and_active_status(client):
    create(client)
    first = {'unit_id': 'extract_1', 'run_id': CHILD_A, 'status': 'running'}
    second = {'node_id': 'merge_1', 'run_id': CHILD_B, 'status': 'completed', 'merge_mode': 'merge_and_extract'}
    save_parent(client, PARENT_A, {'extract_1': first}, {'merge_1': second}, status='running')
    save_parent(client, PARENT_B, {'extract_1': dict(first, reused=True)}, {'merge_1': second}, status='completed')
    old_worker = client.get(url(PARENT_A, 'extract_1')).json()
    assert old_worker['run_id'] == CHILD_A and old_worker['active'] and not old_worker['reused']
    copy = client.get(url(PARENT_B, 'extract_1')).json()
    assert copy['run_id'] == CHILD_A and copy['reused'] and not copy['active']
    merge = client.get(url(PARENT_A, 'merge_1')).json()
    assert merge['run_id'] == CHILD_B and merge['status'] == 'not_recorded' and not merge['active']
    # Earlier subset executions copied old unit states without the reused flag.
    save_parent(client, PARENT_B, {'extract_1': first}, selected=['extract_2'], status='running')
    legacy = client.get(url(PARENT_B, 'extract_1')).json()
    assert legacy['run_id'] == CHILD_A and legacy['reused'] and not legacy['active']


def test_missing_or_mismatched_parent_history_never_borrows_another_run(client):
    create(client)
    units = {'extract_1': {'run_id': CHILD_A}}
    out = save_parent(client, PARENT_A, units)
    save_parent(client, PARENT_B, units)
    history_a = out / 'workflow-runs' / (PARENT_A + '.json')
    history_a.unlink()
    assert client.get(url(PARENT_A, 'extract_1')).status_code == 404
    history_a.write_bytes((out / 'workflow-state.json').read_bytes())
    assert client.get(url(PARENT_A, 'extract_1')).status_code == 404
    # An older workspace may have current state only, but its embedded parent must match.
    (out / 'workflow-runs' / (PARENT_B + '.json')).unlink()
    assert client.get(url(PARENT_B, 'extract_1')).json()['run_id'] == CHILD_A


def test_only_owned_nodes_with_real_workers_can_read_traces(client):
    create(client)
    units = {'unstarted': {'run_id': None}, 'wrong_id': {'unit_id': 'other', 'run_id': CHILD_A},
             'bad_worker': {'run_id': '../elsewhere'}, 'parent': {'run_id': PARENT_A},
             'ambiguous': {'run_id': CHILD_A}}
    merges = {'deterministic': {'merge_mode': 'deterministic', 'run_id': CHILD_B},
              'recomposed': {'merge_mode': 'merge_and_extract', 'run_id': CHILD_B},
              'ambiguous': {'merge_mode': 'merge_and_extract', 'run_id': CHILD_B}}
    save_parent(client, PARENT_A, units, merges, recomposed_nodes=['recomposed'])
    for name in {*units, *merges, 'unknown', CHILD_A}:
        assert client.get(url(PARENT_A, name)).status_code == 404, name


def test_worker_trace_symlinks_use_same_path_checks_as_registered_traces(client, tmp_path):
    create(client)
    out = save_parent(client, PARENT_A, {'extract_1': {'run_id': CHILD_A}})
    external = tmp_path / 'external'
    external.write_text('private')
    archive = out.parent / 'runs' / (CHILD_A + '.trace.jsonl')
    try:
        archive.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip('symlink unavailable')
    assert client.get(url(PARENT_A, 'extract_1')).status_code == 400
    assert external.read_text() == 'private'

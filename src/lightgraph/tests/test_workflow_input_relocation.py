"""Resume a moved workspace from a verified historical node input, preserving its markers."""
import json
import shutil

import httpx
import pytest
from lightgraph.tests.web_client import RevisionClient as TestClient

from lightgraph.construction import overlap_review
from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.snapshot_registry import _context_path
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.construction_worker_fixture import install_scripted_worker, overlapping_metric_proposal
from lightgraph.tests.test_web_api import create, upload
from lightgraph.tests.test_web_execution import wait_run
from lightgraph.web import execution
from lightgraph.web.app import create_app


@pytest.fixture
def archived_csv_workflow(tmp_path, monkeypatch):
    """Old run with real node_delta/full markers, input files and result-permission records."""
    root = tmp_path / 'original'
    base = '/api/projects/alpha'
    with monkeypatch.context() as patch, TestClient(create_app(root)) as client:
        install_scripted_worker(patch, draft_factory=overlapping_metric_proposal)
        current = execution._workflow_node_inputs
        patch.setattr(execution, '_workflow_node_inputs',
                      lambda config, project, files: current(config, project, files, version=1))
        create(client)
        upload(client, 'metrics.csv', b'metric_code,metric_name,class_code,class_name,unit,currency\nK,A,C,Group,kg,EUR\n')
        upload(client, 'analysis.md', b'# Metrics documentation')
        cfg = client.get(base + '/config').json()
        cfg['goal']['purpose'] = 'Combine metric projections'
        cfg['model'].update(mode='in_environment', model='scripted', base_url='http://localhost:9999/v1',
                            api_key_env='LIGHTGRAPH_TEST_REVIEW_KEY')
        patch.setenv('LIGHTGRAPH_TEST_REVIEW_KEY', 'test-only')
        units = [{'unit_id': uid, 'goal_markdown': kind, 'source_ids': [cfg['sources'][0]['id']]}
                 for uid, kind in [('first', 'IncomeMetric'), ('second', 'LedgerItem')]]
        cfg['workflow'] = {'enabled': True, 'units': units, 'merge_nodes': [{'node_id': 'join'}],
                           'edges': [{'edge_id': u['unit_id'], 'source': u['unit_id'], 'target': 'join'} for u in units]}
        assert client.put(base + '/config', json=cfg).is_success
        response = client.post(base + '/runs')
        assert response.status_code == 202, response.text
        run = wait_run(client.app.state.manager, 'alpha', response.json()['run_id'])
        assert run['status'] in {'completed', 'completed_with_gaps'}, run
        project = client.app.state.workspace.project_path('alpha')
    # Legacy state did not store a hash format or a previous root.
    out = project / run['output_path']
    for file in [out / 'workflow-state.json', out / 'workflow-runs' / (run['run_id'] + '.json')]:
        state = json.loads(file.read_text())
        state.pop('node_input_versions')
        state.pop('legacy_node_input_roots')
        file.write_text(json.dumps(state))
    return project, run


def test_relocated_file_checkpoint_needs_full_proof_and_replays_review(archived_csv_workflow, tmp_path, monkeypatch):
    old, first = archived_csv_workflow
    project = tmp_path / 'copy' / 'alpha'
    shutil.copytree(old, project)
    old.rename(old.with_name('archived-offline'))  # Historical root must never be opened.
    monkeypatch.setenv('LIGHTGRAPH_TEST_REVIEW_KEY', 'test-only')
    requests = []

    def review(request):
        cards = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        decisions = [{'pattern_id': p['pattern_id'], 'action': 'merge' if 'apply' in p['eligible_actions'] else 'keep'}
                     for p in cards]
        requests.append(1)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
                             'content': json.dumps({'decisions': decisions})}, 'finish_reason': 'stop'}]})

    monkeypatch.setattr(overlap_review.httpx, 'HTTPTransport', lambda: httpx.MockTransport(review))
    base = '/api/projects/alpha'
    with TestClient(create_app(project.parent)) as client:
        full = first['workflow_state']['artifacts']['join']
        response = client.post(f'{base}/runs/{first["run_id"]}/resume',
                               json={'action': 'publish_artifact', 'artifact_id': full['artifact_id'],
                                     'previous_project_root': str(old)},
                               headers={'If-Match': client.get(base + '/config').headers['etag']})
        assert response.status_code == 202, response.text
        promoted = wait_run(client.app.state.manager, 'alpha', response.json()['run_id'])
        assert promoted['workflow_state']['merge']['published_artifact_id'] == full['artifact_id']
        assert client.get(base + '/graph').json()['nodes']
        assert load_checkpoint(project / first['output_path'], full)['graph']['access_fingerprint'] == full['access_fingerprint']
        cfg = client.get(base + '/config').json()
        cfg['workflow']['merge_nodes'][0]['merge_mode'] = 'overlap_review'
        assert client.put(base + '/config', json=cfg).is_success

        def resume(rid, root=None):
            body = {'action': 'rerun_node', 'from_node_id': 'join', 'verify_checkpoints_in_background': True}
            if root is not None:
                body['previous_project_root'] = str(root)
            return client.post(f'{base}/runs/{rid}/resume', json=body,
                               headers={'If-Match': client.get(base + '/config').headers['etag']})

        url_id = first['run_id']
        for bad_root in (None, old.parent / 'wrong'):
            resp = resume(url_id, bad_root)
            assert resp.status_code == 400 and resp.json()['error']['code'] == 'workflow_resume_input_changed', resp.text
        assert not requests
        record = first['workflow_state']['artifacts']['first']
        witness = _context_path(project / first['output_path'] / 'construction',
                                record['access_fingerprint'], record['result_policy_id'])
        saved = witness.read_bytes()
        witness.unlink()
        assert resume(url_id, old).json()['error']['code'] == 'graph_access_context_changed'
        witness.write_bytes(saved)
        for root in (old, None):
            resp = resume(url_id, root)
            assert resp.status_code == 202, resp.text
            url_id = resp.json()['run_id']
            run = wait_run(client.app.state.manager, 'alpha', url_id)
            assert run['status'] in {'completed', 'completed_with_gaps'}, run
            state = run['workflow_state']
            assert state['node_input_versions'] == {'first': 1, 'second': 1, 'join': 2}
            assert state['reused_nodes'] == ['first', 'second']
            for uid in state['reused_nodes']:
                assert state['artifacts'][uid] == first['workflow_state']['artifacts'][uid]
                assert state['node_inputs'][uid] == state['artifacts'][uid]['input_digest']
            commit = load_checkpoint(project / run['output_path'], state['artifacts']['join'])
            assert commit['overlap_review']['summary']['added_entity_rules'] == 2
            merged, = [n for n in commit['graph']['nodes'] if n['identity'] == {'code': 'K'}]
            assert {s['contributor_id'] for s in merged['supports']} == {'first', 'second'}
        assert requests == [1, 1]
        # A matching root hint does not authorize file, extraction or scope changes.
        source, = (project / 'files').glob('*.csv')
        original = source.read_bytes()
        source.write_bytes(original + b' changed')
        assert resume(url_id, old).json()['error']['code'] == 'workflow_resume_input_changed'
        source.write_bytes(original)
        cfg['workflow']['units'][0]['goal_markdown'] += ' changed'
        assert client.put(base + '/config', json=cfg).is_success
        assert resume(url_id, old).json()['error']['code'] == 'workflow_resume_input_changed'


def test_portable_digest_pins_paths_manifest_auth_and_parent_semantics(tmp_path):
    cfg = {'sources': [{'id': 'sheet', 'kind': 'excel_workbook', 'path': 'files/sheet.xlsx'}],
           'documents': ['files/info.md'], 'rules': ['files/rule.md'],
           'workflow': {'enabled': True, 'units': [{'unit_id': 'a'}], 'merge_nodes': [{'node_id': 'join'}],
                        'edges': [{'edge_id': 'a', 'source': 'a', 'target': 'join'}]}}
    original, moved = [tmp_path / name for name in ('first', 'copy')]
    (original / 'files').mkdir(parents=True)
    for name in ('sheet.xlsx', 'info.md', 'rule.md'):
        (original / 'files' / name).write_text(name)
    shutil.copytree(original, moved)

    def inputs(project, **kwargs):
        return execution._workflow_node_inputs(RunConfig.model_validate(cfg).resolve_paths(project), project, **kwargs)

    expected = inputs(original)
    legacy = inputs(original, version=1)
    assert inputs(moved) == expected
    assert inputs(moved, version=1) != legacy
    assert inputs(moved, version=1, previous_project_root=str(original)) == legacy
    cfg['sources'][0]['allowed_tables'] = ['public.Sheet2']
    assert all(inputs(moved)[nid] != expected[nid] for nid in ('a', 'join'))
    assert inputs(moved, version=1, previous_project_root=str(original)) != legacy
    del cfg['sources'][0]['allowed_tables']
    (moved / 'files/another.xlsx').write_bytes((moved / 'files/sheet.xlsx').read_bytes())
    cfg['sources'][0]['path'] = 'files/another.xlsx'
    assert all(inputs(moved)[nid] != expected[nid] for nid in ('a', 'join'))

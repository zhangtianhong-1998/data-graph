"""Exercise the actual CSV -> two workers -> merge -> history -> resume HTTP path."""
import subprocess
import time

from lightgraph.tests.construction_worker_fixture import install_scripted_worker
from lightgraph.tests.test_web_api import client, create, upload
from lightgraph.tests.test_web_execution import wait_run


def test_apply_merge_rule_to_committed_extracts_via_http(client, monkeypatch):
    install_scripted_worker(monkeypatch)
    create(client)
    base = '/api/projects/alpha'
    for name, label in [('income', '收入'), ('orders', '营业收入')]:
        upload(client, name + '.csv', ('metric_code,metric_name\nK1,' + label + '\n').encode())
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = '合并两表的指标定义'
    cfg['model'].update(mode='in_environment', model='scripted-test', base_url='http://127.0.0.1:9999/v1')
    units = [{'unit_id': 'branch' + str(i), 'source_ids': [source['id']]} for i, source in enumerate(cfg['sources'])]
    assert len(units) == 2
    cfg['workflow'] = {'enabled': True, 'max_parallel_workers': 2, 'units': units,
        'merge_nodes': [{'node_id': 'merge', 'merge_mode': 'deterministic'}],
        'edges': [{'edge_id': u['unit_id'], 'source': u['unit_id'], 'target': 'merge'} for u in units]}
    assert client.put(base + '/config', json=cfg).is_success
    manager = client.app.state.manager

    def finished(response):
        assert response.status_code == 202, response.text
        run = wait_run(manager, 'alpha', response.json()['run_id'])
        assert run['status'] in {'completed', 'completed_with_gaps'}, run
        return run

    first = finished(client.post(base + '/runs'))
    graph = client.get(base + '/graph').json()
    assert len(graph['nodes']) == 2
    version = graph['version_id']
    # No manual analytics request or embedding service is needed after root publication.
    for _ in range(200):
        analysis = client.get(base + '/graph/analytics', params={'version': version}).json()
        if analysis['status'] not in {'running', 'queued'}:
            break
        time.sleep(.01)
    assert analysis['status'] == 'completed' and analysis['counts']['nodes'] == 2

    # Adjust only the merge: neither branch, its source file, nor its successful rules changed.
    cfg = client.get(base + '/config').json()
    cfg['workflow']['merge_nodes'][0]['merge_policy'] = {'fusion_policy': {'entity_rules': [{
        'entity_type': 'Metric', 'canonical_type': 'Metric', 'identity_domain': '统一指标编码',
        'key_mapping': {'code': 'code'}}]}}
    assert client.put(base + '/config', json=cfg).is_success
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected worker')))
    route = base + '/runs/' + first['run_id'] + '/resume'
    headers = {'If-Match': client.get(base + '/config').headers['etag']}
    resumed = finished(client.post(route, json={'action': 'rerun_node', 'from_node_id': 'merge'}, headers=headers))
    assert resumed['workflow_state']['rerun_nodes'] == ['merge']
    merged = client.get(base + '/graph').json()
    assert len(merged['nodes']) == 1
    node = merged['nodes'][0]
    details = client.get(base + '/graph/details', params={'version': merged['version_id'], 'entity_id': node['id']}).json()
    assert set(details['standard_nodes'][0]['label_variants']) == {'收入', '营业收入'}
    assert len(client.get(base + '/graph', params={'version': version}).json()['nodes']) == 2

    artifact = resumed['workflow_state']['artifacts']['merge']['artifact_id']
    promotion = finished(client.post(base + '/runs/' + resumed['run_id'] + '/resume',
                                    json={'action': 'publish_artifact', 'artifact_id': artifact}, headers=headers))
    assert promotion['workflow_state']['merge']['published_artifact_id'] == artifact

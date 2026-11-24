"""Exercise CSV workers, merge, history and resume through the workbench HTTP endpoints."""
import json
import subprocess
import threading
import time
from pathlib import Path
import runpy

from lightgraph.tests.construction_worker_fixture import hierarchy_proposal, install_scripted_worker
from lightgraph.tests.test_web_api import client, create, upload
from lightgraph.tests.test_web_execution import wait_run


def test_rerun_deterministic_merge_via_http_replays_four_checkpoints_before_publication(client, monkeypatch):
    from lightgraph.construction import checkpoints, merger

    install_scripted_worker(monkeypatch, draft_factory=hierarchy_proposal)
    create(client)
    api = '/api/projects/alpha'
    upload(client, 'tree.csv', b'parent_code,parent_name,child_code,child_name\nP,Root,A,One\nP,Root,B,Two\n')
    cfg = client.get(api + '/config').json()
    cfg['goal']['purpose'] = '合并目录树'
    cfg['model'].update(mode='in_environment', model='scripted-test', base_url='http://127.0.0.1:9999/v1')
    units = ['extract_' + str(i) for i in range(1, 5)]
    cfg['workflow'] = {'enabled': True, 'max_parallel_workers': 2,
        'units': [{'unit_id': uid, 'source_ids': [cfg['sources'][0]['id']], 'goal_markdown': kind}
                  for uid, kind in zip(units, ['Entry', 'Term', 'Subject', 'Heading'])],
        'merge_nodes': [{'node_id': 'merge_1', 'merge_mode': 'deterministic'}],
        'edges': [{'edge_id': uid, 'source': uid, 'target': 'merge_1'} for uid in units]}
    assert client.put(api + '/config', json=cfg).is_success
    manager = client.app.state.manager

    def finish(response):
        assert response.status_code == 202, response.text
        return wait_run(manager, 'alpha', response.json()['run_id'], timeout=60)

    # Simulate the historical merge implementation; leave the actual four leaf
    # workers, checkpoints, policy and subsequent HTTP rerun unchanged.
    merge = merger.merge_construction_graphs
    with monkeypatch.context() as old:
        old.setattr(merger, 'merge_construction_graphs',
                    lambda *args, **kwargs: merge(*args, **kwargs, recognize_source_replays=False))
        original = finish(client.post(api + '/runs'))
    old_state = original['workflow_state']
    assert original['status'] == 'completed' and old_state['merge_nodes']['merge_1']['nodes'] == 12
    assert old_state['merge']['nodes'] == 12 and old_state['merge']['edges'] == 8
    route = api + '/runs/' + original['run_id'] + '/resume'
    payload = {'action': 'rerun_node', 'from_node_id': 'merge_1', 'verify_checkpoints_in_background': True}
    headers = {'If-Match': client.get(api + '/config').headers['etag']}

    # Holding a physical checkpoint read must not hold the HTTP request.
    release = threading.Event()
    read = checkpoints.load_checkpoint
    reads = []

    def load_once(output, record):
        reads.append(record['node_id'])
        assert release.wait(10), 'checkpoint body read on request thread'
        return read(output, record)

    monkeypatch.setattr(checkpoints, 'load_checkpoint', load_once)

    def unexpected_worker(*args, **kwargs):
        raise AssertionError('merge-only rerun started an extract worker')
    monkeypatch.setattr(subprocess, 'Popen', unexpected_worker)
    try:
        accepted = client.post(route, json=payload, headers=headers)
        assert accepted.status_code == 202, accepted.text
    finally:
        release.set()
    rerun = finish(accepted)
    state = rerun['workflow_state']
    assert sorted(reads) == units and rerun['status'] == 'completed', state
    assert state['rerun_nodes'] == ['merge_1'] and state['reused_nodes'] == units
    assert state['resumed_from_run_id'] == original['run_id']
    assert all(state['artifacts'][uid] == old_state['artifacts'][uid] for uid in units)
    artifact = state['artifacts']['merge_1']
    assert artifact['artifact_id'] != old_state['artifacts']['merge_1']['artifact_id']
    assert artifact['upstream_artifacts'] == [state['artifacts'][uid]['artifact_id'] for uid in units]
    assert all(state['units'][uid]['model_calls'] == 0 for uid in units)
    node = state['merge_nodes']['merge_1']
    counts = node['merge_counts']
    assert node['model_calls'] == 0 and node['artifact_status'] == 'committed'
    assert counts['source_replay_status'] == 'ran'
    assert (counts['input_nodes'], counts['hierarchy_replay_collapsed_nodes'], node['nodes'], node['edges']) == (12, 9, 3, 8)
    assert state['merge']['nodes'] == 3 and state['merge']['edges'] == 8
    current = client.get(api + '/graph').json()
    assert len(current['nodes']) == 3
    assert client.get(api + '/runs/' + original['run_id']).json()['workflow_state']['merge']['nodes'] == 12

    # Damage a pinned input after a successful run. Content corruption must
    # fail the accepted run and leave the last good root visible.
    project = client.app.state.workspace.project_path('alpha')
    directory = project / original['output_path'] / state['artifacts'][units[0]]['relative_path']
    (directory / 'nodes.parquet').write_bytes(b'corrupt')
    failed = finish(client.post(route, json=payload, headers=headers))
    assert failed['status'] == 'failed' and failed['error_code'] == 'workflow_checkpoint_invalid'
    assert failed['workflow_state']['units'][units[0]]['error_code'] == 'workflow_checkpoint_invalid'
    assert 'merge_1' not in failed['workflow_state']['artifacts']
    assert client.get(api + '/graph').json()['version_id'] == current['version_id']
    synchronous = client.post(route, json={'action': 'rerun_node', 'from_node_id': 'merge_1'}, headers=headers)
    assert not synchronous.is_success and 'workflow_checkpoint_invalid' in synchronous.text
    # A missing required file is catchable before the asynchronous run starts.
    (directory / 'nodes.parquet').unlink()
    missing = client.post(route, json=payload, headers=headers)
    assert not missing.is_success and 'workflow_checkpoint_invalid' in missing.text


def test_audit_replays_the_same_three_committed_workers_without_writing(client, monkeypatch, tmp_path):
    install_scripted_worker(monkeypatch)
    create(client)
    base = '/api/projects/alpha'
    for name in ('collection', 'other'):
        upload(client, name + '.csv', b'metric_code,metric_name\nA,First\nB,Second\n')
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = '归并重复采集的对象'
    cfg['model'].update(mode='in_environment', model='scripted-test', base_url='http://127.0.0.1:9999/v1')
    first, other = [s['id'] for s in cfg['sources']]
    units = [{'unit_id': f'u{i}', 'source_ids': [sid]} for i, sid in enumerate([first, first, other])]
    cfg['workflow'] = {'enabled': True, 'units': units, 'merge_nodes': [{'node_id': 'merged'}],
                       'edges': [{'edge_id': u['unit_id'], 'source': u['unit_id'], 'target': 'merged'} for u in units]}
    assert client.put(base + '/config', json=cfg).is_success
    started = client.post(base + '/runs')
    assert started.is_success
    rid = started.json()['run_id']
    run = wait_run(client.app.state.manager, 'alpha', rid)
    assert run['workflow_state']['merge']['nodes'] == 4
    project = client.app.state.workspace.project_path('alpha')
    checkpoint = project / run['output_path'] / 'checkpoints'
    def snapshot():
        return {p.relative_to(checkpoint): p.stat().st_mtime_ns for p in checkpoint.rglob('*') if p.is_file()}
    before = snapshot()
    script = Path(__file__).resolve().parents[3] / 'scripts/audit_checkpoint_merge.py'
    audit = runpy.run_path(str(script))['audit']
    run_file = project / 'runs' / (rid + '.json')
    report = audit(run_file, 'merged', include_hierarchy_membership=True)
    assert report['historical_input_totals_verified'] and report['explicit_entity_rules'] == 0
    assert 'edge_lineage' not in report['with_source_replay']
    assert report['without_source_replay']['output_nodes'] == 6
    assert report['with_source_replay']['output_nodes'] == report['committed']['nodes'] == 4
    assert report['with_source_replay']['source_replay_collapsed_nodes'] == 2
    membership = report['with_source_replay']['hierarchy_membership']
    assert membership['groups_by_output_id'] == {} and membership['represented_collapsed_nodes'] == 0
    assert membership['agrees_with_recorded_count']  # These copies folded through the row-identity channel.
    previous = tmp_path / 'audit.json'
    previous.write_text(json.dumps(report), encoding='utf-8-sig')
    rerun = audit(run_file, 'merged', compare_to=previous, include_edge_lineage=True, explain_identity='A')
    trace = rerun['with_source_replay']['identity_trace']
    assert trace['identity_value'] == 'A' and len(trace['output_ids']) == 2
    assert 'identity_trace' not in report['with_source_replay']
    assert rerun['without_source_replay']['edge_lineage']['before_hierarchy_replay']['captured_edges'] is None
    assert rerun['with_source_replay']['edge_lineage']['before_hierarchy_replay']['captured_edges'] == report['with_source_replay']['output_edges']
    assert rerun['with_source_replay']['hierarchy_membership'] == membership
    comparison = rerun['hierarchy_comparison']
    assert comparison['policy_digest_comparison'] == 'matched'
    assert comparison['net_node_reduction'] == comparison['additional_hierarchy_collapses'] == 0
    assert report['committed']['integrity'] == report['with_source_replay']['integrity'] == {
        'duplicate_node_ids': 0, 'duplicate_edge_ids': 0, 'edges_with_missing_endpoints': 0,
        'edges_with_missing_definition': 0, 'edges_with_wrong_endpoint_types': 0, 'self_links': 0}
    def unexpected_merge(*args, **kwargs):
        raise AssertionError("observations-only must not replay the merge")
    monkeypatch.setattr('lightgraph.construction.merger.merge_construction_graphs', unexpected_merge)
    observations = audit(run_file, 'merged', observations_only=True)
    assert observations['typed_row_identity'] == report['committed']['typed_row_identity']
    assert observations['typed_row_identity']['multiple_contributor_groups'] == 2
    assert observations['typed_row_identity']['comparison_counts']['physical_tables'] == {'disjoint': 2}
    assert snapshot() == before


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

"""Cover the HTTP merge-only path, its immutable decisions and offline replay."""
import json
from pathlib import Path
import runpy
import time
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.construction import overlap_patterns, overlap_review
from lightgraph.construction import workflow_overlap
from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.workflow_overlap import overlap_review_skip_reason
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import FusionPolicyConfig, MergePolicyConfig, RunConfig, WorkflowMergeNodeConfig
from lightgraph.tests.construction_worker_fixture import (install_scripted_worker, overlapping_metric_proposal,
                                                         partial_role_proposal, regional_proposal, replace_worker_launch)
from lightgraph.tests.test_web_api import client, create, upload
from lightgraph.tests.test_web_execution import wait_run


@pytest.mark.parametrize('split_batches', [False, True])
def test_review_of_two_committed_csv_workers_is_replayable_via_http(client, monkeypatch, split_batches):
    install_scripted_worker(monkeypatch, draft_factory=overlapping_metric_proposal)
    create(client)
    base = '/api/projects/alpha'
    upload(client, 'metrics.csv', b'metric_code,metric_name,class_code,class_name,unit,currency\nK,A,C,Group,kg,EUR\n')
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = 'Combine metric projections'
    cfg['model'].update(mode='in_environment', model='scripted', base_url='http://localhost:9999/v1',
                        api_key_env='LIGHTGRAPH_TEST_REVIEW_KEY')
    if split_batches:
        cfg['model']['max_output_tokens'] = 1024
    monkeypatch.setenv('LIGHTGRAPH_TEST_REVIEW_KEY', 'test-only')
    units = [{'unit_id': uid, 'goal_markdown': kind, 'source_ids': [cfg['sources'][0]['id']]}
             for uid, kind in [('first', 'IncomeMetric'), ('second', 'LedgerItem')]]
    cfg['workflow'] = {'enabled': True, 'units': units, 'merge_nodes': [{'node_id': 'join'}],
                       'edges': [{'edge_id': u['unit_id'], 'source': u['unit_id'], 'target': 'join'} for u in units]}
    assert client.put(base + '/config', json=cfg).is_success

    def finished(response):
        assert response.status_code == 202, response.text
        run = wait_run(client.app.state.manager, 'alpha', response.json()['run_id'])
        assert run['status'] in {'completed', 'completed_with_gaps'}, run['workflow_state']
        return run

    original = finished(client.post(base + '/runs'))
    cfg['workflow']['merge_nodes'][0]['merge_mode'] = 'overlap_review'
    assert client.put(base + '/config', json=cfg).is_success
    requests = []

    def decide(request):
        patterns = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        requests.append(patterns)
        decisions = [{'pattern_id': p['pattern_id'], 'action': 'merge' if 'apply' in p['eligible_actions'] else 'keep'}
                     for p in patterns]
        finish = 'length' if split_batches and patterns[0]['kind'] == 'relation' else 'stop'
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content':
                                               json.dumps({'decisions': decisions})}, 'finish_reason': finish}]})

    monkeypatch.setattr(overlap_review.httpx, 'HTTPTransport', lambda: httpx.MockTransport(decide))
    def unexpected_worker(*args, **kwargs):
        raise AssertionError('unexpected worker during checkpoint reuse')
    replace_worker_launch(monkeypatch, unexpected_worker)
    headers = {'If-Match': client.get(base + '/config').headers['etag']}
    run = finished(client.post(base + '/runs/' + original['run_id'] + '/resume',
                               json={'action': 'rerun_node', 'from_node_id': 'join',
                                     'verify_checkpoints_in_background': True}, headers=headers))
    state = run['workflow_state']
    node = state['merge_nodes']['join']
    project = client.app.state.workspace.project_path('alpha')
    output = project / run['output_path']
    artifact = state['artifacts']['join']
    committed = load_checkpoint(output, artifact)
    report = committed['overlap_review']
    expected_calls = len(report['patterns']) if split_batches else 1
    assert node['model_calls'] == len(requests) == expected_calls
    if split_batches:
        assert expected_calls > 1 and report['summary']['status'] == 'partial'
        assert report['summary']['failed_patterns'] == 1
        assert report['summary']['reviewed_patterns'] == expected_calls - 1
        assert report['summary']['reason'] == 'model_response_truncated'
    else:
        assert report['summary']['status'] == 'reviewed'
    assert node['overlap_review'] == report['summary']
    assert node['overlap_review']['eligible_patterns'] == sum('apply' in p['eligible_actions'] for p in report['patterns'])
    assert node['overlap_review']['added_entity_rules'] == 2, report
    assert state['reused_nodes'] == ['first', 'second']
    assert {name: state['artifacts'][name] for name in state['reused_nodes']} == {
        name: original['workflow_state']['artifacts'][name] for name in state['reused_nodes']}
    assert client.get(base + '/runs/' + run['run_id'] + '?view=workbench').json()[
        'workflow_state']['merge_nodes']['join']['overlap_review'] == report['summary']
    merged, = [n for n in committed['graph']['nodes'] if n['identity'] == {'code': 'K'}]
    assert merged['properties']['unit'] == ['kg'] and merged['properties']['currency'] == ['EUR']
    assert {'first', 'second'} == {s['contributor_id'] for s in merged['supports']}
    assert len(client.get(base + '/graph').json()['nodes']) == len(committed['graph']['nodes']) == 2
    relation, = committed['graph']['edges']
    assert relation['type'] == 'belongs_to' and relation['source'] == merged['id']
    expected = {}
    for owner in state['reused_nodes']:
        leaf = load_checkpoint(output, state['artifacts'][owner])
        edge, = leaf['graph']['edges']
        expected[owner] = (edge['id'], edge['type'], edge['source'], edge['target'])
    origins = {o['contributor_id']: (o['edge_id'], o['type'], o['source'], o['target'])
               for s in relation['supports'] for o in s['endpoint_origins'] if o.get('input_kind') == 'unit'}
    assert origins == expected

    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/audit_checkpoint_merge.py'))['audit']
    run_file = project / 'runs' / (run['run_id'] + '.json')
    with monkeypatch.context() as guard:
        guard.setitem(audit.__globals__, 'load_checkpoint', lambda *_: pytest.fail('read full graph'))
        explained = audit(run_file, 'join', overlap_patterns_only=True, include_overlap_evidence=True)
    assert explained['overlap_review'] == report and explained['model_calls'] == 0
    entity = next(row for row in explained['eligibility_evidence'] if row['decision'] == 'merge')
    assert entity['distinct_recorded_access_fingerprints'] == 1 and not entity['access_missing_from']
    assert entity['different_contract_fields'] == [] and 'apply' in entity['eligible_actions']
    replay = audit(run_file, 'join')
    assert replay['with_source_replay']['output_nodes'] == replay['committed']['nodes']
    assert replay['with_source_replay']['output_edges'] == replay['committed']['edges']
    assert replay['overlap_review'] == report
    promotion = finished(client.post(base + '/runs/' + run['run_id'] + '/resume',
                                     json={'action': 'publish_artifact', 'artifact_id': artifact['artifact_id']},
                                     headers=headers))
    assert promotion['workflow_state']['merge']['published_artifact_id'] == artifact['artifact_id']
    assert promotion['workflow_state']['merge_nodes']['join']['model_calls'] == 0 and len(requests) == expected_calls
    sidecar = (output / artifact['relative_path']).parent / 'overlap_review.json'
    sidecar.write_text('{}', encoding='utf-8')
    with pytest.raises(LightGraphError, match='workflow_checkpoint_invalid'):
        load_checkpoint(output, artifact)


def test_review_requires_same_batch_direct_extract_checkpoints():
    record = {'artifact_status': 'committed', 'graph_kind': 'node_delta', 'status': 'completed',
              'source_attempt': 'original'}
    ids = ['a', 'b']
    inputs = [{'contributor_id': key, 'kind': 'unit', 'graph': {}} for key in ids]
    artifacts = {key: record.copy() for key in ids}
    assert overlap_review_skip_reason(inputs, ids, artifacts, False) is None
    assert overlap_review_skip_reason(inputs, ids, artifacts, True) == 'inherited_graph'
    assert overlap_review_skip_reason(inputs[:1], ids[:1], artifacts, False) == 'insufficient_extract_units'
    assert overlap_review_skip_reason([inputs[0], {**inputs[1], 'kind': 'merged'}], ids, artifacts, False
                                      ) == 'requires_direct_extract_inputs'
    artifacts['b']['source_attempt'] = 'later'
    assert overlap_review_skip_reason(inputs, ids, artifacts, False) == 'mixed_extraction_batches'
    artifacts['b']['coverage'] = 'incomplete'
    assert overlap_review_skip_reason(inputs, ids, artifacts, False) == 'incomplete_extract_inputs'


def test_merge_node_checks_an_agent_proposal_and_replays_only_the_supported_members(client, monkeypatch):
    install_scripted_worker(monkeypatch, draft_factory=partial_role_proposal)
    create(client)
    base = '/api/projects/alpha'
    upload(client, 'facts.csv', b'metric_code,metric_name,class_code,class_name,unit,currency,extra_class\n'
           b'X,Stock,C,Class A,kg,EUR,\nY,Freight,D,Class B,t,USD,C\n')
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = 'Compare the overlapping metric projections of the two workers'
    cfg['model'].update(mode='in_environment', model='scripted', base_url='http://localhost:9999/v1',
                        api_key_env='LIGHTGRAPH_TEST_REVIEW_KEY')
    monkeypatch.setenv('LIGHTGRAPH_TEST_REVIEW_KEY', 'test-only')
    cfg['workflow'] = {'enabled': True, 'units': [
        {'unit_id': owner, 'goal_markdown': kind, 'source_ids': [cfg['sources'][0]['id']]}
        for owner, kind in [('sales', 'IncomeMetric'), ('ledger', 'LedgerItem')]],
        'merge_nodes': [{'node_id': 'join', 'merge_mode': 'overlap_review'}],
        'edges': [{'edge_id': owner, 'source': owner, 'target': 'join'} for owner in ('sales', 'ledger')]}
    assert client.put(base + '/config', json=cfg).is_success
    issued = []

    def decide(request):
        patterns = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        decisions = []
        for p in patterns:
            decision = {'pattern_id': p['pattern_id'], 'action': 'keep'}
            if {m['type'] for m in p['members']} == {'IncomeMetric', 'LedgerItem'}:
                assert 'apply' not in p['eligible_actions']
                assert 'propose' in p['review_actions'] and 'merge' not in p['review_actions']
                issued.append(p['pattern_id'])
                decision.update(action='propose', alignment=[
                    {'member_index': i, 'source_index': m['source_indexes'][0],
                     'role_index': next(j for j, r in enumerate(m['roles']) if r['relation_type'] == 'belongs_to')}
                    for i, m in enumerate(p['members'])])
            decisions.append(decision)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content':
                              json.dumps({'decisions': decisions})}, 'finish_reason': 'stop'}]})

    monkeypatch.setattr(overlap_review.httpx, 'HTTPTransport', lambda: httpx.MockTransport(decide))
    response = client.post(base + '/runs')
    assert response.status_code == 202, response.text
    run = wait_run(client.app.state.manager, 'alpha', response.json()['run_id'])
    state = run['workflow_state']
    assert run['status'] in {'completed', 'completed_with_gaps'}, state
    project = client.app.state.workspace.project_path('alpha')
    artifact = load_checkpoint(project / run['output_path'], state['artifacts']['join'])
    report = artifact['overlap_review']
    assert len(issued) == 1 and state['merge_nodes']['join']['model_calls'] == 1
    assert report['summary']['added_entity_rules'] == 0
    assert report['summary']['proposed_patterns'] == 1
    assert report['summary']['proposal_collapsed_nodes'] == report['summary']['applied_proposal_groups'] == 1, report['proposal_checks']
    assert len([n for n in artifact['graph']['nodes'] if n['label'] == 'Stock']) == 1
    assert len([n for n in artifact['graph']['nodes'] if n['label'] == 'Freight']) == 2
    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/audit_checkpoint_merge.py'))['audit']
    replay = audit(project / 'runs' / (run['run_id'] + '.json'), 'join', include_edge_lineage=True)
    assert replay['with_source_replay']['rows_match_committed'] == {'nodes': True, 'edges': True}
    assert replay['model_calls'] == 0


@pytest.mark.parametrize('rejection', ['overlap_pattern_policy_conflict', 'published_relation_cycle', 'merge_requires_isolation'])
def test_invalid_combined_rules_revert_to_existing_policy_without_second_request(monkeypatch, rejection):
    config = RunConfig(sources=[])
    node = WorkflowMergeNodeConfig(node_id='merge', merge_mode='overlap_review')
    policy = MergePolicyConfig(fusion_policy=FusionPolicyConfig())
    selected_policy = MergePolicyConfig(fusion_policy={'entity_rules': [
        {'entity_type': 'Alias', 'canonical_type': 'Metric', 'identity_domain': 'catalog', 'key_mapping': {'id': 'id'}}]})
    ids = ['a', 'b']
    inputs = [{'kind': 'unit', 'contributor_id': uid, 'graph': {}} for uid in ids]
    artifacts = {uid: {'artifact_id': uid, 'artifact_status': 'committed', 'graph_kind': 'node_delta',
                       'status': 'completed', 'source_attempt': 'batch'} for uid in ids}
    monkeypatch.setattr(workflow_overlap, 'analyze_indexed_overlap_patterns',
                        lambda *args: SimpleNamespace(patterns=[{'pattern_id': 'pair', 'kind': 'entity'}], summary={}))
    requests = []

    def review(*args, **kwargs):
        requests.append(1)
        return {'status': 'reviewed', 'model_calls': 1, 'reviewed_patterns': 1, 'omitted_patterns': 0,
                'decisions': [{'pattern_id': 'pair', 'action': 'merge'}], 'selected_pattern_ids': ['pair']}

    def apply(*args):
        if rejection == 'overlap_pattern_policy_conflict':
            raise LightGraphError(rejection)
        return selected_policy

    def merge(contributors, *, target_access_fingerprint, merge_policy):
        if merge_policy == selected_policy:
            if rejection == 'published_relation_cycle':
                raise LightGraphError(rejection)
            return {'conflict_isolation': {'coverage': 'incomplete'}}
        return {'conflict_isolation': {'coverage': 'complete'}}

    monkeypatch.setattr(workflow_overlap, 'review_overlap_patterns', review)
    monkeypatch.setattr(workflow_overlap, 'apply_overlap_decisions', apply)
    monkeypatch.setattr(workflow_overlap, 'merge_construction_graphs', merge)
    graph, report = workflow_overlap.merge_with_overlap_review(
        config, node, policy, inputs, ids, artifacts, inherited=False, source_namespace='alpha',
        model_api_key=None, check_cancelled=lambda: None, deadline=time.monotonic() + 10)
    assert graph['conflict_isolation']['coverage'] == 'complete'
    assert report['summary']['status'] == 'fallback' and report['summary']['reason'] == rejection
    assert report['summary']['added_entity_rules'] == 0
    assert report['effective_merge_policy'] == policy.model_dump(mode='json')
    assert len(requests) == 1

@pytest.mark.parametrize('inherited', [False, True])
def test_missing_indexes_do_not_trigger_graph_scan_or_model_call(monkeypatch, inherited):
    config = RunConfig(sources=[])
    policy = MergePolicyConfig(fusion_policy=FusionPolicyConfig())
    ids = ['sales', 'service']
    inputs = [{'kind': 'unit', 'contributor_id': uid, 'graph': {}} for uid in ids]
    artifacts = {uid: {'artifact_id': uid, 'artifact_status': 'committed', 'graph_kind': 'node_delta',
                       'status': 'completed', 'source_attempt': 'batch'} for uid in ids}

    def unexpected(*args, **kwargs):
        pytest.fail('missing indexes must not trigger another discovery path')

    def merge(contributors, *, target_access_fingerprint, merge_policy):
        assert contributors is inputs and merge_policy is policy
        return {'nodes': [], 'edges': []}

    monkeypatch.setattr(overlap_patterns, '_read_node', unexpected)
    monkeypatch.setattr(overlap_patterns, '_read_edges', unexpected)
    monkeypatch.setattr(workflow_overlap, 'review_overlap_patterns', unexpected)
    monkeypatch.setattr(workflow_overlap, 'merge_construction_graphs', merge)
    reads = []

    def read_index(contributor):
        assert not inherited
        reads.append(contributor['contributor_id'])
        return None

    _, report = workflow_overlap.merge_with_overlap_review(
        config, WorkflowMergeNodeConfig(node_id='merge', merge_mode='overlap_review'), policy, inputs,
        ids, artifacts, inherited=inherited, source_namespace='alpha', model_api_key=None,
        check_cancelled=lambda: None, deadline=time.monotonic() + 10, load_overlap_index=read_index)
    assert report['summary']['model_calls'] == 0
    if inherited:
        assert reads == [] and report['summary']['reason'] == 'inherited_graph'
    else:
        assert reads == ids and report['summary']['reason'] == 'overlap_index_missing'
        assert sorted(report['pattern_summary']['missing_index_contributors']) == ids
    assert report['effective_merge_policy'] == report['configured_merge_policy']


def test_three_region_projections_review_type_once_on_fresh_run_and_merge_only_rerun(client, monkeypatch):
    install_scripted_worker(monkeypatch, draft_factory=regional_proposal)
    create(client)
    base = '/api/projects/alpha'
    upload(client, 'regions.csv', (
        'region_code,region_name,country_code,country_name,short_name,postal_prefix,zone\n'
        'CN-BJ,北京地区,CN,中国,京,P10,Z01\nCN-JS,江苏地区,CN,中国,苏,P21,Z02\n'
        'CN-TJ,天津地区,CN,中国,津,P30,Z03\n').encode())
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = 'Identify regions shared across parallel business projections'
    cfg['model'].update(mode='in_environment', model='scripted', base_url='http://localhost:9999/v1',
                        api_key_env='LIGHTGRAPH_TEST_REVIEW_KEY')
    monkeypatch.setenv('LIGHTGRAPH_TEST_REVIEW_KEY', 'test-only')
    kinds = {'sales': 'SalesRegion', 'reporting': 'ReportingArea', 'service': 'ServiceTerritory'}
    cfg['workflow'] = {
        'enabled': True, 'units': [{'unit_id': uid, 'goal_markdown': kind, 'source_ids': [cfg['sources'][0]['id']]}
                                   for uid, kind in kinds.items()],
        'merge_nodes': [{'node_id': 'join', 'merge_mode': 'overlap_review'}],
        'edges': [{'edge_id': uid, 'source': uid, 'target': 'join'} for uid in kinds]}
    updated = client.put(base + '/config', json=cfg)
    assert updated.is_success, updated.text
    requests = []

    def decide(request):
        prompt = json.loads(request.content)['messages'][1]['content']
        patterns = json.loads(prompt)['patterns']
        assert not any(value in prompt for value in ('北京地区', '江苏地区', '天津地区', 'CN-BJ', 'CN-JS', 'CN-TJ'))
        region, = [p for p in patterns if {m['type'] for m in p['members']} == set(kinds.values())]
        assert region['overlapping_keys'] == 3 and 'apply' in region['eligible_actions']
        requests.append(region['pattern_id'])
        decisions = [{'pattern_id': p['pattern_id'], 'action': 'merge' if p is region else 'keep'} for p in patterns]
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content':
                                               json.dumps({'decisions': decisions})}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(overlap_review.httpx, 'HTTPTransport', lambda: httpx.MockTransport(decide))
    analyzed = []
    analyze = workflow_overlap.analyze_indexed_overlap_patterns

    def indexed(descriptors, policy):
        assert {d['contributor_id'] for d in descriptors} == set(kinds)
        assert all('graph' not in d and d['overlap_index'] for d in descriptors)
        analyzed.append(1)
        return analyze(descriptors, policy)
    monkeypatch.setattr(workflow_overlap, 'analyze_indexed_overlap_patterns', indexed)

    def completed(response):
        assert response.status_code == 202, response.text
        run = wait_run(client.app.state.manager, 'alpha', response.json()['run_id'])
        assert run['status'] in {'completed', 'completed_with_gaps'}, run['workflow_state']
        meta = run['workflow_state']['merge_nodes']['join']
        assert meta['model_calls'] == 1 and meta['overlap_review']['added_entity_rules'] == 3, meta
        return run

    fresh = completed(client.post(base + '/runs'))
    def fail(*args, **kwargs):
        pytest.fail('merge-only rerun must reuse verified per-branch indexes')

    monkeypatch.setattr(overlap_patterns, '_read_node', fail)
    monkeypatch.setattr(overlap_patterns, '_read_edges', fail)
    replace_worker_launch(monkeypatch, fail)
    headers = {'If-Match': client.get(base + '/config').headers['etag']}
    rerun = completed(client.post(base + '/runs/' + fresh['run_id'] + '/resume',
                                 json={'action': 'rerun_node', 'from_node_id': 'join'}, headers=headers))
    state = rerun['workflow_state']
    assert set(state['reused_nodes']) == set(kinds)
    assert all(state['artifacts'][uid] == fresh['workflow_state']['artifacts'][uid] for uid in kinds)
    assert len(requests) == len(analyzed) == 2 and requests[0] == requests[1]
    checkpoint = load_checkpoint(client.app.state.workspace.project_path('alpha') / rerun['output_path'],
                                 state['artifacts']['join'])
    regions = [n for n in checkpoint['graph']['nodes'] if n['type'] in kinds.values()]
    assert len(regions) == 3 and {n['label'] for n in regions} == {'北京地区', '江苏地区', '天津地区'}
    for region in regions:
        assert {s['contributor_id'] for s in region['supports']} == set(kinds)
        assert set(region['properties']) >= {'short_name', 'postal_prefix', 'zone'}
    relations = checkpoint['graph']['edges']
    assert len(relations) == 3
    assert {r['source'] for r in relations} == {r['id'] for r in regions}
    for relation in relations:
        assert relation['type'] == 'located_in' and relation['source'] != relation['target']
        assert {s['contributor_id'] for s in relation['supports']} == set(kinds)

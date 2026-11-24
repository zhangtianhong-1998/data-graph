"""Cover the HTTP merge-only path, its immutable decisions and offline replay."""
import json
from pathlib import Path
import runpy
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.construction import overlap_review
from lightgraph.construction import workflow_overlap
from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.workflow_overlap import overlap_review_skip_reason
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import FusionPolicyConfig, MergePolicyConfig, RunConfig, WorkflowMergeNodeConfig
from lightgraph.tests.construction_worker_fixture import install_scripted_worker, overlapping_metric_proposal
from lightgraph.tests.test_web_api import client, create, upload
from lightgraph.tests.test_web_execution import wait_run


def test_review_of_two_committed_csv_workers_is_replayable_via_http(client, monkeypatch):
    install_scripted_worker(monkeypatch, draft_factory=overlapping_metric_proposal)
    create(client)
    base = '/api/projects/alpha'
    upload(client, 'metrics.csv', b'metric_code,metric_name,class_code,class_name,unit,currency\nK,A,C,Group,kg,EUR\n')
    cfg = client.get(base + '/config').json()
    cfg['goal']['purpose'] = 'Combine metric projections'
    cfg['model'].update(mode='in_environment', model='scripted', base_url='http://localhost:9999/v1',
                        api_key_env='LIGHTGRAPH_TEST_REVIEW_KEY')
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
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content':
                                               json.dumps({'decisions': decisions})}, 'finish_reason': 'stop'}]})

    monkeypatch.setattr(overlap_review.httpx, 'HTTPTransport', lambda: httpx.MockTransport(decide))
    def unexpected_worker(*args, **kwargs):
        raise AssertionError('unexpected worker during checkpoint reuse')
    monkeypatch.setattr(subprocess, 'Popen', unexpected_worker)
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
    assert len(requests) == 1 and node['model_calls'] == 1
    assert node['overlap_review'] == report['summary']
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
    assert audit(run_file, 'join', overlap_patterns_only=True)['overlap_review'] == report
    replay = audit(run_file, 'join')
    assert replay['with_source_replay']['output_nodes'] == replay['committed']['nodes']
    assert replay['with_source_replay']['output_edges'] == replay['committed']['edges']
    assert replay['overlap_review'] == report
    promotion = finished(client.post(base + '/runs/' + run['run_id'] + '/resume',
                                     json={'action': 'publish_artifact', 'artifact_id': artifact['artifact_id']},
                                     headers=headers))
    assert promotion['workflow_state']['merge']['published_artifact_id'] == artifact['artifact_id']
    assert promotion['workflow_state']['merge_nodes']['join']['model_calls'] == 0 and len(requests) == 1
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
    monkeypatch.setattr(workflow_overlap, 'analyze_overlap_patterns',
                        lambda *args: SimpleNamespace(patterns=[{'pattern_id': 'pair'}], summary={}))
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

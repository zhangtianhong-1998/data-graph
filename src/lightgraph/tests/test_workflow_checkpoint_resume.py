"""Replay a two-branch workflow using committed publications and offline workers."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.workflow import load_workflow_state
from lightgraph.construction.workspace import read_internal_yaml
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_web_execution import ProjectWorkspace, wait_run
from lightgraph.web.execution import RunManager, _input_manifest

WORKER = """
import json, sys
from pathlib import Path
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.workspace import GraphWorkspace
from lightgraph.runtime.config import RunConfig

result = Path(sys.argv[1])
config = RunConfig.model_validate(json.load(sys.stdin)['config'])
out = Path(config.output_dir)
node = out.name
project = result.parent.parent
(project / ('started-' + node + '-' + result.stem)).touch()
mode = (project / 'mode').read_text() if node == 'merge' else 'ok'
status = 'completed' if mode == 'ok' else 'partial'
code = 'input_dependency_changed' if mode == 'changed' else None
issues = [{'code': 'published_definition_conflict'}] if mode in {'isolated', 'unverified'} else []
if mode != 'missing':
    ws = GraphWorkspace(out / 'construction', 'ctx')
    graph = {'target': 'graph', 'status': 'published', 'support_visibility_version': 1,
             'access_fingerprint': access_fingerprint(config),
             'definitions': {'entities': [{'type': 'Item', 'key_names': ['key']}], 'relations': []},
             'nodes': [{'id': node, 'type': 'Item', 'identity': {'key': node}, 'label': node,
                        'supports': [{'scope_ref': 'public', 'label': node}]}], 'edges': []}
    if mode == 'isolated':
        graph['conflict_isolation'] = {'policy': 'isolate_label_conflicts_v1', 'nodes': 1,
            'node_ids': ['excluded'], 'edges': 0, 'edge_ids': [], 'candidate_retained': True, 'coverage': 'incomplete'}
    manifest = ws._manifest()
    manifest['current_graph'] = {'context_fingerprint': 'ctx', 'path': ws._append_record('published', graph)}
    ws._save_manifest(manifest)
result.write_text(json.dumps({'status': status, 'error_code': code, 'report': {'issues': issues}, 'cleanup_status': 'completed'}))
"""


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    ws = ProjectWorkspace(tmp_path / 'workspace')
    project = ws.add('alpha')
    config = RunConfig.model_validate({
        'sources': [], 'documents': ws.configs['alpha'].documents, 'output_dir': str(project / 'output'),
        'isolation_domain': 'alpha', 'workflow': {'enabled': True, 'units': [{'unit_id': 'a'}, {'unit_id': 'b'}],
            'merge_nodes': [{'node_id': 'merge', 'merge_mode': 'merge_and_extract'}],
            'edges': [{'edge_id': name, 'source': name, 'target': 'merge'} for name in ('a', 'b')] +
                     [{'edge_id': 'out', 'source': 'merge', 'target': 'output'}]}})
    ws.configs['alpha'] = config
    (project / 'mode').write_text('isolated')
    real_popen = subprocess.Popen
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen',
        lambda args, **kw: real_popen([sys.executable, '-c', WORKER, args[-1]], **kw))
    manager = RunManager(ws)
    try:
        yield manager, project, config
    finally:
        manager.shutdown()


def test_partial_promotion_and_pinned_recovery(workflow):
    manager, project, config = workflow
    origin = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    state = origin['workflow_state']
    assert origin['status'] == 'partial', state
    assert state['merge']['nodes'] == 3 and state['merge']['conflict_isolation']['coverage'] == 'incomplete'
    assert state['merge']['publication_status'] == 'published'
    full = state['artifacts']['merge']
    assert full['graph_kind'] == 'node_full'
    out = Path(config.output_dir)
    original = (out / 'workflow-runs' / (origin['run_id'] + '.json')).read_bytes()
    before = set(project.glob('started-*'))
    with pytest.raises(LightGraphError, match='workflow_terminal_artifact_required'):
        manager.resume('alpha', origin['run_id'], action='publish_artifact', artifact_id=state['artifacts']['a']['artifact_id'])
    second = manager.resume('alpha', origin['run_id'], action='publish_artifact', artifact_id=full['artifact_id'])
    published = wait_run(manager, 'alpha', second['run_id'])
    assert published['status'] == 'partial'
    assert published['workflow_state']['merge']['published_artifact_id'] == full['artifact_id']
    assert set(project.glob('started-*')) == before
    assert (out / 'workflow-runs' / (origin['run_id'] + '.json')).read_bytes() == original

    # An operational partial cannot replace the current project head. Its
    # mechanically merged base is committed and readable for investigation.
    previous_head = read_internal_yaml(out / 'construction' / 'manifest.yaml')['current_graph']
    for mode in ('changed', 'unverified'):
        (project / 'mode').write_text(mode)
        run = wait_run(manager, 'alpha', manager.resume('alpha', origin['run_id'], from_node_id='merge')['run_id'])
        assert run['workflow_units'] == []
        assert run['workflow_state']['reused_nodes'] == ['a', 'b']
        assert 'merge' not in run['workflow_state']['artifacts']
        assert run['workflow_state']['base_artifacts']['merge']['graph_kind'] == 'node_base'
        assert run['workflow_state']['merge']['status'] == 'skipped'
        assert run['workflow_state']['merge']['publication_status'] == 'retained_previous'
        assert read_internal_yaml(out / 'construction' / 'manifest.yaml')['current_graph'] == previous_head

    # Node-level compatibility allows switching only the merge mode/budget or
    # rearranging the canvas, even after activating another output generation.
    config.workflow.merge_nodes[0].merge_mode = 'deterministic'
    config.workflow.units[0].max_calls = 8
    config.output_dir = str(out / 'generations' / ('run_' + 'e' * 24))
    latest = wait_run(manager, 'alpha', manager.resume('alpha', origin['run_id'], from_node_id='merge')['run_id'])
    assert latest['status'] == 'completed', latest
    assert latest['workflow_state']['merge']['nodes'] == 2
    assert latest['workflow_state']['artifacts']['a']['artifact_id'] == state['artifacts']['a']['artifact_id']
    replay = manager.resume('alpha', latest['run_id'], action='publish_artifact',
                            artifact_id=latest['workflow_state']['artifacts']['merge']['artifact_id'])
    assert wait_run(manager, 'alpha', replay['run_id'])['status'] == 'completed'
    workers = list(project.glob('started-*'))
    assert sum(p.name.startswith(('started-a-', 'started-b-')) for p in workers) == 2
    assert sum(p.name.startswith('started-merge-') for p in workers) == 3
    assert manager.get('alpha', origin['run_id'])['workflow_state'] == state
    Path(config.documents[0]).write_text('changed input')
    with pytest.raises(LightGraphError, match='workflow_resume_input_changed'):
        manager.resume('alpha', origin['run_id'], from_node_id='merge')
    assert len(list(project.glob('started-*'))) == len(workers)


@pytest.mark.parametrize('transient', [True, False])
def test_merge_registration_failure_and_pinned_rerun(workflow, monkeypatch, transient):
    import errno
    from lightgraph.construction import snapshot_registry

    manager, project, config = workflow
    config.workflow.merge_nodes[0].merge_mode = 'deterministic'
    register = snapshot_registry._register_access_context
    attempts = []

    def fail(root, cfg, legacy):
        if cfg.workflow is not None and not attempts:
            attempts.append(True)
            # Check what another client could see BEFORE the checkpoint exists.
            running = load_workflow_state(config.output_dir)
            node = running['merge_nodes']['merge']
            assert node['status'] == 'running' and node['artifact_status'] == 'committing'
            assert node['nodes'] == 2 and node['merge_counts']['input_nodes'] == 2
            assert 'merge' not in running['artifacts']
            if transient:
                error = PermissionError(errno.EACCES, 'SECRET-PATH')
                error.winerror = 32
                raise error
            raise OSError(errno.ENOSPC, 'SECRET-PATH')
        return register(root, cfg, legacy)

    monkeypatch.setattr(snapshot_registry, '_register_access_context', fail)
    original = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    state = original['workflow_state']
    assert attempts == [True]
    if transient:
        assert original['status'] == 'completed'
        assert state['merge_nodes']['merge']['artifact_status'] == 'committed'
        assert state['merge']['publication_status'] == 'published'
        return
    node = state['merge_nodes']['merge']
    assert node['status'] == 'failed' and node['error_code'] == 'workflow_access_context_register_failed'
    assert node['artifact_status'] == 'uncommitted' and 'SECRET-PATH' not in str(state)
    assert node['failure_detail'] == {'stage': 'access_context_register', 'exception_type': 'OSError',
                                     'errno': errno.ENOSPC, 'attempts': 1}
    assert node['merge_counts']['output_nodes'] == 2
    assert set(state['artifacts']) == {'a', 'b'} and state['merge']['status'] == 'skipped'
    before = set(project.glob('started-*'))
    rerun = wait_run(manager, 'alpha', manager.resume('alpha', original['run_id'], from_node_id='merge')['run_id'])
    assert rerun['status'] == 'completed' and set(project.glob('started-*')) == before
    assert rerun['workflow_state']['merge']['publication_status'] == 'published'
    for nid in ('a', 'b'):
        assert rerun['workflow_state']['artifacts'][nid] == state['artifacts'][nid]


def test_unwritable_workflow_state_does_not_strand_parent_run(workflow, monkeypatch):
    import errno
    from lightgraph.construction import workflow as workflow_module

    manager, _, config = workflow
    config.workflow.merge_nodes[0].merge_mode = 'deterministic'
    write = workflow_module.atomic_text

    def reject_commit_state(path, payload):
        if path.parent.name == 'workflow-runs' and json.loads(payload).get('merge_nodes', {}).get('merge', {}).get('artifact_status'):
            raise OSError(errno.ENOSPC, 'SECRET-PATH')
        return write(path, payload)

    monkeypatch.setattr(workflow_module, 'atomic_text', reject_commit_state)
    result = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    assert result['status'] == 'failed' and result['stage'] == 'finished'
    assert result['failure_detail']['stage'] == result['state_write_failure']['stage'] == 'state_history_write'
    assert result['failure_detail']['errno'] == errno.ENOSPC and not manager.active('alpha')
    assert 'SECRET-PATH' not in str(result)


def test_legacy_current_run_can_reuse_extracts_but_cannot_promote_delta(workflow):
    manager, project, config = workflow
    run = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    source_run = run['run_id']
    out = Path(config.output_dir)
    state = load_workflow_state(out, source_run)
    for name in ('artifacts', 'node_inputs', 'terminal_ids', 'artifact_outputs', 'plan', 'base_artifacts'):
        state.pop(name, None)
    legacy_plan = config.workflow.model_dump(mode='json')
    legacy_plan['merge_policy'].pop('fusion_policy')
    state['workflow_input'] = digest(access_fingerprint(config), legacy_plan, config.goal_markdown,
                                    config.goal.model_dump(mode='json'), _input_manifest(config, project))
    (out / 'workflow-state.json').write_text(json.dumps(state))
    (out / 'workflow-runs' / (source_run + '.json')).unlink()
    with pytest.raises(LightGraphError, match='workflow_terminal_artifact_required'):
        manager.resume('alpha', source_run, action='publish_artifact', artifact_id='artifact_' + '1' * 32)
    (project / 'mode').write_text('ok')
    replay = wait_run(manager, 'alpha', manager.resume('alpha', source_run, from_node_id='merge')['run_id'])
    assert replay['status'] == 'completed', replay
    assert replay['workflow_state']['rerun_nodes'] == ['merge']
    assert load_workflow_state(out, source_run) == state
    assert len(list(project.glob('started-a-*'))) == len(list(project.glob('started-b-*'))) == 1
    record = replay['workflow_state']['artifacts']['a']
    (out / record['relative_path'] / 'nodes.parquet').write_bytes(b'broken')
    with pytest.raises(LightGraphError, match='workflow_checkpoint_invalid'):
        manager.resume('alpha', replay['run_id'], from_node_id='merge')


def test_failed_merge_leaves_other_terminal_checkpoint_readable(workflow, monkeypatch):
    from lightgraph.construction import merger
    from lightgraph.construction.checkpoints import load_checkpoint
    from lightgraph.runtime.config import WorkflowConfig
    manager, project, config = workflow
    config.workflow = WorkflowConfig.model_validate({
        'enabled': True, 'units': [{'unit_id': 'a'}, {'unit_id': 'b'}],
        'merge_nodes': [{'node_id': 'bad'}, {'node_id': 'good'}],
        'edges': [{'edge_id': 'a1', 'source': 'a', 'target': 'bad'},
                  {'edge_id': 'b1', 'source': 'b', 'target': 'good'},
                  {'edge_id': 'a2', 'source': 'bad', 'target': 'output'},
                  {'edge_id': 'b2', 'source': 'good', 'target': 'output'}]})
    real_merge = merger.merge_construction_graphs

    def isolated_error(inputs, **kwargs):
        if [row['contributor_id'] for row in inputs] == ['a']:
            raise LightGraphError('published_graph_identity_conflict')
        return real_merge(inputs, **kwargs)

    monkeypatch.setattr(merger, 'merge_construction_graphs', isolated_error)
    run = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    state = run['workflow_state']
    assert run['status'] == 'partial'
    assert state['merge_nodes']['bad']['error_code'] == 'published_graph_identity_conflict'
    assert state['merge']['status'] == 'skipped'
    assert len(load_checkpoint(config.output_dir, state['artifacts']['good'])['graph']['nodes']) == 1
    assert not (Path(config.output_dir) / 'construction' / 'manifest.yaml').is_file()

    # Simulate exit after committing b's files but before advancing the state.
    state['artifacts'].pop('b')
    state['ready_unit_attempts'].pop('b')
    out = Path(config.output_dir)
    (out / 'workflow-runs' / (run['run_id'] + '.json')).write_text(json.dumps(state))
    monkeypatch.setattr(merger, 'merge_construction_graphs', real_merge)
    replay = wait_run(manager, 'alpha', manager.resume('alpha', run['run_id'], from_node_id='bad')['run_id'])
    assert replay['status'] == 'completed', replay
    assert replay['workflow_state']['reused_nodes'] == ['a', 'b', 'good']
    assert replay['workflow_state']['rerun_nodes'] == ['bad']
    assert len(list(project.glob('started-*'))) == 2


def test_explicit_offline_recomposition_of_legacy_worker_delta(workflow):
    from lightgraph.construction.checkpoints import load_checkpoint
    from lightgraph.construction.graph_storage import write_graph_record
    from lightgraph.construction.inheritance import freeze_inherited_graphs
    from lightgraph.construction.merger import merge_construction_graphs
    from lightgraph.construction.workflow import build_merge_node_config, load_unit_contributor, unit_output_path
    from lightgraph.construction.workspace import GraphWorkspace, write_yaml

    manager, project, config = workflow
    source = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    rid, out = source['run_id'], Path(config.output_dir)
    old_state = load_workflow_state(out, rid)
    for name in ('artifacts', 'node_inputs', 'terminal_ids', 'artifact_outputs', 'plan', 'base_artifacts'):
        old_state.pop(name, None)
    plan = config.workflow.model_dump(mode='json')
    plan['merge_policy'].pop('fusion_policy')
    old_state['workflow_input'] = digest(access_fingerprint(config), plan, config.goal_markdown,
                                         config.goal.model_dump(mode='json'), _input_manifest(config, project))
    old_state['merge'] = {'status': 'skipped', 'error_code': 'workflow_units_not_ready'}
    node = config.workflow.merge_nodes[0]
    stage = unit_output_path(out, 'merge', rid)
    upstream = merge_construction_graphs([load_unit_contributor(unit_output_path(out, name, rid), name)
        for name in ('a', 'b')], target_access_fingerprint=access_fingerprint(config), merge_policy=config.workflow.merge_policy)
    snapshot = stage / 'upstream.graph'
    write_graph_record(snapshot, upstream)
    scoped = build_merge_node_config(config, node, upstream_snapshot_path=snapshot, upstream_summary='', node_output_dir=stage)
    freeze_inherited_graphs(scoped, stage / 'construction')

    ws = GraphWorkspace(stage / 'construction', 'ctx')
    delta = ws.read_published_graph()
    delta.update(access_fingerprint=access_fingerprint(scoped), extends_base_graph=True)
    upstream_id = upstream['nodes'][0]['id']
    delta['edges'] = [{'id': 'cross', 'type': 'RELATES', 'source': 'merge', 'target': upstream_id,
        'supports': [{'scope_ref': 'public', 'lookup_matches': [{'endpoint': 'target', 'inherited_node_id': upstream_id}]}]}]
    manifest = ws._manifest()
    manifest['current_graph'] = {'context_fingerprint': 'ctx', 'path': ws._append_record('published', delta)}
    ws._save_manifest(manifest)
    worker_rid = old_state['merge_nodes']['merge']['run_id']
    old_state['merge_nodes']['merge'].pop('issue_codes', None)
    report = {'run_id': worker_rid, 'status': 'PARTIAL', 'issues': [{'code': 'published_definition_conflict'}],
              'counts': {'delta_nodes': 1, 'delta_edges': 1}}
    write_yaml(stage / 'report.yaml', report)
    result_path = project / 'runs' / (worker_rid + '.result.json')
    result = {'status': 'partial', 'error_code': None, 'cleanup_status': 'completed', 'report': report}
    result_path.write_text(json.dumps(result))
    (out / 'workflow-runs' / (rid + '.json')).unlink()
    (out / 'workflow-state.json').write_text(json.dumps(old_state))

    original_head = read_internal_yaml(out / 'construction' / 'manifest.yaml')['current_graph']
    frozen_path = stage / 'construction' / 'inherited' / 'summary.yaml'
    saved = frozen_path.read_bytes()
    damaged = read_internal_yaml(frozen_path)
    damaged['inherited'][0]['source_graph_digest'] = '0' * 64
    write_yaml(frozen_path, damaged)
    def recompose():
        return manager.resume('alpha', rid, action='recompose_legacy_merge', from_node_id='merge')
    with pytest.raises(LightGraphError, match='workflow_legacy_upstream_unverified'):
        recompose()
    frozen_path.write_bytes(saved)
    result_path.write_text(json.dumps(dict(result, error_code='input_dependency_changed')))
    with pytest.raises(LightGraphError, match='workflow_legacy_worker_unverified'):
        recompose()
    assert read_internal_yaml(out / 'construction' / 'manifest.yaml')['current_graph'] == original_head

    result_path.write_text(json.dumps(result))
    source_head = (stage / 'construction' / 'manifest.yaml').read_bytes()
    before = set(project.glob('started-*'))
    resumed = wait_run(manager, 'alpha', recompose()['run_id'])
    assert resumed['status'] == 'partial', resumed
    state = resumed['workflow_state']
    assert state['resume_action'] == 'recompose_legacy_merge'
    assert state['reused_nodes'] == ['a', 'b'] and state['rerun_nodes'] == [] and state['recomposed_nodes'] == ['merge']
    assert state['merge']['publication_status'] == 'published'
    assert state['merge']['nodes'] == 3 and state['merge']['edges'] == 1
    assert state['merge']['conflict_isolation']['coverage'] == 'incomplete'
    rec = state['artifacts']['merge']
    assert rec['recomposed_from']['worker_run_id'] == worker_rid and rec['recomposed_from']['delta_graph_digest'] == digest(delta)
    graph = load_checkpoint(out, rec)['graph']
    head = read_internal_yaml(out / 'construction' / 'manifest.yaml')['current_graph']
    root_graph = GraphWorkspace(out / 'construction', head['context_fingerprint']).read_published_graph()
    assert root_graph['nodes'] == graph['nodes'] and root_graph['edges'] == graph['edges']
    assert graph['edges'][0]['target'] == upstream_id
    assert graph['edges'][0]['source'] in {node['id'] for node in graph['nodes']}
    assert load_workflow_state(out, rid) == old_state
    assert (stage / 'construction' / 'manifest.yaml').read_bytes() == source_head
    assert before == set(project.glob('started-*'))

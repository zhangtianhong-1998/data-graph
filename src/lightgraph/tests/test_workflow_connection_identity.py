"""Checkpoint reuse follows the actual environment connection, never only its variable name."""
import json

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import SourceConfig
from lightgraph.tests.test_web_execution import wait_run
from lightgraph.tests.test_workflow_checkpoint_resume import workflow
from lightgraph.web import execution


def database(config):
    config.sources = [SourceConfig(id='db', kind='mysql_database', dsn_env='TEST_WORKFLOW_DSN',
                                   allowed_tables=['public.items'])]
    config.workflow.merge_nodes[0].merge_mode = 'deterministic'


@pytest.mark.parametrize('action', ['rerun_node', 'publish_artifact'])
def test_changed_environment_connection_cannot_republish_old_checkpoints(workflow, monkeypatch, action):
    manager, project, config = workflow
    database(config)
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:first-secret@host-a/erp')
    first = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
    assert first['status'] == 'completed', first
    args = ({'from_node_id': 'merge'} if action == 'rerun_node' else
            {'action': action, 'artifact_id': first['workflow_state']['artifacts']['merge']['artifact_id']})
    same = wait_run(manager, 'alpha', manager.resume('alpha', first['run_id'], **args)['run_id'])
    assert same['status'] == 'completed'
    before = set(project.glob('started-*'))
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:second-secret@host-b/crm')
    with pytest.raises(LightGraphError, match='workflow_resume_input_changed'):
        manager.resume('alpha', first['run_id'], **args)
    assert set(project.glob('started-*')) == before
    serialized = json.dumps(first['workflow_state'])
    assert all(secret not in serialized for secret in ('first-secret', 'host-a', 'mysql://'))


def test_unbound_legacy_environment_digest_is_not_a_connection_proof(workflow, monkeypatch):
    _, project, config = workflow
    database(config)
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:secret@host-a/erp')
    with monkeypatch.context() as old:
        old.setattr(execution, 'environment_source_bindings', lambda _sources: {})
        v1 = execution._workflow_node_inputs(config, project, version=1)
        v2 = execution._workflow_node_inputs(config, project)
    proofs = execution._WorkflowInputProofs(config, project, execution._input_manifest(config, project))
    for archived in (v1, v2):
        with pytest.raises(LightGraphError, match='workflow_resume_input_changed'):
            proofs.match('a', archived['a'], {'input_digest': archived['a']}, [None, str(project)])


def test_missing_environment_connection_invalidates_checkpoint_and_metadata(workflow, monkeypatch):
    _, project, config = workflow
    database(config)
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:secret@host-a/erp')
    first = execution._WorkflowInputProofs(config, project, execution._input_manifest(config, project))
    metadata = execution._metadata_context(config, project)
    monkeypatch.delenv('TEST_WORKFLOW_DSN')
    current = execution._WorkflowInputProofs(config, project, execution._input_manifest(config, project))
    assert execution._metadata_context(config, project) != metadata
    with pytest.raises(LightGraphError, match='workflow_resume_input_changed'):
        current.match('a', first.current['a'], {'input_digest': first.current['a']}, [None])


def test_environment_override_cannot_diverge_from_publication_permissions(workflow, monkeypatch):
    manager, project, config = workflow
    database(config)
    credentials = {'db': 'mysql://reader:first-secret@host-a/erp'}
    monkeypatch.setenv('TEST_WORKFLOW_DSN', credentials['db'])
    first = wait_run(manager, 'alpha', manager.start('alpha', source_credentials=credentials)['run_id'])
    assert first['status'] == 'completed'
    same = manager.resume('alpha', first['run_id'], from_node_id='merge', source_credentials=credentials)
    assert wait_run(manager, 'alpha', same['run_id'])['status'] == 'completed'
    before = set(project.glob('started-*'))
    other = {'db': 'mysql://reader:second-secret@host-b/crm'}
    with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
        manager.resume('alpha', first['run_id'], from_node_id='merge',
                       source_credentials=other)
    with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
        manager.start('alpha', source_credentials=other)
    with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
        execution.read_metadata(manager.workspace, 'alpha', refresh=True, source_credentials=other)
    assert set(project.glob('started-*')) == before


def test_result_read_and_migration_require_recorded_environment_binding(workflow, monkeypatch):
    from lightgraph.construction.access import access_fingerprint
    from lightgraph.construction import snapshot_registry as registry
    _, project, config = workflow
    database(config)
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:secret@host-a/erp')
    root = project / 'authorization'
    policy_id = registry.register_access_context(root, config)
    head = {'access_fingerprint': access_fingerprint(config), 'result_policy_id': policy_id}
    assert registry.authorize_snapshot(root, head, config)
    monkeypatch.setenv('TEST_WORKFLOW_DSN', 'mysql://reader:secret@host-b/crm')
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        registry.authorize_snapshot(root, head, config)
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        registry.migrate_access_context(root, head, config, config)
    legacy_root = project / 'legacy-authorization'
    with monkeypatch.context() as previous:
        previous.setattr(registry, 'environment_source_bindings', lambda _sources: {})
        old_policy = registry.register_access_context(legacy_root, config)
    for legacy_head in ({'access_fingerprint': access_fingerprint(config)},
                        {'access_fingerprint': access_fingerprint(config), 'result_policy_id': old_policy}):
        with pytest.raises(LightGraphError, match='graph_access_context_changed'):
            registry.authorize_snapshot(legacy_root, legacy_head, config)
        with pytest.raises(LightGraphError, match='graph_access_context_changed'):
            registry.migrate_access_context(legacy_root, legacy_head, config, config)

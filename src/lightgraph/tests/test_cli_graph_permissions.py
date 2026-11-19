"""A managed CLI read observes the same live source grants as the workbench."""
import json
from pathlib import Path

import pytest
import yaml

from lightgraph import cli
from lightgraph.construction.inheritance import freeze_inherited_graphs
from lightgraph.tests.test_web_graph_history import publish
from lightgraph.web.workspace import Workspace


@pytest.fixture
def derived(tmp_path):
    ws = Workspace(tmp_path / 'workspace')
    for pid in ('base', 'derived'):
        ws.create(pid, pid)
    publish(ws.result_config('base'), 'base-node')
    raw = ws.get_config('derived')
    raw['embedding'] = {'provider': 'local_hash'}
    raw['inherited_graphs'] = [{'inherit_id': 'base', 'source_project_id': 'base', 'allowed_scopes': ['public']}]
    ws.save_config('derived', raw)
    config = ws.result_config('derived')
    freeze_inherited_graphs(config, Path(config.output_dir) / 'construction', workspace=ws)
    publish(config, 'child-node')
    return ws, config, ws.project_path('derived') / 'config.yaml'


def revoke(ws):
    raw = ws.get_config('base')
    raw['scopes'] = ['restricted']
    ws.save_config('base', raw)


def test_managed_commands_and_automatic_hook_check_revoked_source_even_with_cached_inputs(derived, capsys, caplog):
    ws, config, path = derived
    commands = [
        ['analyze-graph', str(path)],
        ['retrieve-subgraph', str(path), '--sync', '--node-id', 'child-node'],
        ['retrieve-subgraph', str(path), '全图概览'],
    ]
    for command in commands:
        assert cli.main(command) == 0
        capsys.readouterr()
    revoke(ws)
    for command in commands:
        assert cli.main(command) == 1
        assert json.loads(capsys.readouterr().out) == {'status': 'FAILED', 'code': 'inherited_graph_scope_not_authorized'}
    cli._analyze_after_build(path, config)
    assert 'require a retry' in caplog.text


def test_unmanaged_derived_config_requires_live_workspace(derived, tmp_path, capsys):
    ws, _, _ = derived
    path = tmp_path / 'standalone.yaml'
    path.write_text(yaml.safe_dump(ws.get_config('derived')))
    for opts in [['analyze-graph', str(path)], ['retrieve-subgraph', str(path), '--node-id', 'child-node'],
                 ['export-graph', str(path), str(tmp_path / 'out.yaml')]]:
        assert cli.main(opts) == 1
        assert json.loads(capsys.readouterr().out)['code'] == 'inherited_graph_workspace_required'


def test_recheck_grants_after_query_before_cli_emits_results(derived, monkeypatch, capsys):
    from lightgraph.retrieval.query import RetrieveSubgraphRequest
    ws, _, path = derived
    original = RetrieveSubgraphRequest.run

    def change_during_read(self, retriever, scopes):
        answer = original(self, retriever, scopes)
        revoke(ws)
        return answer

    monkeypatch.setattr(RetrieveSubgraphRequest, 'run', change_during_read)
    assert cli.main(['retrieve-subgraph', str(path), '--sync', '--node-id', 'child-node']) == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'FAILED', 'code': 'inherited_graph_scope_not_authorized'}

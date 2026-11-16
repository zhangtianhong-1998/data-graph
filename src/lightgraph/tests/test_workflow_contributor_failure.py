import subprocess
import sys

import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.workflow import build_unit_config
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_web_execution import ProjectWorkspace, wait_run
from lightgraph.web.execution import RunManager


@pytest.mark.parametrize('first_status', ['partial', 'completed'])
def test_unreadable_or_partial_extract_skips_unstarted_merge(tmp_path, monkeypatch, first_status):
    workspace = ProjectWorkspace(tmp_path / 'workspace')
    project = workspace.add('alpha')
    config = RunConfig.model_validate({
        'sources': [], 'documents': workspace.configs['alpha'].documents,
        'output_dir': str(project / 'output'), 'isolation_domain': 'alpha',
        'model': {'mode': 'in_environment', 'model': 'offline', 'base_url': 'https://model.invalid/v1'},
        'workflow': {'enabled': True, 'units': [{'unit_id': 'u1'}, {'unit_id': 'u2'}],
                     'merge_nodes': [{'node_id': 'm1'}],
                     'edges': [{'edge_id': 'e1', 'source': 'u1', 'target': 'm1'},
                               {'edge_id': 'e2', 'source': 'u2', 'target': 'm1'},
                               {'edge_id': 'e3', 'source': 'm1', 'target': 'output'}]},
    })
    workspace.configs['alpha'] = config
    script = '''
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
assert payload['scoped_workflow_run'] is True
out = Path(payload['config']['output_dir'])
(out / 'construction').mkdir(parents=True, exist_ok=True)
(out / 'construction' / 'manifest.yaml').write_text('current_graph: unavailable.yaml\\n')
status = sys.argv[2] if out.name == 'u1' else 'completed'
Path(sys.argv[1]).write_text(json.dumps({'status': status, 'cleanup_status': 'completed'}))
'''
    real_popen = subprocess.Popen
    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen',
                        lambda args, **kwargs: real_popen([sys.executable, '-c', script, args[-1], first_status], **kwargs))
    loaded = []

    def load(out, uid):
        loaded.append(uid)
        if uid == 'u1':
            raise LightGraphError('yaml_document_too_large')
        unit = next(unit for unit in config.workflow.units if unit.unit_id == uid)
        scoped = build_unit_config(config, unit, unit_output_dir=out)
        return {'kind': 'unit', 'contributor_id': uid,
                'graph': {'nodes': [], 'edges': [], 'access_fingerprint': access_fingerprint(scoped)}}

    monkeypatch.setattr('lightgraph.construction.workflow.load_unit_contributor', load)
    manager = RunManager(workspace)
    try:
        result = wait_run(manager, 'alpha', manager.start('alpha')['run_id'])
        assert result['status'] == 'partial', result
        assert result['error_code'] == 'workflow_unit_partial'
        state = result['workflow_state']
        assert state['merge_nodes']['m1']['status'] == 'skipped'
        assert state['merge_nodes']['m1']['run_id'] is None
        assert state['merge_nodes']['m1']['error_code'] == 'workflow_units_not_ready'
        assert ('u1' in loaded) == (first_status == 'completed')
        if first_status == 'completed':
            assert state['units']['u1']['status'] == 'failed'
            assert state['units']['u1']['error_code'] == 'yaml_document_too_large'
        else:
            assert state['units']['u1']['status'] == 'partial'
    finally:
        manager.shutdown()

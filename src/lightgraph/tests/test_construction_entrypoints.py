"""The public entrypoints dispatch one Harness runtime and never fixed rules."""
import json

import pytest
import yaml

from lightgraph import cli
from lightgraph.construction.workspace import dump_yaml, write_yaml
from lightgraph.contracts import RunReport
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig


def configuration(tmp_path):
    config = RunConfig(sources=[], output_dir=str(tmp_path / 'output'))
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')), encoding='utf-8')
    return config, path


@pytest.mark.parametrize('command', ['run', 'discover', 'build-graph'])
def test_every_run_command_routes_to_construction(tmp_path, monkeypatch, capsys, command):
    from lightgraph.construction import runner
    config, path = configuration(tmp_path)
    calls = []
    monkeypatch.setattr(runner, 'run_construction', lambda cfg, adapters, **kw:
                        calls.append((cfg, adapters, kw)) or RunReport(run_id='test'))
    assert cli.main([command, str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == 'test'
    assert len(calls) == 1
    assert calls[0][2]['discover_only'] is (command == 'discover')
    assert calls[0][0].goal.purpose == config.goal.purpose


def test_pipeline_does_not_silently_accept_old_model_or_rules(tmp_path):
    config, _ = configuration(tmp_path)
    with pytest.raises(LightGraphError, match='legacy_model_runtime_removed'):
        run_pipeline(config, [], model=object())
    config.rules = ['old-rules.yaml']
    with pytest.raises(LightGraphError, match='legacy_rule_input_removed'):
        run_pipeline(config, [])


def test_existing_scope_selection_reaches_the_same_source_entrypoint(tmp_path, monkeypatch, capsys):
    from lightgraph.construction import runner
    config, path = configuration(tmp_path)
    config.scopes = ['tenant_a']
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')), encoding='utf-8')
    calls = []
    monkeypatch.setattr(cli, 'open_sources', lambda cfg, **kw: calls.append(('sources', cfg.scopes)) or [])
    monkeypatch.setattr(runner, 'run_construction', lambda cfg, adapters, **kw:
                        calls.append(('construct', cfg.scopes)) or RunReport(run_id='scoped'))
    assert cli.main(['run', str(path)]) == 0
    assert calls == [('sources', ['tenant_a']), ('construct', ['tenant_a'])]


def test_source_tools_do_not_load_the_retired_model_runtime():
    import subprocess
    import sys
    script = '''
import sys
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.runner import run_construction
assert not any(name in sys.modules for name in (
    'agentscope', 'lightgraph.discovery.agentscope_runtime',
    'lightgraph.discovery.engine', 'lightgraph.discovery.llm'))
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


def test_validate_yaml_is_schema_only_and_keeps_native_names(tmp_path, capsys):
    path = tmp_path / 'draft.yaml'
    path.write_text(dump_yaml({'draft_id': '设备档案', 'entities': [
        {'type': '设备', 'key_names': ['编号'], 'mappings': [{'source_id': 'local',
          'table': '档案', 'key_columns': ['资产编号'], 'label_column': '简称'}]}]}), encoding='utf-8')
    assert cli.main(['validate', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['drafts'] == ['设备档案']
    assert result['data_validation'] == result['semantic_assessment'] == 'not_run'
    path.write_text('draft_id: a\ndraft_id: b\n', encoding='utf-8')
    assert cli.main(['validate', str(path)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'duplicate_yaml_key'


def test_report_reads_current_yaml_and_historical_json(tmp_path, capsys):
    report = RunReport(run_id='test', status='COMPLETED_WITH_GAPS')
    write_yaml(tmp_path / 'report.yaml', report)
    assert cli.main(['report', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'COMPLETED_WITH_GAPS'
    (tmp_path / 'old.json').write_text(report.model_dump_json(), encoding='utf-8')
    assert cli.main(['report', str(tmp_path / 'old.json')]) == 0


def test_export_yaml_uses_current_access_guard(tmp_path, monkeypatch, capsys):
    from lightgraph.web import construction_graph
    _, path = configuration(tmp_path)
    target = tmp_path / 'graph.yaml'
    monkeypatch.setattr(construction_graph, 'load_graph', lambda cfg:
                        {'nodes': [{'id': '001', 'label': '设备'}], 'edges': []})
    assert cli.main(['export-graph', str(path), str(target)]) == 0
    assert yaml.safe_load(target.read_text(encoding='utf-8'))['nodes'][0]['id'] == '001'
    assert json.loads(capsys.readouterr().out)['format'] == 'yaml'

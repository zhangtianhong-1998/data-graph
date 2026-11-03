"""Scripted Agent fixture for real worker subprocess and HTTP API regressions.

The real runner, sources, YAML validation and publication execute unchanged.
Only the model runtime is scripted; this never claims discovery-quality evidence.
"""
from contextlib import contextmanager
import subprocess
import sys


def install_scripted_worker(monkeypatch):
    original = subprocess.Popen
    script = ('from lightgraph.tests.construction_worker_fixture import main; '
              'import sys; sys.exit(main())')

    def child(args, **kwargs):
        return original([sys.executable, '-c', script, args[-1]], **kwargs)

    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)


def main():
    from lightgraph.construction import runner
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.web.worker import main as worker_main

    captured = {}

    @contextmanager
    def bridge(tools):
        captured['tools'] = tools
        yield 'http://127.0.0.1:9998/mcp', 'scripted-domain-token'

    def scripted_runtime(intent, **options):
        tools = captured['tools']
        records = tools.workspace.read_summary()['publications']
        if records:
            return {'finish_reason': 'completed', 'budget': {'calls': 1}}
        table = tools.call('describe_sources')['tables'][0]
        proposal = {'draft_id': 'metrics', 'purpose': tools.config.goal.purpose,
            'entities': [{'type': 'Metric', 'key_names': ['code'],
                'mappings': [{'source_id': table['source'], 'schema': table['schema'],
                    'table': table['name'], 'key_columns': ['metric_code'],
                    'label_column': 'metric_name'}]}]}
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))
        assert saved['status'] == 'ok', saved
        checked = tools.call('validate_graph_draft', draft_id='metrics')
        assert checked.get('passed'), checked
        published = tools.call('publish_graph_draft', draft_id='metrics')
        assert published['status'] == 'ok', published
        return {'finish_reason': 'completed', 'budget': {'calls': 1}}

    original = runner.run_construction
    runner.serve_local_tools = bridge

    def construction(*args, **kwargs):
        return original(*args, **kwargs, runtime=scripted_runtime)

    runner.run_construction = construction
    return worker_main()

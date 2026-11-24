"""Scripted Agent fixture for real worker subprocess and HTTP API regressions.

The real runner, sources, YAML validation and publication execute unchanged.
Only the model runtime is scripted; this never claims discovery-quality evidence.
"""
from contextlib import contextmanager
import subprocess
import sys


def metric_proposal(tools, table):
    return {'draft_id': 'metrics', 'purpose': tools.config.goal.purpose,
            'entities': [{'type': 'Metric', 'key_names': ['code'],
                'mappings': [{'source_id': table['source'], 'schema': table['schema'],
                    'table': table['name'], 'key_columns': ['metric_code'],
                    'label_column': 'metric_name'}]}]}


def overlapping_metric_proposal(tools, table):
    kind = tools.config.goal_markdown.splitlines()[-1]
    field = {'IncomeMetric': 'unit', 'LedgerItem': 'currency'}[kind]
    binding = {'source_id': table['source'], 'schema': table['schema'], 'table': table['name']}
    address = f"{table['source']}:{table['schema']}.{table['name']}"
    ref = tools.call('inspect_source', table=address, operation='get_metadata')['evidence_ref']
    return {'draft_id': 'metrics', 'entities': [
        {'type': kind, 'key_names': ['code'], 'identity_scope': 'global', 'identity_domain': 'test.metrics',
         'identity_evidence_refs': [ref], 'mappings': [{**binding, 'key_columns': ['metric_code'],
         'label_column': 'metric_name', 'properties': {field: field}, 'applicability': {'status': 'global'}}]},
        {'type': 'MetricCategory', 'key_names': ['class_code'], 'mappings': [
            {**binding, 'key_columns': ['class_code'], 'label_column': 'class_name'}]}],
        'relations': [{'type': 'belongs_to', 'source_type': kind, 'target_type': 'MetricCategory',
                       'mappings': [{**binding, 'source_key_columns': ['metric_code'],
                                     'target_key_columns': ['class_code']}]}]}


def hierarchy_proposal(tools, table):
    kind = tools.config.goal_markdown.splitlines()[-1]
    binding = {'source_id': table['source'], 'schema': table['schema'], 'table': table['name']}
    address = f"{table['source']}:{table['schema']}.{table['name']}"
    evidence = tools.call('inspect_source', table=address, operation='get_metadata')['evidence_ref']
    return {'draft_id': 'tree', 'entities': [{'type': kind, 'key_names': ['code'], 'identity_scope': 'global',
        'identity_domain': kind, 'identity_evidence_refs': [evidence],
        'mappings': [{**binding, 'key_columns': [position + '_code'], 'label_column': position + '_name'}
                     for position in ('parent', 'child')]}],
        'relations': [{'type': kind + 'Children', 'source_type': kind, 'target_type': kind,
            'hierarchy_orientation': 'parent_to_child', 'mappings': [
                {**binding, 'source_key_columns': ['parent_code'], 'target_key_columns': ['child_code']}]}]}


def install_scripted_worker(monkeypatch, *, draft_factory=metric_proposal):
    original = subprocess.Popen
    script = (f'from lightgraph.tests.construction_worker_fixture import main, {draft_factory.__name__}; '
              f'import sys; sys.exit(main({draft_factory.__name__}))')

    def child(args, **kwargs):
        return original([sys.executable, '-c', script, args[-1]], **kwargs)

    monkeypatch.setattr('lightgraph.web.execution.subprocess.Popen', child)


def main(draft_factory=metric_proposal):
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
        proposal = draft_factory(tools, table)
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))
        assert saved['status'] == 'ok', saved
        checked = tools.call('validate_graph_draft', draft_id=proposal['draft_id'])
        assert checked.get('passed'), checked
        published = tools.call('publish_graph_draft', draft_id=proposal['draft_id'])
        assert published['status'] == 'ok', published
        return {'finish_reason': 'completed', 'budget': {'calls': 1}}

    original = runner.run_construction
    runner.serve_local_tools = bridge

    def construction(*args, **kwargs):
        return original(*args, **kwargs, runtime=scripted_runtime)

    runner.run_construction = construction
    return worker_main()

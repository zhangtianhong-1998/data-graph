"""Incomplete source reads must not look like publishable label-only conflicts."""
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.construction import runner
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def source():
    table = TableSpec(source_id='s', name='items',
        columns=[ColumnSpec(name=c) for c in ['code', 'name', 'alternate']])
    return MemorySource([table], {table.qualified_name: [
        ('A', 'Old', 'x'*3000), ('A', 'New', 'y'*3000), ('B', 'Safe', 'z'*3000)]})


def authored():
    return {'draft_id': 'objects', 'entities': [{'type': 'Object', 'key_names': ['code'],
        'mappings': [
            {'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'label_column': 'name'},
            {'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'alias_columns': ['alternate']},
        ]}]}


def test_return_exhaustion_precedes_conflict_advice_and_retains_resumable_draft(tmp_path):
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, max_returned_bytes=1024)
    with source() as adapter:
        tools = ConstructionTools(config, tmp_path/'work', adapters=[adapter])
        try:
            assert tools.call('describe_sources')['query_budget']['max_returned_bytes'] == 1024
            tools.call('save_graph_draft', yaml_text=dump_yaml(authored()))
            result = tools.call('validate_graph_draft', draft_id='objects')
            assert not result['passed']
            assert result['publishable_subset'] is None
            assert set(result['diagnostic_codes']) == {
                'same_identity_conflicting_label', 'returned_bytes_budget_exhausted'}
            assert result['failure_class'] == 'resource'
            assert result['recovery'] == 'run_capacity_change_required'
            assert 'does not reset' in result['next_action']
            assert 'incomplete' in result['next_action']
            budget = result['query_budget']
            assert budget['returned_bytes'] > 1024
            assert budget['remaining_returned_bytes'] == 0
            assert result['effective_limits']['max_returned_bytes'] == 1024
            diagnostic = next(d for d in result['diagnostics'] if d['code'] == 'returned_bytes_budget_exhausted')
            assert diagnostic['query_budget']['config_field'] == 'max_returned_bytes'
            assert diagnostic['query_budget']['max_returned_bytes'] == 1024
            failed_id = result['validation_id']
            query_count = adapter.query_budget.queries
            blocked = tools.call('validate_graph_draft', draft_id='objects')
            assert blocked['queries_stopped'] is True
            assert blocked['query_budget']['returned_bytes'] == budget['returned_bytes']
            assert adapter.query_budget.queries == query_count
            assert tools.call('publish_graph_draft', draft_id='objects', validation_id=failed_id)[
                'code'] == 'graph_validation_required'
            assert tools.workspace.read_published_graph()['nodes'] == []
        finally:
            tools.close()

    # Changing a resource allowance does not erase or invalidate authored work.
    # A new run still performs a complete validation of the retained mapping.
    config.max_returned_bytes = None
    with source() as adapter:
        tools = ConstructionTools(config, tmp_path/'work', adapters=[adapter])
        try:
            assert tools.workspace.get_draft('objects').revision == 1
            result = tools.call('validate_graph_draft', draft_id='objects')
            assert set(result['diagnostic_codes']) == {'same_identity_conflicting_label'}
            assert result['validation_id'] != failed_id
            assert result['publishable_subset']['nodes'] == 1
            assert tools.call('publish_graph_draft', draft_id='objects')['status'] == 'ok'
            graph = tools.workspace.read_published_graph()
            assert [n['identity']['code'] for n in graph['nodes']] == ['B']
        finally:
            tools.close()


def test_report_records_return_allowance_and_consumption(tmp_path):
    config = RunConfig(sources=[], output_dir=str(tmp_path/'output'),
        model={'mode': 'in_environment', 'base_url': 'http://127.0.0.1:9999/v1',
               'model': 'scripted-test'}, max_returned_bytes=1024)
    with source() as adapter:
        def runtime(*args, **kwargs):
            # Source accounting is shared with the tool host even for borrowed adapters.
            adapter.query_budget.charge_return(2, 100)
            return {'finish_reason': 'completed', 'budget': {'calls': 0}}
        report = runner.run_construction(config, adapters=[adapter], runtime=runtime)
    assert report.validation['query_budget']['max_returned_bytes'] == 1024
    assert report.validation['query_budget']['returned_bytes'] == 100
    assert report.counts['source_returned_rows'] == 2
    assert report.counts['source_returned_bytes'] == 100

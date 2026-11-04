"""Runner regressions with real source tools and an explicitly scripted runtime.

These tests validate orchestration, permissions and durable results. They do not
exercise a real model or establish autonomous discovery quality.
"""
from contextlib import contextmanager

import pytest

from lightgraph.construction import runner
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig


def configuration(tmp_path, **model):
    source = tmp_path / 'items.csv'
    source.write_text('代号,中文名\n001,甲\n002,乙\n', encoding='utf-8')
    return RunConfig.model_validate({
        'goal': {'purpose': '发现资料中的对象'},
        'sources': [{'id': 'local', 'kind': 'csv_file', 'path': str(source),
                     'encoding': 'utf-8', 'delimiter': ',', 'header': True,
                     'null_values': [''], 'type_inference': 'text'}],
        'model': {'mode': 'in_environment', 'base_url': 'http://127.0.0.1:9999/v1',
                  'model': 'scripted-test', 'max_calls': 12, **model},
        'output_dir': str(tmp_path / 'output'), 'max_run_seconds': 120,
    })


@pytest.fixture
def scripted_bridge(monkeypatch):
    """Keep real domain tools; only replace runtime networking in these tests."""
    bound = {}

    @contextmanager
    def serve(tools):
        bound['tools'] = tools
        yield 'http://127.0.0.1:9998/mcp', 'test-local-token'

    monkeypatch.setattr(runner, 'serve_local_tools', serve)
    return bound


def save_object(tools):
    table = tools.call('describe_sources')['tables'][0]
    proposal = {'draft_id': '资料对象', 'entities': [{'type': '资料对象',
        'key_names': ['代号'], 'mappings': [{'source_id': table['source'],
            'schema': table['schema'], 'table': table['name'],
            'key_columns': ['代号'], 'label_column': '中文名'}]}]}
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))['status'] == 'ok'
    return proposal


def completed(calls=1):
    return {'finish_reason': 'completed', 'budget': {'calls': calls}}


def test_effective_options_preserve_tokens_timeout_calls_and_window(tmp_path, monkeypatch):
    config = configuration(tmp_path, max_prompt_tokens=1048576, max_output_tokens=65536,
        timeout_seconds=600, max_calls=None, working_context_tokens=32768, retries=1)
    monkeypatch.setenv('LIGHTGRAPH_DSH_BIN', '/explicit/runtime')
    selected = runner.runtime_options(config)
    assert selected.max_input_tokens == 1048576
    assert selected.max_output_tokens == 65536
    assert selected.timeout_seconds == 600
    assert selected.working_context_tokens == 32768
    assert selected.max_calls == 2**53 - 1
    assert selected.max_run_seconds == 120
    assert selected.dsh_bin == '/explicit/runtime'
    config.model.context_window_tokens = 100000
    assert runner.runtime_options(config).max_input_tokens == 34464
    config.model.max_calls = 0
    with pytest.raises(LightGraphError, match='construction_call_budget_exhausted'):
        runner.runtime_options(config)
    config.model.mode = 'disabled'
    assert runner.runtime_options(config) is None


def test_no_automatic_validation_or_publication(tmp_path, scripted_bridge):
    config = configuration(tmp_path)

    def runtime(intent, **options):
        assert '发现资料中的对象' in intent
        assert options['mcp_url'].endswith('/mcp')
        assert options['environment']['LIGHTGRAPH_CONSTRUCTION_TOKEN'] == 'test-local-token'
        save_object(scripted_bridge['tools'])
        return completed(3)

    report = runner.run_construction(config, runtime=runtime)
    assert report.status == 'COMPLETED_WITH_GAPS'
    assert report.rules_published == []
    assert report.counts['business_nodes'] == 0
    assert report.counts['llm_calls'] == 0
    assert report.counts['simulated_model_calls'] == 3
    assert report.validation['D'] == 'test_double'
    tools = scripted_bridge['tools']
    record = tools.workspace.get_draft('资料对象')
    assert record.status == 'candidate' and record.validation is None
    result = safe_load_yaml((tools.workspace.root / 'runtime-result.yaml').read_text())
    assert result['execution_kind'] == 'test_double'


def test_discover_only_enforced_below_prompt_and_validation_can_complete(tmp_path, scripted_bridge):
    config = configuration(tmp_path)

    def runtime(intent, **options):
        tools = scripted_bridge['tools']
        save_object(tools)
        assert tools.call('validate_graph_draft', draft_id='资料对象')['passed']
        refused = tools.call('publish_graph_draft', draft_id='资料对象')
        assert refused['code'] == 'construction_publication_disabled'
        return completed()

    report = runner.run_construction(config, discover_only=True, runtime=runtime)
    assert report.status == 'COMPLETED'
    assert report.counts['business_nodes'] == 0
    assert scripted_bridge['tools'].workspace.get_draft('资料对象').status == 'validated'
    assert not scripted_bridge['tools'].workspace.read_summary()['publications']


@pytest.mark.parametrize('scopes,visible_count', [(['A'], 0), (['A', 'B', 'public'], 2), ([], 0)])
def test_result_scopes_filter_counts_without_changing_existing_configuration(tmp_path, scripted_bridge, scopes, visible_count):
    config = configuration(tmp_path)
    config.scopes = scopes
    original_sources = config.sources[0].model_dump(mode='json')

    def runtime(intent, **options):
        tools = scripted_bridge['tools']
        assert tools.config.scopes == scopes
        save_object(tools)
        assert tools.call('validate_graph_draft', draft_id='资料对象')['passed']
        published = tools.call('publish_graph_draft', draft_id='资料对象')
        assert published['status'] == 'ok' and published['nodes'] == visible_count
        return completed()

    report = runner.run_construction(config, runtime=runtime)
    assert report.status == 'COMPLETED'
    assert report.counts['business_nodes'] == visible_count
    assert config.scopes == scopes
    assert config.sources[0].model_dump(mode='json') == original_sources
    if not visible_count:
        assert report.validation['business_graph_reason'] == 'scope_filtered'
        assert any(issue['code'] == 'business_graph_filtered_by_scopes' for issue in report.issues)


def test_runtime_failure_keeps_published_revision_when_new_candidate_exists(tmp_path, scripted_bridge):
    config = configuration(tmp_path)

    def runtime(intent, **options):
        tools = scripted_bridge['tools']
        draft = save_object(tools)
        assert tools.call('validate_graph_draft', draft_id='资料对象')['passed']
        assert tools.call('publish_graph_draft', draft_id='资料对象')['status'] == 'ok'
        draft['unresolved'] = ['新增属性仍需证据']
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['revision'] == 2
        raise LightGraphError('scripted_transport_failed')

    report = runner.run_construction(config, runtime=runtime)
    assert report.status == 'PARTIAL'
    assert report.rules_published == ['资料对象']
    assert report.counts['rules_published'] == 1
    assert report.counts['business_nodes'] == 2
    assert {'code': 'scripted_transport_failed', 'status': 'partial'} in report.issues
    assert any(i['code'] == 'construction_goal_gap' and i['message'] == '新增属性仍需证据' for i in report.issues)
    workspace = scripted_bridge['tools'].workspace
    assert workspace.get_draft('资料对象').status == 'candidate'
    assert workspace.read_summary()['publications'][0]['revision'] == 1
    assert len(workspace.read_published_graph()['nodes']) == 2


def test_metadata_mode_cannot_query_validate_or_republish_previous_graph(tmp_path, scripted_bridge):
    config = configuration(tmp_path)

    def publish(intent, **options):
        tools = scripted_bridge['tools']
        save_object(tools)
        assert tools.call('validate_graph_draft', draft_id='资料对象')['passed']
        assert tools.call('publish_graph_draft', draft_id='资料对象')['status'] == 'ok'
        return completed()

    assert runner.run_construction(config, runtime=publish).status == 'COMPLETED'
    fingerprint = scripted_bridge['tools'].workspace.context_fingerprint
    config.model.mode = 'metadata_only'

    def metadata(intent, **options):
        tools = scripted_bridge['tools']
        assert tools.workspace.context_fingerprint == fingerprint
        assert tools.adapters[0].metadata_only
        assert tools.workspace.get_draft('资料对象').status == 'published'
        table = tools.call('describe_sources')['tables'][0]['table']
        assert tools.call('inspect_source', table=table, operation='get_metadata')['status'] == 'ok'
        for operation, arguments in [('profile_columns', {'table': table, 'columns': ['代号']}),
                ('execute_readonly_sql', {'sql': 'SELECT * FROM public.items'})]:
            assert tools.call('inspect_source', table=table, operation=operation,
                              arguments=arguments)['code'] == 'metadata_only_query_forbidden'
        for operation in ['validate_graph_draft', 'publish_graph_draft']:
            assert tools.call(operation, draft_id='资料对象')['code'] == 'metadata_only_query_forbidden'
        return completed()

    report = runner.run_construction(config, runtime=metadata)
    assert report.status == 'COMPLETED'
    assert report.counts['business_nodes'] == 2  # retained artifact, not fresh inference


def test_prepare_and_mcp_start_consume_one_deadline(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    bound = {}

    def construct(*args, **kwargs):
        tools = ConstructionTools(*args, **kwargs)
        clock[0] += 30
        bound['tools'] = tools
        return tools

    @contextmanager
    def serve(tools):
        clock[0] += 20
        yield 'http://127.0.0.1:9998/mcp', 'token'

    monkeypatch.setattr(runner, 'ConstructionTools', construct)
    monkeypatch.setattr(runner, 'serve_local_tools', serve)

    def runtime(intent, **options):
        assert options['options'].max_run_seconds == 70
        tools = bound['tools']
        assert tools.budget.deadline_monotonic == 220
        assert tools.adapters[0].query_budget.deadline == 220
        return completed()

    runner.run_construction(config, runtime=runtime)


def test_source_change_retains_history_without_counting_old_graph_as_current(tmp_path, scripted_bridge):
    config = configuration(tmp_path)

    def publish(intent, **options):
        tools = scripted_bridge['tools']
        save_object(tools)
        assert tools.call('validate_graph_draft', draft_id='资料对象')['passed']
        assert tools.call('publish_graph_draft', draft_id='资料对象')['status'] == 'ok'
        return completed()

    assert runner.run_construction(config, runtime=publish).status == 'COMPLETED'
    workspace = scripted_bridge['tools'].workspace
    old_files = set((workspace.root / 'published').glob('*.yaml'))
    with open(config.sources[0].path, 'a', encoding='utf-8') as stream:
        stream.write('003,丙\n')
    report = runner.run_construction(config, runtime=lambda *a, **k: completed())
    assert report.status == 'COMPLETED_WITH_GAPS'
    assert report.counts['business_nodes'] == 0
    assert report.rules_published == []
    assert scripted_bridge['tools'].workspace.get_draft('资料对象').status == 'stale'
    assert old_files <= set((workspace.root / 'published').glob('*.yaml'))


def test_expired_or_cancelled_runtime_is_not_started(tmp_path, scripted_bridge):
    config = configuration(tmp_path)
    calls = []
    report = runner.run_construction(config, runtime=lambda *a, **k: calls.append(True),
                                     cancel_requested=lambda: True)
    assert not calls
    assert report.status == 'PARTIAL'
    assert report.issues[0]['code'] == 'construction_cancelled'


def test_source_initialization_error_is_reported(tmp_path, monkeypatch):
    config = configuration(tmp_path)

    def broken(*args, **kwargs):
        raise LightGraphError('construction_source_unavailable')

    monkeypatch.setattr(runner, 'ConstructionTools', broken)
    report = runner.run_construction(config)
    assert report.status == 'PARTIAL'
    assert report.issues == [{'code': 'construction_source_unavailable', 'status': 'partial'}]
    stored = safe_load_yaml((tmp_path / 'output' / 'report.yaml').read_text())
    assert stored['status'] == 'PARTIAL'


def test_disabled_model_never_starts_runtime(tmp_path, scripted_bridge, monkeypatch):
    config = configuration(tmp_path, mode='disabled')
    calls = []
    sources = []

    def construct(*args, **kwargs):
        tools = ConstructionTools(*args, **kwargs)
        sources.extend(tools.adapters)
        return tools

    monkeypatch.setattr(runner, 'ConstructionTools', construct)
    report = runner.run_construction(config, runtime=lambda *a, **k: calls.append(True))
    assert not calls
    assert report.status == 'COMPLETED'
    assert report.counts['metadata_tables'] == 1
    assert sources[0].metadata_only


def test_http_publication_capability_is_enforced(tmp_path):
    """Real MCP HTTP transport; the scripted client is not a model test."""
    import anyio
    import httpx
    import json
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from lightgraph.construction.mcp_server import serve_local_tools

    tools = ConstructionTools(configuration(tmp_path), tmp_path / 'work', allow_publication=False)
    try:
        with serve_local_tools(tools) as (url, token):
            async def request():
                async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}, trust_env=False) as http:
                    async with streamable_http_client(url, http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            response = await session.call_tool('publish_graph_draft', {'draft_id': 'anything'})
                            value = response.structuredContent or json.loads(response.content[0].text)
                            assert value['code'] == 'construction_publication_disabled'
            anyio.run(request)
    finally:
        tools.close()

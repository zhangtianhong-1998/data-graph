"""Published graph authorization is checked again at the web read boundary."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml, safe_load_yaml, write_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.web.construction_graph import graph_details, load_graph


@pytest.fixture
def published(tmp_path):
    source = tmp_path / 'source.csv'
    source.write_text('编号,名称\n001,设备甲\n', encoding='utf-8')
    config = RunConfig.model_validate({
        'sources': [{'id': 'local', 'kind': 'csv_file', 'path': str(source),
                     'encoding': 'utf-8', 'delimiter': ',', 'header': True,
                     'null_values': [''], 'type_inference': 'text'}],
        'model': {'mode': 'in_environment'}, 'output_dir': str(tmp_path / 'output'),
        'goal': {'purpose': '发现授权资料中的对象'},
    })
    tools = ConstructionTools(config, tmp_path / 'output' / 'construction')
    try:
        table = tools.describe_sources()['tables'][0]
        draft = {'draft_id': '设备', 'entities': [{'type': '设备', 'key_names': ['编号'],
            'mappings': [{'source_id': table['source'], 'schema': table['schema'],
                         'table': table['name'], 'key_columns': ['编号'], 'label_column': '名称'}]}]}
        for name in ('设备', '另一类'):
            current = deepcopy(draft)
            current['draft_id'] = name
            current['entities'][0]['type'] = name
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(current))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id=name)['passed']
            assert tools.call('publish_graph_draft', draft_id=name)['status'] == 'ok'
        yield config, tools
    finally:
        tools.close()


def test_program_access_fingerprint_bound_to_validation_and_merged_head(published):
    config, tools = published
    expected = access_fingerprint(config)
    graph = load_graph(config)
    assert len(graph['nodes']) == 2
    assert graph['access_fingerprint'] == expected
    manifest = safe_load_yaml((tools.workspace.root / 'manifest.yaml').read_text())
    assert manifest['current_graph']['access_fingerprint'] == expected
    for publication in tools.workspace.read_summary()['publications']:
        artifact = safe_load_yaml((tools.workspace.root / publication['graph']).read_text())
        assert artifact['access_fingerprint'] == expected
        assert tools.workspace.get_draft(publication['draft_id']).validation.graph_digest


@pytest.mark.parametrize('change', [
    lambda config: setattr(config, 'isolation_domain', 'another_tenant'),
    lambda config: setattr(config, 'document_scopes', ['restricted']),
    lambda config: setattr(config.goal, 'source_scope', ['other_source']),
    lambda config: setattr(config.goal, 'excluded_columns', ['名称']),
    lambda config: setattr(config.goal, 'evidence_only_columns', ['编号']),
    lambda config: setattr(config.goal, 'entity_kinds', ['Other']),
    lambda config: setattr(config.goal, 'relation_kinds', ['Other']),
    lambda config: setattr(config.sources[0], 'allowed_tables', ['another_table']),
    lambda config: setattr(config.sources[0], 'allowed_columns', {'public.source': ['编号']}),
    lambda config: setattr(config.sources[0], 'credential_revision', 'revision2'),
    lambda config: setattr(config.sources[0], 'path', '/new/source.csv'),
])
def test_access_change_denies_old_graph_without_removing_it(published, change):
    config, tools = published
    artifacts = {path: path.read_bytes() for path in (tools.workspace.root / 'published').glob('*.yaml')}
    changed = config.model_copy(deep=True)
    change(changed)
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        load_graph(changed)
    assert all(path.read_bytes() == content for path, content in artifacts.items())
    assert len(load_graph(config)['nodes']) == 2


def test_model_budget_and_goal_wording_do_not_change_read_permissions(published):
    config, _ = published
    changed = config.model_copy(deep=True)
    changed.model.mode = 'metadata_only'
    changed.model.model = 'another_model'
    changed.model.max_prompt_tokens = 1048576
    changed.model.max_output_tokens = 65536
    changed.model.timeout_seconds = 600
    changed.model.max_calls = 240
    changed.max_run_seconds = 7200
    changed.goal.purpose = '同一授权范围下的另一种表述'
    assert access_fingerprint(changed) == access_fingerprint(config)
    assert len(load_graph(changed)['nodes']) == 2


def test_head_and_artifact_must_both_match_current_access(published):
    config, tools = published
    path = tools.workspace.root / 'manifest.yaml'
    manifest = safe_load_yaml(path.read_text())
    graph_path = tools.workspace.root / manifest['current_graph']['path']
    graph = safe_load_yaml(graph_path.read_text())
    graph.pop('access_fingerprint')
    write_yaml(graph_path, graph)
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        load_graph(config)


def test_old_unscoped_head_is_not_implicitly_authorized(published):
    config, tools = published
    path = tools.workspace.root / 'manifest.yaml'
    manifest = safe_load_yaml(path.read_text())
    manifest['current_graph'].pop('access_fingerprint')
    write_yaml(path, manifest)
    with pytest.raises(LightGraphError, match='graph_access_context_changed'):
        load_graph(config)


@pytest.mark.parametrize('permissions', [
    {'scopes': ['A']}, {'scopes': ['public', 'A']}, {'scopes': []},
    {'document_scopes': ['A']},
])
def test_custom_scopes_preserve_source_connection_behavior(tmp_path, monkeypatch, permissions):
    opened = []
    def open_existing(config, **kwargs):
        opened.append((config.scopes, config.document_scopes, kwargs))
        return []
    monkeypatch.setattr('lightgraph.construction.tools.open_sources', open_existing)
    config = RunConfig(sources=[], **permissions)
    tools = ConstructionTools(config, tmp_path / 'work')
    tools.close()
    assert opened == [(config.scopes, config.document_scopes,
                       {'metadata_only': True, 'isolate_database_failures': True})]
    assert (tmp_path / 'work').exists()


def test_custom_scope_historical_store_remains_filtered(tmp_path):
    from types import SimpleNamespace
    from lightgraph.store import GraphStore
    from lightgraph.tests.test_graph_store import node, prop, publish
    from lightgraph.web.execution import read_graph

    config = RunConfig(sources=[], output_dir=str(tmp_path / 'output'), scopes=['A'])
    with GraphStore(tmp_path / 'output' / 'graph.sqlite') as store:
        publish(store, [node('a'), prop('label', 'A可见', 'a')], scope='A', rule='a')
        publish(store, [node('b'), prop('label', 'B不可见', 'b')], scope='B', rule='b')
    workspace = SimpleNamespace(resolved_config=lambda _pid: config)
    historical = read_graph(workspace, 'project')
    assert [entry['label'] for entry in historical['nodes']] == ['A可见']
    assert 'B不可见' not in str(historical)


def test_read_scope_changes_filter_public_graph_without_changing_authority(published):
    config, _ = published
    changed = config.model_copy(deep=True)
    changed.scopes = ['A']
    assert access_fingerprint(changed) == access_fingerprint(config)
    assert load_graph(changed)['nodes'] == []
    changed.scopes = ['A', 'public']
    assert len(load_graph(changed)['nodes']) == 2


def test_repeated_pages_and_two_hops_read_the_published_file_once(published, monkeypatch):
    import lightgraph.web.construction_graph as views
    from lightgraph.web.graph_view_cache import clear_graph_views, flush_graph_views

    config, _tools = published
    clear_graph_views()
    calls = {'n': 0}
    original = GraphWorkspace.read_published_graph

    def wrapped(self):
        calls['n'] += 1
        return original(self)

    monkeypatch.setattr(GraphWorkspace, 'read_published_graph', wrapped)
    graph = load_graph(config)
    node_id = graph['nodes'][0]['id']
    details = graph_details(config, entity_id=node_id)
    assert details['supports']
    monkeypatch.setattr(views, 'digest', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('paged view digested the graph')))
    page = views.graph_page(config, limit=1000)
    focused = views.graph_page(config, focus=node_id, hops=2, limit=1000)
    assert calls['n'] == 1
    assert 'supports' not in page['nodes'][0] and 'provenance' not in page['nodes'][0]
    assert focused['generation'] == page['generation']
    assert focused['focus_hops'] == 2 and focused['nodes'][0]['id'] == node_id
    flush_graph_views()
    clear_graph_views()
    restored = views.graph_page(config, focus=node_id, hops=2, limit=1000)
    assert calls['n'] == 1
    assert restored['generation'] == page['generation']
    assert restored['nodes'][0]['id'] == node_id


def test_replacing_the_published_file_drops_the_cached_view(published, monkeypatch):
    import os

    config, tools = published
    load_graph(config)
    calls = {'n': 0}
    original = GraphWorkspace.read_published_graph

    def wrapped(self):
        calls['n'] += 1
        return original(self)

    monkeypatch.setattr(GraphWorkspace, 'read_published_graph', wrapped)
    assert load_graph(config)['nodes']
    assert calls['n'] == 0
    manifest = safe_load_yaml((tools.workspace.root / 'manifest.yaml').read_text())
    path = tools.workspace.root / manifest['current_graph']['path']
    before = path.stat().st_mtime_ns
    payload = path.read_bytes()
    path.write_bytes(payload)
    if path.stat().st_mtime_ns == before:
        os.utime(path, ns=(path.stat().st_atime_ns, before + 1))
    assert len(load_graph(config)['nodes']) == 2
    assert calls['n'] == 1

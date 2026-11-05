"""Safety guarantees migrated from the former fixed-rule publication pipeline."""
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def definition(name):
    return {'draft_id': name, 'entities': [{'type': '对象', 'key_names': ['编号'],
        'mappings': [{'source_id': 's', 'table': name, 'key_columns': [column]}
                     for column in ('起点', '终点')]}],
        'relations': [{'type': '有向关系', 'source_type': '对象', 'target_type': '对象',
            'acyclic': True, 'mappings': [{'source_id': 's', 'table': name,
                'source_key_columns': ['起点'], 'target_key_columns': ['终点']}]}]}


def test_individually_acyclic_drafts_cannot_publish_a_joint_cycle(tmp_path):
    tables = [TableSpec(source_id='s', name=name, columns=[ColumnSpec(name=c) for c in ('起点', '终点')])
              for name in ('first', 'reverse', 'independent')]
    rows = {'public.first': [('A', 'B')], 'public.reverse': [('B', 'A')],
            'public.independent': [('X', 'Y')]}
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    with MemorySource(tables, rows) as adapter:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[adapter])
        try:
            for name in ('first', 'reverse', 'independent'):
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(definition(name)))['status'] == 'ok'
                assert tools.call('validate_graph_draft', draft_id=name)['passed']
            assert tools.call('publish_graph_draft', draft_id='first')['status'] == 'ok'
            previous = tools.workspace.read_published_graph()
            rejected = tools.call('publish_graph_draft', draft_id='reverse')
            assert rejected['code'] == 'published_relation_cycle'
            assert tools.workspace.read_published_graph() == previous
            assert tools.workspace.get_draft('reverse').status == 'validated'
            assert tools.call('publish_graph_draft', draft_id='independent')['status'] == 'ok'
            current = tools.workspace.read_published_graph()
            assert len(current['nodes']) == 4 and len(current['edges']) == 2
            assert {p['draft_id'] for p in tools.workspace.read_summary()['publications']} == {'first', 'independent'}
        finally:
            tools.close()


def test_driver_exception_is_value_free_and_preserves_published_graph(tmp_path, capsys, caplog):
    marker = 'DRIVER_BODY_VALUE_CANARY'
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in ('起点', '终点')])
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    with MemorySource([table], {'public.items': [('A', 'B')]}) as adapter:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[adapter])
        try:
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(definition('items')))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id='items')['passed']
            assert tools.call('publish_graph_draft', draft_id='items')['status'] == 'ok'
            before = tools.workspace.read_published_graph()

            def broken(*args, **kwargs):
                raise RuntimeError(marker)
                yield

            adapter.project = broken
            result = tools.call('validate_graph_draft', draft_id='items')
            assert result['status'] == 'ok' and not result['passed']
            assert result['diagnostics'] == [{'code': 'construction_source_execution_failed'}]
            assert tools.workspace.get_draft('items').status == 'needs_revision'
            assert tools.workspace.read_published_graph() == before
            # A transport failure does not refute the previously checked
            # snapshot, which can be published without another source read.
            assert tools.call('publish_graph_draft', draft_id='items')['status'] == 'ok'
            assert tools.workspace.read_published_graph() == before
            assert marker not in str(result)
            assert all(marker.encode() not in path.read_bytes() for path in tools.workspace.root.rglob('*') if path.is_file())
            captured = capsys.readouterr()
            assert marker not in captured.out + captured.err + caplog.text
        finally:
            tools.close()


def test_unexpected_workspace_error_is_not_reflected_by_mcp(tmp_path, monkeypatch):
    tools = ConstructionTools(RunConfig(sources=[]), tmp_path / 'work')
    try:
        def broken():
            raise OSError('SECRET_PATH_AND_VALUE')
        monkeypatch.setattr(tools.workspace, 'read_summary', broken)
        response = tools.call('read_workspace')
        assert response['code'] == 'construction_tool_failed'
        assert 'SECRET_PATH_AND_VALUE' not in str(response)
    finally:
        tools.close()


def test_failed_manifest_commit_leaves_previous_published_graph_readable(tmp_path, monkeypatch):
    from lightgraph.tests.test_graph_yaml_workspace import graph, payload
    store = GraphWorkspace(tmp_path / 'work', 'ctx')
    store.save_draft(payload('first'))
    store.record_validation('first', revision=1, passed=True, graph=graph())
    store.publish('first', revision=1, graph=graph())
    store.save_draft(payload('second'))
    second = graph('n2')
    store.record_validation('second', revision=1, passed=True, graph=second)
    before = store.manifest_path.read_bytes()

    def unavailable(_manifest):
        raise OSError('simulated manifest failure')

    monkeypatch.setattr(store, '_save_manifest', unavailable)
    with pytest.raises(OSError):
        store.publish('second', revision=1, graph=second)
    assert store.manifest_path.read_bytes() == before
    reopened = GraphWorkspace(store.root, 'ctx')
    assert [node['id'] for node in reopened.read_published_graph()['nodes']] == ['n1']
    assert reopened.get_draft('second').status == 'validated'

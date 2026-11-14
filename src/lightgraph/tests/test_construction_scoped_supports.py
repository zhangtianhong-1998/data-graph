"""Keep legacy result scopes without changing source connectors or guessing ACLs."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec, canonical
from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.web.construction_graph import load_graph, graph_details


def fixture():
    columns = ['归属', '编号', '名称', '别名', '属性', '上游编号', '上游名', '关系属性']
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in columns])
    rows = [('A', 'X', 'A名称', 'A别名', 'A属性', 'P', 'A上游', 'A关系值'),
            ('B', 'X', 'B名称', 'B别名', 'B属性', 'Q', 'B上游', 'B关系值')]
    draft = {'draft_id': 'objects', 'entities': [{'type': '对象', 'key_names': ['编号'], 'mappings': [
        {'source_id': 's', 'table': 'items', 'key_columns': ['编号'], 'label_column': '名称',
         'alias_columns': ['别名'], 'properties': {'说明': '属性'}},
        {'source_id': 's', 'table': 'items', 'key_columns': ['上游编号'], 'label_column': '上游名'}]}],
        'relations': [{'type': '依赖', 'source_type': '对象', 'target_type': '对象', 'mappings': [
            {'source_id': 's', 'table': 'items', 'source_key_columns': ['编号'],
             'target_key_columns': ['上游编号'], 'properties': {'说明': '关系属性'}}]}]}
    bindings = [{'source_id': 's', 'table': 'items', 'columns': ['归属']}]
    return table, rows, draft, bindings


def test_same_node_scoped_labels_aliases_properties_and_edges_are_filtered(tmp_path):
    table, rows, draft, bindings = fixture()
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, scopes=['A'],
                       document_scopes=['A'], scope_bindings=bindings, output_dir=str(tmp_path / 'output'))
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        tools = ConstructionTools(config, tmp_path / 'output' / 'construction', adapters=[adapter])
        try:
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            checked = tools.call('validate_graph_draft', draft_id='objects')
            assert checked['passed'], checked
            assert tools.call('publish_graph_draft', draft_id='objects')['nodes'] == 2
            raw = tools.workspace.read_published_graph()
            assert len(raw['nodes']) == 3 and len(raw['edges']) == 2
            a = load_graph(config)
            changed = config.model_copy(deep=True)
            changed.scopes = ['B']
            b = load_graph(changed)
            for scope, result in [('A', a), ('B', b)]:
                assert len(result['nodes']) == 2 and len(result['edges']) == 1
                shared = next(node for node in result['nodes'] if node['identity']['编号'] == 'X')
                assert shared['label'] == scope + '名称'
                assert shared['aliases'] == [scope + '别名']
                assert shared['properties'] == {'说明': [scope + '属性']}
                assert result['edges'][0]['properties'] == {'说明': [scope + '关系值']}
                hidden = 'B' if scope == 'A' else 'A'
                assert all(hidden + suffix not in str(result) for suffix in ['名称', '别名', '属性', '上游', '关系值'])
            shared_a = next(node for node in a['nodes'] if node['identity']['编号'] == 'X')
            shared_b = next(node for node in b['nodes'] if node['identity']['编号'] == 'X')
            assert shared_a['id'] == shared_b['id']
            changed.scopes = ['A', 'B']
            details = graph_details(changed, entity_id=shared_a['id'])
            private_property = next(fact for fact in details['facts'] if fact['payload'].get('value') == 'A属性')
            assert {support['scope_ref'] for support in details['supports']
                    if support['fact_id'] == private_property['id']} == {'A'}
            details_a = graph_details(config, entity_id=shared_a['id'])
            assert {support['scope_ref'] for support in details_a['supports']} == {'A'}
            assert 'B属性' not in str(details_a)
            changed.scopes = ['public']
            assert load_graph(changed)['nodes'] == []
        finally:
            tools.close()


def test_scope_bindings_use_explicit_columns_and_preserve_multi_column_encoding():
    table, rows, draft, bindings = fixture()
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        bindings[0]['columns'] = ['归属', '编号']
        result = execute_draft(draft, [adapter], [table], scope_bindings=bindings)
        assert result['passed']
        scope = canonical({'归属': 'A', '编号': 'X'})
        assert len(filter_graph_scopes(result['graph'], [scope])['nodes']) == 2
        assert not filter_graph_scopes(result['graph'], ['A'])['nodes']


def test_identity_columns_do_not_implicitly_grant_visibility():
    table, rows, draft, _ = fixture()
    for mapping in draft['entities'][0]['mappings']:
        mapping['key_columns'].insert(0, '归属')
    draft['entities'][0]['key_names'].insert(0, '归属')
    draft['relations'] = []
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
    assert result['passed']
    assert not filter_graph_scopes(result['graph'], ['A'])['nodes']
    assert len(filter_graph_scopes(result['graph'], ['public'])['nodes']) == 4


def test_agent_cannot_set_scopes_and_trusted_binding_cannot_read_excluded_columns(tmp_path):
    table, rows, draft, bindings = fixture()
    for field in ('scope_ref', 'scope_bindings', 'namespace_columns'):
        invalid = deepcopy(draft)
        invalid['entities'][0]['mappings'][0][field] = ['归属']
        with pytest.raises(ValidationError):
            GraphDraft.model_validate(invalid)
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, scope_bindings=bindings,
                       goal={'excluded_columns': ['归属']})
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[adapter])
        try:
            tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            result = tools.call('validate_graph_draft', draft_id='objects')
            assert not result['passed']
            assert 'unauthorized_column' in {diagnostic['code'] for diagnostic in result['diagnostics']}
        finally:
            tools.close()


def test_independent_supports_merge_without_leaking_other_scope(tmp_path):
    tables = [TableSpec(source_id='s', name=name, columns=[ColumnSpec(name=c) for c in ('范围', 'code', 'name', 'alias')])
              for name in ('a', 'b')]
    rows = {'public.a': [('A', 'X', 'A名称', 'A别名')], 'public.b': [('B', 'X', 'B名称', 'B别名')]}
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, scopes=['A', 'B'], scope_bindings=[
        {'source_id': 's', 'table': table.name, 'columns': ['范围']} for table in tables])
    with MemorySource(tables, rows) as adapter:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[adapter])
        try:
            for name in ('a', 'b'):
                draft = {'draft_id': name, 'entities': [{'type': '对象', 'key_names': ['code'], 'mappings': [
                    {'source_id': 's', 'table': name, 'key_columns': ['code'], 'label_column': 'name', 'alias_columns': ['alias']}]}]}
                tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
                assert tools.call('validate_graph_draft', draft_id=name)['passed']
                assert tools.call('publish_graph_draft', draft_id=name)['status'] == 'ok'
            graph = tools.workspace.read_published_graph()
            assert len(graph['nodes']) == 1
            assert filter_graph_scopes(graph, ['A'])['nodes'][0]['aliases'] == ['A别名']
            assert filter_graph_scopes(graph, ['B'])['nodes'][0]['aliases'] == ['B别名']
            # Replacing one draft with a complete empty result withdraws only
            # its supports; the independent B publication stays visible.
            record = tools.workspace.get_draft('a')
            empty = deepcopy(graph)
            empty['nodes'] = []
            empty['edges'] = []
            revised = record.draft.model_copy(update={'rationale': '来源A已完整确认没有记录'})
            revision = tools.workspace.save_draft(revised).revision
            tools.workspace.record_validation('a', revision=revision, passed=True, graph=empty)
            tools.workspace.publish('a', revision=revision, graph=empty)
            current = tools.workspace.read_published_graph()
            assert filter_graph_scopes(current, ['A'])['nodes'] == []
            assert filter_graph_scopes(current, ['B'])['nodes'][0]['aliases'] == ['B别名']
        finally:
            tools.close()


def test_empty_new_workspace_does_not_hide_historical_graph_and_corruption_fails_closed(tmp_path):
    from lightgraph.store import GraphStore
    from lightgraph.tests.test_graph_store import node, prop, publish
    from lightgraph.web.execution import read_graph
    config = RunConfig(sources=[], output_dir=str(tmp_path / 'out'), scopes=['A'])
    with GraphStore(tmp_path / 'out' / 'graph.sqlite') as store:
        publish(store, [node('a'), prop('label', 'A可见', 'a')], scope='A')
    work = GraphWorkspace(tmp_path / 'out' / 'construction', 'ctx')
    workspace = SimpleNamespace(resolved_config=lambda _pid: config)
    assert read_graph(workspace, 'project')['nodes'][0]['label'] == 'A可见'
    work.manifest_path.write_text('broken: [')
    with pytest.raises(LightGraphError, match='invalid_graph_yaml'):
        read_graph(workspace, 'project')


def test_visible_node_does_not_retain_names_or_merge_details_from_hidden_scope():
    graph = {'support_visibility_version': 1, 'nodes': [{'id': 'n', 'type': 'item', 'identity': {'id': 1},
        'english_names': ['private-name'], 'descriptions': ['private-description'],
        'supports': [{'scope_ref': 'public', 'label': '公开'},
                     {'scope_ref': 'hidden', 'english_name': 'private-name', 'description': 'private-description'}]}],
        'edges': [], 'conflict_isolation': {'nodes': 1, 'details': [{'label': 'private-conflict'}]},
        'candidate_identity_merges': [{'label': 'private-candidate'}],
        'evidence_catalog': {'private-ref': {'original_ref': 'e18'}}, 'evidence_refs': ['private-ref']}
    view = filter_graph_scopes(graph, ['public'])
    assert view['nodes'][0]['label'] == '公开'
    assert view['conflict_isolation']['nodes'] == 1
    assert 'private-' not in str(view)

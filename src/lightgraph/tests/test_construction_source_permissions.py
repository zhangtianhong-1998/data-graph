"""Per-source fact permissions apply before probes and before draft execution."""
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


@pytest.fixture
def mixed(tmp_path):
    sources = [dict(id=sid, kind='mysql_database', allowed_tables=['public.items'], allow_fact_data=allowed)
               for sid, allowed in [('allowed', True), ('blocked', False)]]
    config = RunConfig(sources=sources, model={'mode': 'in_environment'})
    adapters = []
    for source in sources:
        table = TableSpec(source_id=source['id'], schema='public', name='items',
                          columns=[ColumnSpec(name='code'), ColumnSpec(name='label')])
        adapters.append(MemorySource([table], {'public.items': [('A', '可用对象')]}))
    tools = ConstructionTools(config, tmp_path/'work', adapters=adapters)
    try:
        yield tools, adapters
    finally:
        tools.close()
        for adapter in adapters:
            adapter.close()


def test_catalog_and_metadata_expose_per_source_fact_access(mixed):
    tools, adapters = mixed
    tables = tools.describe_sources()['tables']
    assert {t['source']: t['fact_data_allowed'] for t in tables} == {'allowed': True, 'blocked': False}
    metadata = tools.call('inspect_source', table='blocked:public.items', operation='get_metadata')
    assert metadata['status'] == 'ok'
    assert metadata['data']['tables'][0]['fact_data_allowed'] is False
    assert all(adapter.stats.queries == 0 for adapter in adapters)


@pytest.mark.parametrize('operation,args', [
    ('execute_readonly_sql', {'sql': 'SELECT code FROM public.items'}),
    ('profile_columns', {'columns': ['code']}),
    ('check_fd', {'key_columns': ['code'], 'value_columns': ['label']}),
])
def test_denied_source_rejects_before_query_while_allowed_source_still_works(mixed, operation, args):
    tools, adapters = mixed
    denied = tools.call('inspect_source', table='blocked:public.items', operation=operation, arguments=args)
    assert denied['code'] == 'source_fact_data_forbidden'
    assert all(adapter.stats.queries == 0 for adapter in adapters)
    allowed = tools.call('inspect_source', table='allowed:public.items', operation='execute_readonly_sql',
                         arguments={'sql': 'SELECT code FROM public.items'})
    assert allowed['status'] == 'ok' and allowed['data']['rows'] == [['A']]
    assert adapters[1].stats.queries == 0


@pytest.mark.parametrize('source,target', [('allowed', 'blocked'), ('blocked', 'allowed')])
def test_cross_source_reference_cannot_borrow_enabled_source_permission(mixed, source, target):
    tools, adapters = mixed
    result = tools.call('inspect_source', table='allowed:public.items', operation='check_reference', arguments={
        'source_table': f'{source}:public.items', 'source_columns': ['code'],
        'target_table': f'{target}:public.items', 'target_columns': ['code']})
    assert result['code'] == 'source_fact_data_forbidden'
    assert all(adapter.stats.queries == 0 for adapter in adapters)


def test_draft_validation_cannot_bypass_permission_and_other_source_can_publish(mixed):
    tools, adapters = mixed
    def save(source):
        draft = {'draft_id': source, 'entities': [{'type': 'Item', 'key_names': ['code'],
            'mappings': [{'source_id': source, 'table': 'items', 'key_columns': ['code'], 'label_column': 'label'}]}]}
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
    save('blocked')
    assert tools.call('validate_graph_draft', draft_id='blocked')['code'] == 'source_fact_data_forbidden'
    assert tools.call('publish_graph_draft', draft_id='blocked')['code'] == 'source_fact_data_forbidden'
    assert all(adapter.stats.queries == 0 for adapter in adapters)
    save('allowed')
    assert tools.call('validate_graph_draft', draft_id='allowed')['passed'] is True
    assert tools.call('publish_graph_draft', draft_id='allowed')['status'] == 'ok'
    assert adapters[1].stats.queries == 0

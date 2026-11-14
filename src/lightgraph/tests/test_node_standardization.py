"""Tests for standardized node contract, explicit English/description roles, and scope-safe multi-table details."""
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, write_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.web.construction_graph import graph_details


def test_standard_node_details_separate_names_descriptions_catalog_notes_and_physical_tables(tmp_path):
    t_income = TableSpec(
        source_id='db_a',
        schema='dm',
        name='income_f',
        comment='收入宽表物理注释',
        columns=[
            ColumnSpec(name='prod_code', data_type='varchar'),
            ColumnSpec(name='prod_cn', data_type='varchar'),
            ColumnSpec(name='prod_en', data_type='varchar'),
            ColumnSpec(name='unit_col', data_type='character varying', comment='计量单位物理备注'),
            ColumnSpec(name='tenant', data_type='varchar'),
        ],
    )
    t_order = TableSpec(
        source_id='db_b',
        schema='dm',
        name='order_f',
        comment='订单事实表物理注释',
        columns=[
            ColumnSpec(name='product_cd', data_type='varchar'),
            ColumnSpec(name='prod_desc', data_type='text'),
            ColumnSpec(name='tenant', data_type='varchar'),
        ],
    )
    rows_a = [('P01', '云专线', 'Cloud Dedicated Line', '万元', 'public')]
    rows_b = [('P01', '面向政企提供的高可用云网互联专线', 'public'), ('P01', '内部秘密备注', 'private')]

    config = RunConfig.model_validate({
        'sources': [],
        'model': {'mode': 'in_environment'},
        'scopes': ['public'],
        'scope_bindings': [
            {'source_id': 'db_a', 'schema': 'dm', 'table': 'income_f', 'columns': ['tenant']},
            {'source_id': 'db_b', 'schema': 'dm', 'table': 'order_f', 'columns': ['tenant']},
        ],
        'output_dir': str(tmp_path / 'out'),
    })
    draft = {
        'draft_id': 'products',
        'entities': [{
            'type': 'Product',
            'description': '实体定义默认说明',
            'key_names': ['code'],
            'mappings': [
                {
                    'source_id': 'db_a', 'schema': 'dm', 'table': 'income_f',
                    'key_columns': ['prod_code'], 'label_column': 'prod_cn',
                    'english_name_column': 'prod_en',
                    'properties': {'unit': 'unit_col'},
                },
                {
                    'source_id': 'db_b', 'schema': 'dm', 'table': 'order_f',
                    'key_columns': ['product_cd'],
                    'description_column': 'prod_desc',
                },
            ],
        }],
    }
    GraphDraft.model_validate(draft)

    with MemorySource([t_income], {t_income.qualified_name: rows_a}) as s_a, \
         MemorySource([t_order], {t_order.qualified_name: rows_b}) as s_b:
        tools = ConstructionTools(config, tmp_path / 'out' / 'construction', adapters=[s_a, s_b])
        try:
            meta = tools.describe_sources()
            write_yaml(tmp_path / 'out' / 'physical_catalog.yaml', {'tables': meta['tables']})
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            checked = tools.call('validate_graph_draft', draft_id='products')
            assert checked['passed'], checked
            assert tools.call('publish_graph_draft', draft_id='products')['status'] == 'ok'
            published_graph = tools.workspace.read_published_graph()
        finally:
            tools.close()

    details = graph_details(config, entity_id=published_graph['nodes'][0]['id'])
    std, = details['standard_nodes']
    assert std['chinese_name'] == '云专线'
    assert std['english_name'] == 'Cloud Dedicated Line'
    assert std['english_name_status'] == 'mapped'
    assert std['business_description'] == '面向政企提供的高可用云网互联专线'
    assert std['business_description_origin'] == 'source_column'
    assert '内部秘密备注' not in str(details)

    # Physical catalog comments stay strictly separated from business description.
    assert any(n['table_comment'] == '收入宽表物理注释' for n in std['catalog_metadata_notes'])
    assert all(n['note_kind'] == 'physical_catalog_declaration' for n in std['catalog_metadata_notes'])

    # Dynamic attributes enriched from metadata without internal keys.
    unit_attr = next(a for a in std['attributes'] if a['key'] == 'unit')
    assert unit_attr['values'] == ['万元']
    assert unit_attr['data_type'] == 'character varying'
    assert unit_attr['origin_tables'][0]['column'] == 'unit_col'
    assert all(a['key'] not in {'overview_only', 'label_observed'} for a in std['attributes'])

    # Standardized multi-table source evidences grouped cleanly.
    assert len(std['source_evidences']) == 2
    by_table = {e['qualified_table']: e for e in std['source_evidences']}
    assert {'chinese_name', 'english_name', 'attribute'} <= {
        f['role'] for f in by_table['dm.income_f']['contributed_fields']
    }
    assert any(f['role'] == 'description' for f in by_table['dm.order_f']['contributed_fields'])


def test_legacy_node_without_english_column_reports_unknown_without_guessing_aliases(tmp_path):
    table = TableSpec(source_id='s', name='metrics', columns=[ColumnSpec(name=c) for c in ['code', 'cn', 'en_alias']])
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, output_dir=str(tmp_path / 'out'))
    draft = {
        'draft_id': 'metrics',
        'entities': [{
            'type': 'Metric',
            'key_names': ['code'],
            'mappings': [{'source_id': 's', 'table': 'metrics', 'key_columns': ['code'],
                          'label_column': 'cn', 'alias_columns': ['en_alias']}],
        }],
    }
    with MemorySource([table], {table.qualified_name: [('M1', '营业收入', 'Operating Revenue')]}) as src:
        tools = ConstructionTools(config, tmp_path / 'out' / 'construction', adapters=[src])
        try:
            tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            checked = tools.call('validate_graph_draft', draft_id='metrics')
            assert checked['passed'], checked
            tools.call('publish_graph_draft', draft_id='metrics')
            nid = tools.workspace.read_published_graph()['nodes'][0]['id']
        finally:
            tools.close()

    details = graph_details(config, entity_id=nid)
    std, = details['standard_nodes']
    assert std['chinese_name'] == '营业收入'
    assert std['english_name'] is None
    assert std['english_name_status'] == 'unknown'
    assert std['aliases'] == ['Operating Revenue']


def test_same_table_positions_keep_both_key_columns_and_catalog_notes():
    from lightgraph.construction.node_standardizer import standardize_node_details

    source = {'source_id': 'sheet', 'schema': 'public', 'table': 'levels'}
    supports = [{'label': '收入', 'scope_ref': 'public', 'provenance': {
        'source': source, 'key_columns': [key], 'label_column': name, 'columns': [key, name],
    }} for key, name in [('l1', 'name1'), ('l2', 'name2')]]
    view = standardize_node_details(
        {'id': 'n1', 'type': 'Metric', 'supports': supports, 'properties': {'unit': ['元']},
         'future_control_flag': True, 'english_names': ['hidden aggregate']},
        catalog_tables=[{'source_id': 'sheet', 'name': 'levels', 'columns': [
            {'name': 'name1', 'comment': '名称1'}, {'name': 'name2', 'comment': '名称2'}]}])
    assert view['source_evidences'][0]['key_columns'] == ['l1', 'l2']
    assert {note['column'] for note in view['catalog_metadata_notes'][0]['column_comments']} == {'name1', 'name2'}
    assert [attribute['key'] for attribute in view['attributes']] == ['unit']
    assert view['english_name'] is None

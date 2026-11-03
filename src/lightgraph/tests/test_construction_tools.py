"""Real CSV/Excel domain tools, with no model calls or legacy proposal runtime."""
from copy import deepcopy
import pytest
from openpyxl import Workbook

from lightgraph.construction.models import GraphDraft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource


def config_for(tmp_path, *, excel=False, **updates):
    if excel:
        path = tmp_path / 'input.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.title = '资料'
        sheet.append(['编码', '名称', '归属', '密钥'])
        sheet.append(['001', '设备甲', 'G1', 'hidden'])
        sheet.append(['002', '设备乙', 'G1', 'hidden'])
        book.save(path)
        book.close()
        source = {'id': 'input', 'kind': 'excel_workbook', 'path': str(path)}
    else:
        path = tmp_path / 'input.csv'
        path.write_text('编码,名称,归属,密钥\n001,设备甲,G1,hidden\n002,设备乙,G1,hidden\n', encoding='utf-8')
        source = {'id': 'input', 'kind': 'csv_file', 'path': str(path), 'encoding': 'utf-8',
                  'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text', 'table_name': '资料'}
    return RunConfig.model_validate({'sources': [source], 'model': {'mode': 'in_environment'},
        'goal': {'purpose': '从用户资料构建对象及所属关系'}, **updates})


def draft(table, *, draft_id='设备', kind='设备'):
    return {'draft_id': draft_id, 'entities': [{'type': kind, 'key_names': ['编码'],
            'mappings': [{'source_id': table['source'], 'schema': table['schema'], 'table': table['name'],
                          'key_columns': ['编码'], 'label_column': '名称', 'properties': {'归属': '归属'}}]}]}


@pytest.mark.parametrize('excel', [False, True])
def test_real_source_save_validate_publish_and_independent_drafts(tmp_path, excel):
    config = config_for(tmp_path, excel=excel)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        catalog = tools.call('describe_sources')
        assert catalog['status'] == 'ok'
        table = catalog['tables'][0]
        assert [c['name'] for c in table['columns']] == ['编码', '名称', '归属']
        probe = tools.call('inspect_source', table=table['table'], operation='get_metadata')
        assert probe['status'] == 'ok'
        assert probe['evidence_ref'] == 'e1'
        metadata = probe['data']
        assert all('id' not in c and 'sql_identifier' in c for t in metadata['tables'] for c in t['columns'])
        assert 'proposal_references' not in metadata['identifier_usage']
        assert 'probe_id' not in probe
        specs = tools.call('read_material', name='source_operations')['operations']
        assert 'proposal references' not in str(specs)
        assert 'column.id' not in str(specs)
        raw = draft(table)
        raw['evidence_refs'] = [probe['evidence_ref']]
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(raw))
        assert saved['state'] == 'candidate'
        checked = tools.call('validate_graph_draft', draft_id=raw['draft_id'])
        assert checked['status'] == 'ok' and checked['passed'], checked
        published = tools.call('publish_graph_draft', draft_id=raw['draft_id'])
        assert published['status'] == 'ok' and published['nodes'] == 2, published
        original = tools.workspace.read_published_graph()
        assert {n['identity']['编码'] for n in original['nodes']} == {'001', '002'}
        second = draft(table, draft_id='另一类', kind='另一个对象类型')
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(second))['status'] == 'ok'
        assert tools.call('validate_graph_draft', draft_id=second['draft_id'])['passed']
        assert tools.call('publish_graph_draft', draft_id=second['draft_id'])['nodes'] == 4
        assert {n['id'] for n in original['nodes']} <= {n['id'] for n in tools.workspace.read_published_graph()['nodes']}
        assert not (tmp_path / 'work' / 'graph.sqlite').exists()
        assert not (tmp_path / 'work' / 'graph.yaml').exists()
    finally:
        tools.close()


def test_truncated_probe_evidence_never_claims_full_results(tmp_path):
    config = config_for(tmp_path)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        result = tools.call('inspect_source', table=table['table'], operation='execute_readonly_sql',
            arguments={'sql': 'SELECT "编码", "名称" FROM public."资料"', 'max_rows': 1})
        assert result['status'] == 'ok', result
        assert result['coverage']['truncated']
        evidence = tools.workspace.read_evidence(result['evidence_ref'])
        assert evidence.details['complete'] is False
        assert 'rows' not in evidence.details and 'sql' not in evidence.details['request']
        denied = tools.call('inspect_source', table=table['table'], operation='execute_readonly_sql',
            arguments={'sql': 'SELECT "密钥" FROM public."资料"'})
        assert denied['status'] == 'error'
    finally:
        tools.close()


def test_draft_repair_preserves_other_drafts_and_exact_validated_graph(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        raw = draft(table)
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(raw))['status'] == 'ok'
        assert tools.call('validate_graph_draft', draft_id='设备')['passed']
        bad = deepcopy(raw)
        bad['draft_id'] = '待修订'
        bad['entities'][0]['mappings'][0]['label_column'] = '不存在'
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(bad))['status'] == 'ok'
        result = tools.call('validate_graph_draft', draft_id='待修订')
        assert result['status'] == 'ok' and not result['passed']
        assert tools.workspace.get_draft('设备').status == 'validated'
        assert tools.workspace.get_draft('待修订').status == 'needs_revision'
        checked = tools.call('validate_graph_draft', draft_id='设备')
        path = tools.workspace.root / checked['candidate_graph']
        modified = safe_load_yaml(path.read_text(encoding='utf-8'))
        modified['nodes'][0]['label'] = 'tampered'
        path.write_text(dump_yaml(modified), encoding='utf-8')
        assert tools.call('publish_graph_draft', draft_id='设备')['code'] == 'validated_graph_mismatch'
    finally:
        tools.close()


def test_changed_inputs_and_format_settings_invalidate_saved_checks(tmp_path):
    config = config_for(tmp_path)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        raw = draft(tools.describe_sources()['tables'][0])
        tools.call('save_graph_draft', yaml_text=dump_yaml(raw))
        assert tools.call('validate_graph_draft', draft_id='设备')['passed']
        path = config.sources[0].path
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write('003,设备丙,G2,hidden\n')
        assert tools.call('publish_graph_draft', draft_id='设备')['code'] == 'construction_source_changed'
    finally:
        tools.close()
    refreshed = ConstructionTools(config, tmp_path / 'work')
    try:
        assert refreshed.workspace.get_draft('设备').status == 'stale'
    finally:
        refreshed.close()


def test_material_pages_issue_only_read_sections_and_removed_documents_are_hidden(tmp_path):
    config = config_for(tmp_path)
    doc = tmp_path / 'guide.md'
    doc.write_text('---\nid: guide\npurpose: data_description\n---\n# 定义\n' + '资料说明' * 5000, encoding='utf-8')
    config.documents = [str(doc)]
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        result = tools.call('read_material', name='document:guide')
        assert result['status'] == 'ok' and not result['section_fully_read']
        assert result['evidence_refs'] == []
        while result['next_page'] is not None:
            result = tools.call('read_material', name='document:guide', page=result['next_page'])
        assert result['section_fully_read'] and result['evidence_refs']
        ref = result['evidence_refs'][0]
        assert tools.workspace.read_evidence(ref).kind == 'document_section'
        assert tools.call('read_material', name='reference:../../config.yaml')['status'] == 'error'
        schema = tools.call('read_material', name='draft_schema')['content']
        assert 'RuleSpec' not in schema and 'EntityMapping' in schema
    finally:
        tools.close()
    config.removed_document_ids = ['guide']
    removed = ConstructionTools(config, tmp_path / 'removed')
    try:
        assert removed.describe_sources()['documents'] == []
        assert removed.call('read_material', name='document:guide')['status'] == 'error'
    finally:
        removed.close()


def test_borrowed_adapter_is_not_closed(tmp_path):
    table = TableSpec(source_id='s', name='t', columns=[ColumnSpec(name='key')])
    adapter = MemorySource([table], {'public.t': [('A',)]})
    config = RunConfig.model_validate({'sources': [], 'model': {'mode': 'in_environment'}})
    tools = ConstructionTools(config, tmp_path / 'work', adapters=[adapter])
    tools.close()
    try:
        rows = []
        for batch in adapter.project(table, ['key']):
            with batch:
                rows.extend(batch.rows())
        assert rows == [('A',)]
    finally:
        adapter.close()


def test_mcp_registered_material_arguments_execute_without_dispatch_collision(tmp_path):
    import asyncio
    import json
    from lightgraph.construction.mcp_server import create_server
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        server = create_server(tools)
        registered = asyncio.run(server.list_tools())
        assert {tool.name for tool in registered} == {'describe_sources', 'inspect_source', 'read_material',
            'read_workspace', 'save_graph_draft', 'validate_graph_draft', 'publish_graph_draft'}
        result = asyncio.run(server.call_tool('read_material', {'name': 'draft_schema'}))
        # FastMCP validates and serializes the real wrapper; this exercises the
        # parameter named `name` that previously collided with dispatch itself.
        encoded = str(result)
        assert 'EntityMapping' in encoded and 'invalid_construction_arguments' not in encoded
    finally:
        tools.close()

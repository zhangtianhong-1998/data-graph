"""Tests for cross-project graph inheritance, parallel unit execution, safe GraphMerger, and single-unit retry."""
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.inheritance import freeze_inherited_graphs
from lightgraph.construction.merger import merge_construction_graphs, write_sharded_graph
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import build_unit_config, merge_project_workflow
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig, WorkflowUnitConfig
from lightgraph.sources import MemorySource
from lightgraph.web.app import create_app
from lightgraph.web.construction_graph import graph_details, load_graph
from lightgraph.web.workspace import Workspace


def test_cross_project_inheritance_bypasses_isolation_domain_and_resolves_base_endpoints_without_old_tables(tmp_path):
    ws = Workspace(tmp_path / 'workspace')
    ws.create('BaseProject', project_id='proj_base')
    ws.create('ChildProject', project_id='proj_child')

    # Project A builds a baseline graph over a legacy CSV that Project B does NOT access.
    csv_bytes = 'ind_code,ind_name,scope\nIND_REV,营业收入,public\nIND_SECRET,秘密指标,private\n'.encode('utf-8')
    uploaded = ws.add_file('proj_base', 'base_indicators.csv', csv_bytes)
    cfg_a = ws.get_config('proj_base')
    cfg_a['sources'][0].update(
        id='legacy_csv', encoding='utf-8', delimiter=',', header=True,
        null_values=[''], type_inference='text', table_name='base_ind',
    )
    cfg_a['scopes'] = ['public']
    cfg_a['scope_bindings'] = [{'source_id': 'legacy_csv', 'table': 'base_ind', 'columns': ['scope']}]
    cfg_a['model'] = {'mode': 'in_environment'}
    ws.save_config('proj_base', cfg_a)
    resolved_a = ws.resolved_config('proj_base')
    tools_a = ConstructionTools(resolved_a, Path(resolved_a.output_dir) / 'construction')
    try:
        d_a = {
            'draft_id': 'base_ind',
            'entities': [{'type': 'Indicator', 'key_names': ['code'], 'mappings': [
                {'source_id': 'legacy_csv', 'table': 'base_ind', 'key_columns': ['ind_code'], 'label_column': 'ind_name'}]}],
        }
        assert tools_a.call('save_graph_draft', yaml_text=dump_yaml(d_a))['status'] == 'ok'
        assert tools_a.call('validate_graph_draft', draft_id='base_ind')['passed']
        assert tools_a.call('publish_graph_draft', draft_id='base_ind')['status'] == 'ok'
    finally:
        tools_a.close()

    # Project B has its own isolation_domain='proj_child' and ONLY mounts a new order table.
    t_orders = TableSpec(source_id='new_db', name='orders', columns=[ColumnSpec(name='ord_id'), ColumnSpec(name='ind_name')])
    cfg_b = RunConfig.model_validate({
        'isolation_domain': 'proj_child',
        'sources': [],
        'inherited_graphs': [{'inherit_id': 'base_v1', 'source_project_id': 'proj_base', 'allowed_scopes': ['public']}],
        'model': {'mode': 'in_environment'},
        'output_dir': str(ws.project_path('proj_child') / 'output'),
    })
    freeze_inherited_graphs(cfg_b, Path(cfg_b.output_dir) / 'construction', workspace=ws)

    # Incremental draft references inherited Indicator without redefining Indicator or scanning legacy_csv!
    delta_draft = {
        'draft_id': 'delta_orders',
        'extends_base_graph': True,
        'inherited_entity_types': ['Indicator'],
        'inherited_entity_key_arities': {'Indicator': 1},
        'entities': [{
            'type': 'Order', 'key_names': ['id'],
            'mappings': [{'source_id': 'new_db', 'table': 'orders', 'key_columns': ['ord_id']}],
        }],
        'relations': [{
            'type': 'tracks_indicator',
            'source_type': 'Order',
            'target_type': 'Indicator',
            'mappings': [{
                'source_id': 'new_db', 'table': 'orders',
                'source_key_columns': ['ord_id'],
                'target_key_columns': ['ind_name'],
                'target_lookup': {
                    'source_id': '__base_graph__',
                    'table': 'Indicator',
                    'match_columns': ['label'],
                    'key_columns': ['code'],
                },
            }],
        }],
    }
    with MemorySource([t_orders], {t_orders.qualified_name: [('ORD_100', '营业收入')]}) as src_b:
        tools_b = ConstructionTools(cfg_b, Path(cfg_b.output_dir) / 'construction', adapters=[src_b])
        try:
            assert tools_b.describe_sources()['inherited_graph_summary']['entity_types'] == ['Indicator']
            assert tools_b.call('save_graph_draft', yaml_text=dump_yaml(delta_draft))['status'] == 'ok'
            val = tools_b.call('validate_graph_draft', draft_id='delta_orders')
            assert val['passed'], val
            assert val['statistics']['edges'] == 1
            assert tools_b.call('publish_graph_draft', draft_id='delta_orders')['status'] == 'ok'
            delta_published = tools_b.workspace.read_published_graph()
        finally:
            tools_b.close()

    # Merge base + delta into final sharded graph & verify zero private leak and full provenance!
    merged = merge_construction_graphs([
        {'contributor_id': 'base_v1', 'kind': 'base', 'graph': tools_b.base_graph},
        {'contributor_id': 'unit_orders', 'kind': 'unit', 'graph': delta_published},
    ])
    assert len(merged['nodes']) == 2
    assert len(merged['edges']) == 1
    assert 'IND_SECRET' not in str(merged)
    ind_node = next(n for n in merged['nodes'] if n['type'] == 'Indicator')
    assert ind_node['supports'][0]['inherited_from']['source_project_id'] == 'proj_base'


def test_merger_blocks_unverified_auto_merges_and_isolates_cross_unit_cycles(tmp_path):
    # Two units produce same code for 'Product' WITHOUT shared_entity_types AND same-name metadata columns "年累计".
    col_a = {
        'id': 'n_col_a', 'type': 'MeasureDef', 'identity_origin': 'physical_column',
        'identity': {'source_id': 'db1', 'schema': 'dm', 'table': 't1', 'column': 'ytd_amt'},
        'label': '年累计', 'supports': [{'scope_ref': 'public', 'label': '年累计',
            'provenance': {'evidence_refs': ['e1'], 'source': {'source_id': 'db1', 'schema': 'dm', 'table': 't1'}}}],
    }
    col_b = {
        'id': 'n_col_b', 'type': 'MeasureDef', 'identity_origin': 'physical_column',
        'identity': {'source_id': 'db2', 'schema': 'dm', 'table': 't2', 'column': 'ytd_qty'},
        'label': '年累计', 'supports': [{'scope_ref': 'public', 'label': '年累计',
            'provenance': {'evidence_refs': ['e1'], 'source': {'source_id': 'db2', 'schema': 'dm', 'table': 't2'}}}],
    }
    prod_a = {
        'id': 'n_p01', 'type': 'Product', 'identity': {'code': '01'}, 'label': '政企网关',
        'supports': [{'scope_ref': 'public', 'label': '政企网关',
            'provenance': {'evidence_refs': ['e2'], 'source': {'source_id': 'db1', 'schema': 'dm', 'table': 't1'}}}],
    }
    prod_b = {
        'id': 'n_p01', 'type': 'Product', 'identity': {'code': '01'}, 'label': '消费网关',
        'supports': [{'scope_ref': 'public', 'label': '消费网关',
            'provenance': {'evidence_refs': ['e2'], 'source': {'source_id': 'db2', 'schema': 'dm', 'table': 't2'}}}],
    }
    # Also test cross-unit cycle quarantine for shared Region nodes.
    reg1 = {'id': 'n_r1', 'type': 'Region', 'identity': {'code': 'R1'}, 'label': '大区1',
            'supports': [{'scope_ref': 'public', 'label': '大区1', 'provenance': {'source': {'source_id': 'db1'}}}]}
    reg2 = {'id': 'n_r2', 'type': 'Region', 'identity': {'code': 'R2'}, 'label': '大区2',
            'supports': [{'scope_ref': 'public', 'label': '大区2', 'provenance': {'source': {'source_id': 'db2'}}}]}
    edge_ab = {'id': 'e_12', 'type': 'parent_of', 'source': 'n_r1', 'target': 'n_r2',
               'supports': [{'scope_ref': 'public', 'provenance': {'source': {'source_id': 'db1'}}}]}
    edge_ba = {'id': 'e_21', 'type': 'parent_of', 'source': 'n_r2', 'target': 'n_r1',
               'supports': [{'scope_ref': 'public', 'provenance': {'source': {'source_id': 'db2'}}}]}
    defs = {
        'entities': [{'type': 'Product', 'key_names': ['code']}, {'type': 'Region', 'key_names': ['code']},
                     {'type': 'MeasureDef', 'key_names': ['source_id', 'schema', 'table', 'column'], 'identity_origin': 'physical_column'}],
        'relations': [{'type': 'parent_of', 'source_type': 'Region', 'target_type': 'Region', 'acyclic': True}],
    }

    merged = merge_construction_graphs(
        [
            {'contributor_id': 'u1', 'kind': 'unit', 'graph': {'nodes': [col_a, prod_a, reg1], 'edges': [edge_ab], 'definitions': defs, 'evidence_refs': ['e1', 'e2']}},
            {'contributor_id': 'u2', 'kind': 'unit', 'graph': {'nodes': [col_b, prod_b, reg2], 'edges': [edge_ba], 'definitions': defs, 'evidence_refs': ['e1', 'e2']}},
        ],
        merge_policy={'shared_entity_types': ['Region'], 'conflict_mode': 'quarantine'},
    )
    # Product NOT in shared_entity_types -> kept separate, NOT blindly merged, candidate emitted!
    products = [n for n in merged['nodes'] if n['type'] == 'Product']
    assert len(products) == 2
    assert len(merged['candidate_identity_merges']) == 1
    assert merged['candidate_identity_merges'][0]['entity_type'] == 'Product'

    # Metadata columns NOT auto-connected, candidate alignment emitted!
    assert len(merged['candidate_column_alignments']) == 1
    assert merged['candidate_column_alignments'][0]['normalized_label'] == '年累计'

    # Scoped evidence refs prevent e1 collision!
    assert {'unit:u1:e1', 'unit:u2:e1', 'unit:u1:e2', 'unit:u2:e2'} <= set(merged['evidence_catalog'])

    # Cross-unit cycle quarantined!
    assert len(merged['edges']) == 0
    assert merged['conflict_isolation']['edges'] == 2


def test_parallel_workflow_two_units_and_single_unit_retry_replaces_only_that_unit(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'workspace')
    # Scripted worker runs unit configs directly in subprocess via fake run_construction!
    import lightgraph.web.execution as execution_mod
    real_popen = execution_mod.subprocess.Popen

    def make_worker_script():
        return r'''
import json, sys
from pathlib import Path
from lightgraph.contracts import ColumnSpec, TableSpec, RunReport
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource

result_path = Path(sys.argv[1])
payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))
cfg = RunConfig.model_validate(payload['config'])
out = Path(cfg.output_dir)
unit_name = out.name
is_u1 = (unit_name == 'u1')
tbl = TableSpec(source_id=unit_name, name='t_' + unit_name, columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
retry_marker = out.parents[2] / 'u2_retry_flag'
if not is_u1 and not retry_marker.exists():
    # First attempt for u2 fails cleanly, leaving u1 intact!
    result_path.write_text(json.dumps({'status': 'failed', 'cleanup_status': 'completed', 'error_code': 'simulated_unit_failure'}))
    sys.exit(2)
rows = [('R1', '华北区')] if is_u1 else [('R1', '华北区'), ('R2', '华东区')]
mode = '' if is_u1 else retry_marker.read_text('utf-8')
if mode in {'replacement', 'partial'}:
    rows = [('R3', '西北区')]
with MemorySource([tbl], {tbl.qualified_name: rows}) as src:
    tools = ConstructionTools(cfg, out / 'construction', adapters=[src])
    try:
        draft = {'draft_id': 'reg_' + unit_name, 'entities': [{'type': 'Region', 'key_names': ['code'],
            'mappings': [{'source_id': unit_name, 'table': 't_' + unit_name, 'key_columns': ['code'], 'label_column': 'name'}]}]}
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        assert tools.call('validate_graph_draft', draft_id='reg_' + unit_name)['passed']
        assert tools.call('publish_graph_draft', draft_id='reg_' + unit_name)['status'] == 'ok'
    finally:
        tools.close()
status = 'partial' if mode == 'partial' else 'completed'
rep = RunReport(run_id=result_path.stem, status=status.upper(), rules_published=['reg_' + unit_name],
                counts={'business_nodes': len(rows), 'business_edges': 0})
result_path.write_text(json.dumps({'status': status, 'cleanup_status': 'completed', 'report': rep.model_dump(mode='json')}))
sys.exit(0)
'''

    worker_file = tmp_path / 'fake_wf_worker.py'
    worker_file.write_text(make_worker_script(), encoding='utf-8')
    monkeypatch.setattr(
        execution_mod.subprocess,
        'Popen',
        lambda args, **kw: real_popen([sys.executable, str(worker_file), args[-1]], **kw),
    )

    import sys
    with TestClient(app) as client:
        client.post('/api/projects', json={'name': 'WorkflowProj', 'project_id': 'wf_p'})
        doc = tmp_path / 'notes.md'
        doc.write_text('# 说明\n测试并行工作流。', encoding='utf-8')
        etag = client.get('/api/projects/wf_p/config').headers['etag']
        with doc.open('rb') as f:
            up = client.post('/api/projects/wf_p/files', headers={'If-Match': etag}, files={'file': ('notes.md', f, 'text/markdown')})
            assert up.status_code == 201
            etag = up.headers['etag']
            cfg = client.get('/api/projects/wf_p/config').json()
            cfg['goal_markdown'] = '# 并行工作流测试目标'
            cfg['model'] = {'mode': 'in_environment', 'model': 'offline', 'base_url': 'https://model.invalid/v1'}
        cfg['workflow'] = {
            'enabled': True,
            'max_parallel_workers': 2,
            'units': [
                {'unit_id': 'u1'},
                {'unit_id': 'u2'},
            ],
            'merge_policy': {'shared_entity_types': ['Region']},
        }
        saved = client.put('/api/projects/wf_p/config', headers={'If-Match': etag}, json=cfg)
        assert saved.status_code == 200, saved.text
        etag = saved.headers['etag']

        started = client.post('/api/projects/wf_p/runs', headers={'If-Match': etag}, json={'run_mode': 'continue'})
        assert started.status_code == 202
        rid = started.json()['run_id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            cur = client.get(f'/api/projects/wf_p/runs/{rid}').json()
            if cur['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert cur['status'] == 'partial'
        wf = client.get('/api/projects/wf_p/workflow').json()
        assert wf['state']['units']['u1']['status'] == 'completed'
        assert wf['state']['units']['u2']['status'] == 'failed'
        # Merged graph after partial run already preserves u1's R1 node!
        g1 = client.get('/api/projects/wf_p/graph').json()
        assert [n['identity']['code'] for n in g1['nodes']] == ['R1']

        # Now flip retry flag and retry ONLY u2!
        (app.state.workspace.project_path('wf_p') / 'output' / 'u2_retry_flag').touch()
        retried = client.post('/api/projects/wf_p/workflow/units/u2/retry', headers={'If-Match': etag})
        assert retried.status_code == 202
        rrid = retried.json()['run_id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            rcur = client.get(f'/api/projects/wf_p/runs/{rrid}').json()
            if rcur['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert rcur['status'] == 'completed'
        # Merged sharded graph now cleanly merges u1 + retried u2!
        g2 = client.get('/api/projects/wf_p/graph').json()
        assert {n['identity']['code'] for n in g2['nodes']} == {'R1', 'R2'}
        # Check details reading from sharded storage!
        r1_id = next(n['id'] for n in g2['nodes'] if n['identity']['code'] == 'R1')
        det = client.get(f'/api/projects/wf_p/graph/details?entity_id={r1_id}').json()
        assert len(det['standard_nodes'][0]['source_evidences']) == 2

        flag = app.state.workspace.project_path('wf_p') / 'output' / 'u2_retry_flag'

        def retry_status():
            response = client.post('/api/projects/wf_p/workflow/units/u2/retry', headers={'If-Match': etag})
            assert response.status_code == 202
            until = time.monotonic() + 20
            while time.monotonic() < until:
                result = client.get(f'/api/projects/wf_p/runs/{response.json()["run_id"]}').json()
                if result['status'] not in {'queued', 'running'}:
                    return result['status']
                time.sleep(.05)
            raise AssertionError("retry did not finish")

        # A failed attempt must leave the last committed contribution visible.
        flag.unlink()
        assert retry_status() == 'partial'
        assert {n['identity']['code'] for n in client.get('/api/projects/wf_p/graph').json()['nodes']} == {'R1', 'R2'}
        flag.write_text('partial', encoding='utf-8')
        assert retry_status() == 'partial'
        assert {n['identity']['code'] for n in client.get('/api/projects/wf_p/graph').json()['nodes']} == {'R1', 'R2'}
        # Success replaces the old u2 membership AND its support on the shared R1.
        flag.write_text('replacement', encoding='utf-8')
        assert retry_status() == 'completed'
        assert {n['identity']['code'] for n in client.get('/api/projects/wf_p/graph').json()['nodes']} == {'R1', 'R3'}
        det = client.get(f'/api/projects/wf_p/graph/details?entity_id={r1_id}').json()
        assert len(det['standard_nodes'][0]['source_evidences']) == 1


def test_six_parallel_units_inheriting_base_graph_merge_and_cli_retrieve(tmp_path, monkeypatch):
    """End-to-end check: Project A base -> Project B inherits + 6 parallel units -> merge -> CLI retrieval."""
    from lightgraph import cli
    app = create_app(tmp_path / 'workspace')
    ws = app.state.workspace
    ws.create('Base', project_id='base_p')
    csv_bytes = 'ind_code,ind_name\nIND_1,营业收入\nIND_2,净利润\n'.encode('utf-8')
    ws.add_file('base_p', 'base.csv', csv_bytes)
    cfg_a = ws.get_config('base_p')
    cfg_a['sources'][0].update(
        id='base_csv', encoding='utf-8', delimiter=',', header=True,
        null_values=[''], type_inference='text', table_name='base_ind',
    )
    cfg_a['model'] = {'mode': 'in_environment'}
    ws.save_config('base_p', cfg_a)
    res_a = ws.resolved_config('base_p')
    tools_a = ConstructionTools(res_a, Path(res_a.output_dir) / 'construction')
    try:
        d_a = {'draft_id': 'base', 'entities': [{'type': 'Indicator', 'key_names': ['code'], 'mappings': [
            {'source_id': 'base_csv', 'table': 'base_ind', 'key_columns': ['ind_code'], 'label_column': 'ind_name'}]}]}
        tools_a.call('save_graph_draft', yaml_text=dump_yaml(d_a))
        assert tools_a.call('validate_graph_draft', draft_id='base')['passed']
        assert tools_a.call('publish_graph_draft', draft_id='base')['status'] == 'ok'
    finally:
        tools_a.close()

    import lightgraph.web.execution as execution_mod
    import sys
    real_popen = execution_mod.subprocess.Popen
    worker_script = r'''
import json, sys
from pathlib import Path
from lightgraph.contracts import ColumnSpec, TableSpec, RunReport
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource

result_path = Path(sys.argv[1])
payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))
cfg = RunConfig.model_validate(payload['config'])
out = Path(cfg.output_dir)
uid = out.name
tbl = TableSpec(source_id=uid, name='tbl_' + uid, columns=[ColumnSpec(name='dim_code'), ColumnSpec(name='dim_name'), ColumnSpec(name='ind_code')])
rows = [(f'D_{uid}', f'维度_{uid}', 'IND_1')]
with MemorySource([tbl], {tbl.qualified_name: rows}) as src:
    tools = ConstructionTools(cfg, out / 'construction', adapters=[src])
    try:
        draft = {
            'draft_id': 'ext_' + uid,
            'extends_base_graph': True,
            'inherited_entity_types': ['Indicator'],
            'inherited_entity_key_arities': {'Indicator': 1},
            'entities': [{'type': 'Dimension', 'key_names': ['code'], 'mappings': [
                {'source_id': uid, 'table': 'tbl_' + uid, 'key_columns': ['dim_code'], 'label_column': 'dim_name'}]}],
            'relations': [{'type': 'uses_dim', 'source_type': 'Indicator', 'target_type': 'Dimension', 'mappings': [
                {'source_id': uid, 'table': 'tbl_' + uid, 'source_key_columns': ['ind_code'], 'target_key_columns': ['dim_code']}]}],
        }
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        assert tools.call('validate_graph_draft', draft_id='ext_' + uid)['passed']
        assert tools.call('publish_graph_draft', draft_id='ext_' + uid)['status'] == 'ok'
    finally:
        tools.close()
rep = RunReport(run_id=result_path.stem, status='COMPLETED', rules_published=['ext_' + uid])
result_path.write_text(json.dumps({'status': 'completed', 'cleanup_status': 'completed', 'report': rep.model_dump(mode='json')}))
sys.exit(0)
'''
    worker_file = tmp_path / 'six_unit_worker.py'
    worker_file.write_text(worker_script, encoding='utf-8')
    monkeypatch.setattr(
        execution_mod.subprocess,
        'Popen',
        lambda args, **kw: real_popen([sys.executable, str(worker_file), args[-1]], **kw),
    )

    with TestClient(app) as client:
        client.post('/api/projects', json={'name': 'SixUnits', 'project_id': 'six_p'})
        doc = tmp_path / 'doc.md'
        doc.write_text('# 六并行扩展目标', encoding='utf-8')
        etag = client.get('/api/projects/six_p/config').headers['etag']
        with doc.open('rb') as f:
            up = client.post('/api/projects/six_p/files', headers={'If-Match': etag}, files={'file': ('doc.md', f, 'text/markdown')})
            etag = up.headers['etag']
        cfg = client.get('/api/projects/six_p/config').json()
        cfg['goal_markdown'] = '# 继承基础图谱并开展6并行扩展'
        cfg['model'] = {'mode': 'in_environment', 'model': 'offline', 'base_url': 'https://model.invalid/v1'}
        cfg['inherited_graphs'] = [{'inherit_id': 'base_v1', 'source_project_id': 'base_p', 'allowed_scopes': ['public']}]
        cfg['workflow'] = {
            'enabled': True,
            'max_parallel_workers': 6,
            'units': [{'unit_id': f'u{i}'} for i in range(1, 7)],
            'merge_policy': {'shared_entity_types': ['Indicator', 'Dimension']},
        }
        saved = client.put('/api/projects/six_p/config', headers={'If-Match': etag}, json=cfg)
        assert saved.status_code == 200, saved.text
        etag = saved.headers['etag']
        started = client.post('/api/projects/six_p/runs', headers={'If-Match': etag})
        assert started.status_code == 202
        rid = started.json()['run_id']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            cur = client.get(f'/api/projects/six_p/runs/{rid}').json()
            if cur['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert cur['status'] == 'completed', cur

        graph = client.get('/api/projects/six_p/graph').json()
        # 2 inherited Indicators + 6 new Dimensions = 8 nodes, and 6 edges linking IND_1 to all 6 Dimensions!
        assert len(graph['nodes']) == 8
        assert len(graph['edges']) == 6

        # Sync and query via retrieval endpoint: "维度_u1 和 维度_u6" should discover IND_1 as common bridge node!
        assert client.post('/api/projects/six_p/retrieval/sync').status_code == 200
        ret = client.post('/api/projects/six_p/retrieval/query', json={'query': '维度_u1 和 维度_u6', 'hops': 1, 'top_p_seeds': 1}).json()
        assert any(b['display_name'] == '营业收入' and b['bridge_role'] == 'intermediate_bridge' for b in ret['bridge_nodes'])


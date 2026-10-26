"""HTTP-to-engine checks, project isolation and safe error boundaries."""
import json
import time

import pytest
import yaml
from lightgraph.tests.web_client import RevisionClient as TestClient

from lightgraph.contracts import RuleSpec
from lightgraph.web.app import MAX_REQUEST_BYTES, create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'no-dist')
    with TestClient(app) as result:
        yield result


def create(client, pid='alpha'):
    response = client.post('/api/projects', json={'name': '经营分析', 'project_id': pid})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, name, content, pid='alpha'):
    response = client.post(f'/api/projects/{pid}/files', files={'file': (name, content)})
    assert response.status_code == 201, response.text
    return response.json()


def test_projects_persist_and_uploads_are_project_scoped(client):
    create(client)
    create(client, 'beta')
    file = upload(client, '说明.md', '# 指标说明\n指标和事实值分开。'.encode())
    assert file['scope'] == {'source_ids': [], 'tables': [], 'columns': []}
    assert client.get(f"/api/projects/alpha/files/{file['file_id']}/content").json()['content'].startswith('# 指标')
    assert client.get(f"/api/projects/beta/files/{file['file_id']}/content").status_code == 404
    assert client.app.state.workspace.file_path('alpha', file['file_id']).is_file()
    from lightgraph.web.workspace import Workspace
    persisted = Workspace(client.app.state.workspace.root)
    assert len(persisted.get('alpha')['files']) == 1
    assert not persisted.get('beta')['files']


def test_binding_changes_real_document_and_config_roundtrip(client):
    create(client)
    file = upload(client, '说明.md', '# 经营指标\n排除业务观测值。'.encode())
    binding = {'purpose': 'data_description', 'scope': {'tables': ['input.public.metrics']}}
    response = client.patch(f"/api/projects/alpha/files/{file['file_id']}/binding", json=binding)
    assert response.status_code == 200
    from lightgraph.documents import load_document
    document = load_document(client.app.state.workspace.file_path('alpha', file['file_id']))
    assert document.scope.tables == ['input.public.metrics']
    config = client.get('/api/projects/alpha/config').json()
    config['goal']['purpose'] = '只构建指标定义'
    config['goal']['exclude'] = ['逐笔金额']
    config['isolation_domain'] = 'beta'
    config['output_dir'] = '../beta/output'
    result = client.put('/api/projects/alpha/config', json=config)
    assert result.status_code == 200, result.text
    assert result.json()['isolation_domain'] == 'alpha'
    assert result.json()['output_dir'] == 'output'
    exported = client.get('/api/projects/alpha/config/export')
    parsed = yaml.safe_load(exported.text)
    assert parsed['goal']['purpose'] == '只构建指标定义'
    assert parsed['documents'] == [file['relative_path']]


def test_upload_validation_does_not_create_orphan_files(client):
    create(client)
    for name, content in [('unsupported.exe', b'x'), ('../escape.md', b'x'),
                          ('bad.yaml', b'a: &ref x\nb: *ref'),
                          ('key.yaml', b'llm:\n  api_key: SECRET_UPLOAD_TOKEN')]:
        response = client.post('/api/projects/alpha/files', files={'file': (name, content)})
        assert response.status_code == 400
        assert 'SECRET_UPLOAD_TOKEN' not in response.text
    assert not client.get('/api/projects/alpha').json()['files']


def test_preview_revalidates_credentials_edited_on_disk(client):
    create(client)
    file = upload(client, 'agent.yaml', b'framework: agentscope\nllm: {mode: disabled}')
    path = client.app.state.workspace.file_path('alpha', file['file_id'])
    path.write_text('llm:\n  api_key: SECRET_DISK_TOKEN\n')
    response = client.get(f"/api/projects/alpha/files/{file['file_id']}/content")
    assert response.status_code == 400
    assert 'SECRET_DISK_TOKEN' not in response.text


def test_safe_http_errors_and_origin_boundary(client, monkeypatch):
    create(client)
    assert client.post('/api/projects', json={'name': 'x'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/does-not-exist').status_code == 404
    assert client.get('/').status_code == 503  # No fake UI if dist is absent.
    invalid = client.post('/api/projects', json={'name': '', 'secret': 'SECRET_REQUEST_TOKEN'})
    assert invalid.status_code == 422 and 'SECRET_REQUEST_TOKEN' not in invalid.text
    large = client.put('/api/projects/alpha/config', content=b'{}', headers={'Content-Type': 'application/json', 'Content-Length': str(MAX_REQUEST_BYTES + 1)})
    assert large.status_code == 413
    def fail(*_):
        raise RuntimeError('RAW_QUERY_ROW_PRIVATE')
    monkeypatch.setattr(client.app.state.workspace, 'get_config', fail)
    response = client.get('/api/projects/alpha/config')
    assert response.status_code == 500
    assert response.json() == {'error': {'code': 'internal_error'}}


def test_active_run_blocks_all_input_mutations(client, monkeypatch):
    create(client)
    config = client.get('/api/projects/alpha/config').json()
    file = upload(client, 'doc.md', b'# Context')
    monkeypatch.setattr(client.app.state.manager, 'active', lambda pid: pid == 'alpha')
    for response in [
        client.patch('/api/projects/alpha', json={'name': 'changed'}),
        client.put('/api/projects/alpha/config', json=config),
        client.post('/api/projects/alpha/files', files={'file': ('new.csv', b'a\nx')}),
        client.patch(f"/api/projects/alpha/files/{file['file_id']}/binding", json={'purpose': 'mixed', 'scope': {}}),
        client.post('/api/projects/alpha/metadata/refresh'),
        client.post('/api/projects/alpha/runs'),
    ]:
        assert response.status_code == 409, response.text
    assert client.get('/api/projects/alpha').json()['name'] == '经营分析'


def test_actual_api_run_publishes_graph_and_reuses_project_output(client):
    create(client)
    create(client, 'beta')
    data = upload(client, 'metrics.csv', 'metric_code,metric_name,amount\nK1,收入,PRIVATE_OBSERVATION_CANARY\nK1,收入,300\nK2,费用,10\n'.encode())
    config = client.get('/api/projects/alpha/config').json()
    config['goal']['purpose'] = '构建指标定义，不保留业务观测值'
    config['sources'][0]['table_name'] = 'metrics'
    sid = config['sources'][0]['id']
    rule = RuleSpec(rule_id='metrics', kind='column_group_entity',
        source={'source_id': sid, 'table': 'metrics'},
        identity={'key_columns': ['metric_code'], 'entity_kind': 'Metric'},
        attributes={'single_valued': {'label': 'metric_name'},
                    'excluded': [{'column': 'amount', 'reason': 'observation_value'}]},
        evidence={'semantic_basis': ['fixture:E:approved_definition']})
    rules = upload(client, 'rules.yaml', yaml.safe_dump([rule.model_dump(mode='json', by_alias=True)]).encode())
    config['rules'] = [rules['relative_path']]
    assert client.put('/api/projects/alpha/config', json=config).status_code == 200
    metadata = client.post('/api/projects/alpha/metadata/refresh')
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()['tables'][0]['name'] == 'metrics'
    assert any(e['relation'] == 'table_has_column' for e in metadata.json()['edges'])
    assert 'PRIVATE_OBSERVATION_CANARY' not in metadata.text
    ids = None
    for _ in range(2):
        response = client.post('/api/projects/alpha/runs')
        assert response.status_code == 202, response.text
        rid = response.json()['run_id']
        until = time.monotonic() + 15
        while time.monotonic() < until:
            run = client.get(f'/api/projects/alpha/runs/{rid}').json()
            if run['status'] not in {'queued', 'running'}:
                break
            time.sleep(.03)
        assert run['status'] == 'completed', run
        assert run['cleanup_status'] == 'completed'
        graph = client.get('/api/projects/alpha/graph').json()
        assert {n['label'] for n in graph['nodes']} == {'收入', '费用'}
        current = {n['id'] for n in graph['nodes']}
        assert ids is None or ids == current
        ids = current
    assert not client.get('/api/projects/beta/graph').json()['nodes']
    assert len(client.get('/api/projects/alpha/runs').json()['runs']) == 2
    project = client.app.state.workspace.project_path('alpha')
    # The explicitly uploaded CSV remains an asset. Generated outputs must not
    # retain excluded observations, samples, tool messages or raw worker logs.
    assert b'PRIVATE_OBSERVATION_CANARY' in (project / data['relative_path']).read_bytes()
    for directory in ('output', 'runs'):
        for file in (project / directory).rglob('*'):
            if file.is_file():
                assert b'PRIVATE_OBSERVATION_CANARY' not in file.read_bytes()


def test_static_frontend_and_api_share_service(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<div id="app">build-marker</div>')
    with TestClient(create_app(tmp_path / 'workspace', frontend_dir=dist)) as client:
        assert 'build-marker' in client.get('/').text
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/api/unknown').status_code == 404

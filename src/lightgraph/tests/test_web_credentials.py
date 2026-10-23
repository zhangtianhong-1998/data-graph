"""Browser-only credentials, project boundaries, and declaration-only onboarding."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources import MemorySource
from lightgraph.web.app import create_app
from lightgraph.web.credentials import SessionCredentials
from lightgraph.web.execution import _resolved_payload, read_metadata


@pytest.fixture
def client(tmp_path):
    root = tmp_path / 'workspace'
    audit_secrets = []
    try:
        with TestClient(create_app(root, frontend_dir=tmp_path / 'absent')) as client:
            client._audit_secrets = audit_secrets
            for pid in ('alpha', 'beta'):
                assert client.post('/api/projects', json={'name':pid, 'project_id':pid}).status_code == 201
            yield client
    finally:
        # Windows denies reads of the live service lease. Audit after lifespan
        # shutdown, including the lock and anything written during cleanup.
        _scan_workspace(root, audit_secrets)


def session(client, pid='alpha'):
    result=client.post(f'/api/projects/{pid}/session')
    assert result.status_code == 201
    assert result.headers['Cache-Control'] == 'no-store'
    return {'X-Workbench-Session':result.json()['session_id']}


def set_model(client):
    config=client.get('/api/projects/alpha/config').json()
    config['model'].update(base_url='https://model.example/v1',model='test-model',mode='metadata_only')
    assert client.put('/api/projects/alpha/config',json=config).status_code==200
    return config


def scan_no_secrets(client, *secrets):
    client._audit_secrets.extend(secrets)


def _scan_workspace(root, secrets):
    for path in root.rglob('*'):
        if path.is_file():
            content = path.read_bytes()
            assert all(secret.encode() not in content for secret in secrets), f'credential persisted in {path}'


def test_secret_audit_includes_released_service_lock(tmp_path):
    from lightgraph.runtime.locking import FileLock

    path = tmp_path / '.service.lock'
    secret = 'SECRET_LOCK_AUDIT'
    path.write_text(secret, encoding='utf-8')
    lock = FileLock(path).acquire()
    lock.close()
    with pytest.raises(AssertionError, match='credential persisted'):
        _scan_workspace(tmp_path, [secret])


def test_secret_audit_does_not_ignore_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('configuration', encoding='utf-8')

    def denied(_path):
        raise PermissionError('audit read denied')

    monkeypatch.setattr(type(path), 'read_bytes', denied)
    with pytest.raises(PermissionError, match='audit read denied'):
        _scan_workspace(tmp_path, ['SECRET_AUDIT'])


def test_model_key_is_project_session_only_and_binds_endpoint(client, monkeypatch):
    config=set_model(client)
    headers=session(client)
    other=session(client)
    key='SECRET_BROWSER_MODEL_239'
    result=client.put('/api/projects/alpha/credentials/model',headers=headers,json={'api_key':key})
    assert result.json()=={'model':True,'database_sources':[]}
    assert key not in result.text
    assert client.get('/api/projects/alpha/credentials').json()['model'] is False
    assert client.get('/api/projects/alpha/credentials',headers=other).json()['model'] is False
    assert client.get('/api/projects/beta/credentials',headers=headers).status_code==400
    sent=[]
    monkeypatch.setattr(client.app.state.manager,'start',lambda pid,**kwargs: sent.append(kwargs) or {'run_id':'fake'})
    assert client.post('/api/projects/alpha/runs',headers=headers).status_code==202
    assert sent[-1]=={'model_api_key':key}
    config['model']['base_url']='https://changed.example/v1'
    assert client.put('/api/projects/alpha/config',json=config).status_code==200
    assert not client.get('/api/projects/alpha/credentials',headers=headers).json()['model']
    assert client.post('/api/projects/alpha/runs',headers=headers).status_code==202
    assert sent[-1]=={}
    scan_no_secrets(client,key,headers['X-Workbench-Session'])


def test_model_key_requires_saved_endpoint_and_valid_session(client):
    headers=session(client)
    payload={'api_key':'SECRET_UNBOUND_MODEL'}
    assert client.put('/api/projects/alpha/credentials/model',headers=headers,json=payload).status_code==400
    set_model(client)
    assert client.put('/api/projects/alpha/credentials/model',json=payload).status_code==400
    assert client.put('/api/projects/alpha/credentials/model',headers=headers,json=payload).status_code==200
    assert client.delete('/api/projects/alpha/credentials/model',headers=headers).status_code==200
    assert not client.get('/api/projects/alpha/credentials',headers=headers).json()['model']
    scan_no_secrets(client,payload['api_key'])


def test_sessions_expire_are_bounded_and_shutdown_clears(client):
    now=[10.0]
    vault=SessionCredentials(ttl_seconds=2,max_sessions=1,clock=lambda:now[0])
    token=vault.create('alpha')
    with pytest.raises(LightGraphError,match='capacity'):
        vault.create('alpha')
    now[0]=13
    with pytest.raises(LightGraphError,match='invalid'):
        vault.require('alpha',token)
    vault.create('beta')
    vault.clear()
    assert not vault._sessions


def db_body(source_id='warehouse'):
    return {'source_id':source_id,'allowed_tables':['metrics'],
            'connection':{'kind':'mysql_database','host':'localhost','database':'business',
                          'username':'reader','password':'SECRET_DB_BROWSER_382'}}


def fake_open(calls):
    def opened(config, *, metadata_only=False, source_credentials=None):
        calls.append((config,metadata_only,dict(source_credentials or {})))
        result=[]
        for source in config.sources:
            tables=[TableSpec(source_id=source.id,schema=name.split('.')[0],name=name.split('.')[1],
                              columns=[ColumnSpec(name='metric_code'),ColumnSpec(name='metric_name')])
                    for name in source.allowed_tables]
            adapter=MemorySource(tables,{})
            adapter.execute=lambda *_a,**_kw:pytest.fail('onboarding cannot read instance data')
            result.append(adapter)
        return result
    return opened


def test_connect_uses_only_selected_source_and_keeps_credentials_out_of_files(client,monkeypatch):
    headers=session(client)
    calls=[]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open(calls))
    config=client.get('/api/projects/alpha/config').json()
    config['sources']=[{'id':'unavailable','kind':'mysql_database','dsn_env':'UNSET_DB_ENV','allowed_tables':['other.table']}]
    client.put('/api/projects/alpha/config',json=config)
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body())
    assert result.status_code==200,result.text
    data=result.json()
    assert data['metadata']['coverage']=='declarations_partial'
    assert data['metadata']['missing_sources']==['unavailable']
    assert data['metadata']['instance_queries']==0
    assert len(data['metadata']['files'])==3
    assert calls[-1][1] is True
    assert [s.id for s in calls[-1][0].sources]==['warehouse']
    assert 'SECRET_DB_BROWSER_382' in calls[-1][2]['warehouse']
    saved=next(s for s in data['config']['sources'] if s['id']=='warehouse')
    assert saved['credential_mode']=='session' and saved['credential_revision']
    assert saved['allowed_tables']==['business.metrics']
    assert saved['dsn_env'] is None
    assert data['credentials']['database_sources']==['warehouse']
    assert client.get('/api/projects/alpha/metadata').json()['requires_refresh']
    assert 'SECRET_DB_BROWSER_382' not in result.text
    scan_no_secrets(client,'SECRET_DB_BROWSER_382',headers['X-Workbench-Session'])


@pytest.mark.parametrize('sslmode', [None, 'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'])
def test_jdbc_connect_normalizes_session_credential_and_reuses_it_for_run(client, monkeypatch, sslmode):
    from psycopg.conninfo import conninfo_to_dict
    headers = session(client)
    calls = []
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', fake_open(calls))
    password = "SECRET_JDBC_BROWSER:/@? #中文'&+%"
    jdbc = 'jdbc:gaussdb://db.example.com:8000/business'
    result = client.post('/api/projects/alpha/database/connect', headers=headers, json={
        'source_id': 'warehouse', 'allowed_tables': ['metrics'],
        'connection': {'kind': 'gauss_database', 'dsn': jdbc, 'username': 'reader', 'password': password, 'sslmode': sslmode},
    })
    assert result.status_code == 200, result.text
    normalized = calls[-1][2]['warehouse']
    options = conninfo_to_dict(normalized)
    assert options['host'] == 'db.example.com' and options['port'] == '8000'
    assert options['dbname'] == 'business' and options['user'] == 'reader' and options['password'] == password
    assert options['sslmode'] == (sslmode or 'verify-full')
    assert password not in result.text and jdbc not in result.text
    assert result.json()['config']['sources'][0]['allowed_tables'] == ['public.metrics']
    sent = []
    monkeypatch.setattr(client.app.state.manager, 'start', lambda pid, **kwargs: sent.append(kwargs) or {'run_id': 'fake'})
    assert client.post('/api/projects/alpha/runs', headers=headers).status_code == 202
    assert sent[-1]['source_credentials']['warehouse'] == normalized
    scan_no_secrets(client, password, jdbc, headers['X-Workbench-Session'])


def test_connect_failure_rolls_back_config_and_secret(client,monkeypatch):
    headers=session(client)
    before=client.get('/api/projects/alpha/config').json()
    def fail(*_a,**_kw):
        raise RuntimeError('SECRET_DB_BROWSER_382 driver diagnostic')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fail)
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body())
    assert result.status_code==500 and 'SECRET_DB_BROWSER_382' not in result.text
    assert client.get('/api/projects/alpha/config').json()==before
    assert client.get('/api/projects/alpha/credentials',headers=headers).json()['database_sources']==[]
    scan_no_secrets(client,'SECRET_DB_BROWSER_382')


def test_failure_after_catalog_write_cannot_relabel_new_metadata_as_old_authorization(client,monkeypatch):
    headers=session(client)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open([]))
    request=db_body()
    assert client.post('/api/projects/alpha/database/connect',headers=headers,json=request).status_code==200
    before=client.get('/api/projects/alpha/config').json()
    def fail(*_a,**_kw):
        raise RuntimeError('render failure after catalog write')
    monkeypatch.setattr('lightgraph.metadata.publish_metadata',fail)
    request['allowed_tables']=['new_table']
    assert client.post('/api/projects/alpha/database/connect',headers=headers,json=request).status_code==500
    assert client.get('/api/projects/alpha/config').json()==before
    metadata=client.get('/api/projects/alpha/metadata').json()
    assert metadata['requires_refresh'] and metadata['tables']==[]


def test_partial_catalog_defers_documents_for_unread_source(client,monkeypatch):
    headers=session(client)
    calls=[]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open(calls))
    upload=client.post('/api/projects/alpha/files',files={'file':('bound.md',b'# Descriptions')}).json()
    binding={'purpose':'data_description','scope':{'tables':['unavailable.other.table']}}
    assert client.patch(f"/api/projects/alpha/files/{upload['file_id']}/binding",json=binding).status_code==200
    config=client.get('/api/projects/alpha/config').json()
    config['sources']=[{'id':'unavailable','kind':'mysql_database','dsn_env':'UNSET_DB_ENV','allowed_tables':['other.table']}]
    client.put('/api/projects/alpha/config',json=config)
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body())
    assert result.status_code==200,result.text
    assert result.json()['metadata']['document_context_deferred'] is True
    # Full refresh still checks document scopes rather than silently accepting
    # a table that is absent from the completed declared catalog.
    config=result.json()['config']
    config['sources']=[s for s in config['sources'] if s['id']=='warehouse']
    client.put('/api/projects/alpha/config',json=config)
    assert client.post('/api/projects/alpha/metadata/refresh',headers=headers).status_code==400


def test_reconnecting_preserves_column_authorization_and_capability_file(client,monkeypatch):
    headers=session(client)
    calls=[]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open(calls))
    config=client.get('/api/projects/alpha/config').json()
    manifest=client.post('/api/projects/alpha/files',files={'file':('capabilities.yaml',b'warehouse: {}')}).json()
    config['sources']=[{'id':'warehouse','kind':'mysql_database','dsn_env':'OLD_ENV',
        'allowed_tables':['business.metrics'],'allowed_columns':{'business.metrics':['metric_code']},
        'capability_manifest':manifest['relative_path']}]
    client.put('/api/projects/alpha/config',json=config)
    request=db_body()
    request['deployment_retention_verified']=True
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=request)
    assert result.status_code==200,result.text
    config=result.json()['config']
    assert config['deployment_retention_verified']
    source=config['sources'][0]
    assert source['allowed_columns']=={'business.metrics':['metric_code']}
    assert source['capability_manifest']==manifest['relative_path']
    request['allowed_tables']=['different']
    rejected=client.post('/api/projects/alpha/database/connect',headers=headers,json=request)
    assert rejected.json()['error']['code']=='source_column_scope_conflict'
    assert client.get('/api/projects/alpha/config').json()==config


def test_table_list_upload_does_not_become_business_source_or_project_asset(client):
    from io import BytesIO
    from openpyxl import Workbook
    book=Workbook()
    book.active.append(['schema','table_name'])
    book.active.append(['business','metrics'])
    stream=BytesIO()
    book.save(stream)
    book.close()
    before=client.get('/api/projects/alpha/config').json()
    result=client.post('/api/projects/alpha/database/table-list',files={'file':('tables.xlsx',stream.getvalue())})
    assert result.status_code==200,result.text
    assert result.json()['tables']==['business.metrics']
    assert not client.get('/api/projects/alpha').json()['files']
    assert client.get('/api/projects/alpha/config').json()==before


def test_selected_refresh_reuses_only_unchanged_other_declarations(client,monkeypatch):
    headers=session(client)
    calls=[]
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open(calls))
    assert client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body('one')).status_code==200
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body('two')).json()
    assert {t['source_id'] for t in result['metadata']['tables']}=={'one','two'}
    assert not result['metadata']['requires_refresh']
    assert [s.id for s in calls[-1][0].sources]==['two']
    config=result['config']
    config['sources'][0]['allowed_tables']=['business.different']
    client.put('/api/projects/alpha/config',json=config)
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body('two')).json()
    assert {t['source_id'] for t in result['metadata']['tables']}=={'two'}
    assert result['metadata']['missing_sources']==['one']


def test_source_credentials_expired_session_cannot_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv('UNSAFE_OLD_ENV','mysql://reader:OLD_SECRET@localhost/business')
    config=RunConfig(sources=[{'id':'db','kind':'mysql_database','credential_mode':'session',
        'credential_revision':'revision','dsn_env':'UNSAFE_OLD_ENV','allowed_tables':['business.metrics']}])
    with pytest.raises(LightGraphError,match='source_credential_missing'):
        open_sources(config,metadata_only=True)


def test_direct_source_credentials_reach_adapter_without_mutating_environment(monkeypatch):
    monkeypatch.setenv('EXISTING_DB_ENV','mysql://reader:OLD_ENV_SECRET@localhost/business')
    config=RunConfig(sources=[{'id':'db','kind':'mysql_database','credential_mode':'session',
        'credential_revision':'revision','dsn_env':'EXISTING_DB_ENV','allowed_tables':['business.metrics'],
        'allowed_columns':{'business.metrics':['metric_code']}}])
    calls=[]
    adapter=object()
    monkeypatch.setattr('lightgraph.sources.MySQLSource',lambda dsn,**kwargs:calls.append((dsn,kwargs)) or adapter)
    result=open_sources(config,metadata_only=True,source_credentials={'db':'SECRET_DIRECT_DSN'})
    assert result==[adapter]
    assert calls[0][0]=='SECRET_DIRECT_DSN'
    assert calls[0][1]['allowed_columns']=={'business.metrics':['metric_code']}
    assert os.environ['EXISTING_DB_ENV']=='mysql://reader:OLD_ENV_SECRET@localhost/business'
    with pytest.raises(LightGraphError,match='deployment_retention_unverified'):
        open_sources(config,source_credentials={'db':'SECRET_DIRECT_DSN'})


def test_source_key_not_available_in_other_session_or_after_revision_change(client,monkeypatch):
    headers=session(client)
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',fake_open([]))
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body()).json()
    other=session(client)
    assert client.get('/api/projects/alpha/credentials',headers=other).json()['database_sources']==[]
    config=result['config']
    config['goal']['purpose']='Extract definitions'
    assert client.put('/api/projects/alpha/config',json=config).status_code==200
    blocked=client.post('/api/projects/alpha/runs',headers=other)
    assert blocked.status_code==404 and blocked.json()['error']['code']=='source_credential_missing'
    config['sources'][0]['credential_revision']='new_connection_revision'
    client.put('/api/projects/alpha/config',json=config)
    assert client.get('/api/projects/alpha/credentials',headers=headers).json()['database_sources']==[]


def test_private_worker_payload_carries_credentials_without_serializing_them_in_config(client,monkeypatch):
    set_model(client)
    config=client.app.state.workspace.resolved_config('alpha')
    environ=dict(os.environ)
    payload=_resolved_payload(config,client.app.state.workspace.project_path('alpha'),
        source_credentials={'db':'SECRET_DSN_PIPE'},model_api_key='SECRET_KEY_PIPE')
    assert payload['source_credentials']=={'db':'SECRET_DSN_PIPE'}
    assert payload['config']['model']['api_key']=='SECRET_KEY_PIPE'
    assert 'SECRET_KEY_PIPE' not in json.dumps(config.model_dump(mode='json'))
    assert dict(os.environ)==environ
    scan_no_secrets(client,'SECRET_DSN_PIPE','SECRET_KEY_PIPE')


def test_worker_opens_sources_with_pipe_credentials_and_clears_payload(client,monkeypatch,tmp_path):
    from lightgraph.web.worker import execute
    config=client.app.state.workspace.resolved_config('alpha')
    path=client.app.state.workspace.project_path('alpha')/'runs'/'private.result.json'
    path.parent.mkdir(exist_ok=True)
    payload=_resolved_payload(config,client.app.state.workspace.project_path('alpha'),source_credentials={'db':'SECRET_PIPE_SOURCE'})
    observed=[]
    def opened(_config,**kwargs):
        observed.append(kwargs)
        raise LightGraphError('source_scope_missing')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources',opened)
    execute(payload,path)
    assert observed==[{'source_credentials':{'db':'SECRET_PIPE_SOURCE'}}]
    assert not payload
    assert 'SECRET_PIPE_SOURCE' not in path.read_text()


def test_new_mutations_remain_blocked_during_run_and_cors_allows_session_header(client,monkeypatch):
    headers=session(client)
    monkeypatch.setattr(client.app.state.manager,'active',lambda _pid:True)
    result=client.post('/api/projects/alpha/database/connect',headers=headers,json=db_body())
    assert result.status_code==409
    assert client.put('/api/projects/alpha/credentials/model',headers=headers,json={'api_key':'secret'}).status_code==409
    cors=client.options('/api/projects/alpha/credentials/model',headers={
        'Origin':'http://localhost:5173','Access-Control-Request-Method':'PUT',
        'Access-Control-Request-Headers':'X-Workbench-Session,Content-Type'})
    assert cors.status_code==200

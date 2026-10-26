"""Partially entered connections survive refresh without becoming live sources."""
import pytest
from lightgraph.tests.web_client import RevisionClient as TestClient

from lightgraph.web.app import create_app


def draft_payload(revision=1):
    return {'revision':revision,'draft':{
        'form':{'id':'','kind':'gauss_database','dsnEnv':'','tables':'','capabilityManifest':''},
        'connection':{'kind':'gauss_database','format':'dsn','dsn':'jdbc:gaussdb://unfinished',
                      'host':'','port':'','database':'','username':'','password':'SECRET_PARTIAL_DRAFT_823',
                      'schema':'','sslmode':'prefer'},
        'mode':'direct','retention':False,'checked':[]}}


def session(client,pid='alpha'):
    result=client.post(f'/api/projects/{pid}/session')
    assert result.status_code==201
    return {'X-Workbench-Session':result.json()['session_id']}


@pytest.fixture
def client(tmp_path):
    root=tmp_path/'workspace'
    with TestClient(create_app(root,frontend_dir=tmp_path/'absent')) as instance:
        for pid in ('alpha','beta'):
            assert instance.post('/api/projects',json={'name':pid,'project_id':pid}).status_code==201
        yield instance
    for path in root.rglob('*'):
        if path.is_file():
            content=path.read_bytes()
            assert b'SECRET_PARTIAL_DRAFT_823' not in content
            assert b'jdbc:gaussdb://unfinished' not in content


def test_incomplete_form_persists_without_connecting_or_changing_config(client,monkeypatch):
    monkeypatch.setattr('lightgraph.web.app.build_dsn',lambda *_:pytest.fail('draft must not validate DSN'))
    monkeypatch.setattr('lightgraph.web.app.test_connection',lambda *_:pytest.fail('draft must not connect'))
    monkeypatch.setattr('lightgraph.web.app.read_metadata',lambda *_a,**_k:pytest.fail('draft must not read metadata'))
    headers=session(client)
    path='/api/projects/alpha/database/drafts/new'
    before=client.get('/api/projects/alpha/config').json()
    payload=draft_payload()
    assert client.put(path,headers=headers,json=payload).json()=={'saved':True,'revision':1}
    result=client.get(path,headers=session(client))
    assert result.status_code==200 and result.headers['Cache-Control']=='no-store'
    assert result.json()=={'saved':True,**payload}
    assert client.get('/api/projects/alpha/config').json()==before
    assert client.get('/api/projects/alpha/credentials',headers=headers).json()['database_sources']==[]
    assert client.get('/api/projects/alpha/runs').json()['runs']==[]
    export=client.get('/api/projects/alpha/config/export')
    assert 'SECRET_PARTIAL_DRAFT_823' not in export.text


def test_drafts_require_project_session_and_do_not_cross_projects(client):
    path='/api/projects/alpha/database/drafts/new'
    payload=draft_payload()
    assert client.put(path,json=payload).status_code==400
    assert client.get(path).status_code==400
    assert client.delete(path).status_code==400
    wrong=session(client,'beta')
    assert client.put(path,headers=wrong,json=payload).status_code==400
    headers=session(client)
    assert client.put(path,headers=headers,json=payload).status_code==200
    assert client.get('/api/projects/beta/database/drafts/new',headers=wrong).json()=={'saved':False}
    assert client.get(path,headers=wrong).status_code==400
    assert client.delete(path,headers=wrong).status_code==400
    assert client.delete(path,headers=headers).json()['saved'] is False
    assert client.get(path,headers=headers).json()['saved'] is False


def test_older_autosave_cannot_overwrite_latest_form(client):
    path='/api/projects/alpha/database/drafts/new'; headers=session(client)
    latest=draft_payload(20)
    latest['draft']['connection']['database']='latest'
    assert client.put(path,headers=headers,json=latest).json()['revision']==20
    assert client.put(path,headers=headers,json=draft_payload(10)).json()['revision']==20
    assert client.get(path,headers=headers).json()['draft']['connection']['database']=='latest'


def test_existing_source_draft_is_separate_and_cannot_rebind_source(client):
    config=client.get('/api/projects/alpha/config').json()
    config['sources']=[{'id':'warehouse','kind':'gauss_database','dsn_env':'TEST_DSN','allowed_tables':['public.metrics']}]
    assert client.put('/api/projects/alpha/config',json=config).status_code==200
    headers=session(client); path='/api/projects/alpha/database/drafts/source-warehouse'
    payload=draft_payload()
    assert client.put(path,headers=headers,json=payload).status_code==400
    payload['draft']['form']['id']='warehouse'
    assert client.put(path,headers=headers,json=payload).status_code==200
    assert client.get('/api/projects/alpha/database/drafts/new',headers=headers).json()=={'saved':False}
    assert client.get('/api/projects/alpha/database/drafts/source-missing',headers=headers).status_code==404
    config['sources']=[]
    assert client.put('/api/projects/alpha/config',json=config).status_code==200
    assert client.delete(path,headers=headers).status_code==200


def test_draft_available_after_service_restart(tmp_path):
    root=tmp_path/'workspace'; path='/api/projects/alpha/database/drafts/new'
    with TestClient(create_app(root,frontend_dir=tmp_path/'absent')) as first:
        first.post('/api/projects',json={'name':'alpha','project_id':'alpha'})
        assert first.put(path,headers=session(first),json=draft_payload()).status_code==200
    with TestClient(create_app(root,frontend_dir=tmp_path/'absent')) as second:
        assert second.get(path,headers=session(second)).json()=={'saved':True,**draft_payload()}


@pytest.mark.parametrize('field,value',[('unknown','forbidden'),('rows',[['should not be stored']])])
def test_draft_rejects_unlisted_payload_fields(client,field,value):
    payload=draft_payload(); payload['draft'][field]=value
    result=client.put('/api/projects/alpha/database/drafts/new',headers=session(client),json=payload)
    assert result.status_code==422
    assert 'SECRET_PARTIAL_DRAFT_823' not in result.text


def test_active_extraction_does_not_block_draft_but_draft_never_changes_it(client,monkeypatch):
    monkeypatch.setattr(client.app.state.manager,'active',lambda *_:True)
    headers=session(client); path='/api/projects/alpha/database/drafts/new'
    assert client.put(path,headers=headers,json=draft_payload()).status_code==200
    assert client.delete(path,headers=headers).status_code==200
    assert client.get('/api/projects/alpha/config').json()['sources']==[]


def test_deletion_blocks_delayed_autosave_after_refresh(client):
    path='/api/projects/alpha/database/drafts/new'; headers=session(client)
    assert client.put(path,headers=headers,json=draft_payload(10)).status_code==200
    assert client.request('DELETE',path,headers=headers,json={'revision':30}).status_code==200
    other=session(client)
    assert client.put(path,headers=other,json=draft_payload(20)).json()['revision']==30
    assert client.get(path,headers=other).json()=={'saved':False,'revision':30}
    assert client.put(path,headers=other,json=draft_payload(40)).json()['revision']==40
    assert client.get(path,headers=other).json()['saved'] is True

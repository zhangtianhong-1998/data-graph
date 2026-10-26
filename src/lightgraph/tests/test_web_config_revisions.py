"""Use the raw HTTP client: no helper may supply missing revision conditions."""
from copy import deepcopy

import pytest
import yaml
from fastapi.testclient import TestClient

from lightgraph.web.app import create_app
from lightgraph.web.workspace import Workspace


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'workspace', frontend_dir=tmp_path/'missing')) as client:
        assert client.post('/api/projects',json={'name':'revision','project_id':'p'}).status_code==201
        yield client


def current(client):
    response=client.get('/api/projects/p/config')
    assert response.status_code==200
    return response.json(),response.headers['etag']


def upload(client,name,body):
    return client.post('/api/projects/p/files',headers={'If-Match':current(client)[1]},files={'file':(name,body)})


def test_two_tabs_cannot_replace_a_newer_config_and_revision_survives_restart(client):
    original,tag=current(client)
    first=deepcopy(original);first['goal']['purpose']='first writer'
    response=client.put('/api/projects/p/config',headers={'If-Match':tag},json=first)
    assert response.status_code==200
    assert int(response.headers['etag'].strip('"'))>int(tag.strip('"'))
    second=deepcopy(original);second['goal']['exclude']=['second writer']
    conflict=client.put('/api/projects/p/config',headers={'If-Match':tag},json=second)
    assert conflict.status_code==409
    assert conflict.json()['error']['code']=='config_revision_conflict'
    assert current(client)[0]['goal']['purpose']=='first writer'
    reopened=Workspace(client.app.state.workspace.root)
    assert reopened.config_etag('p')==response.headers['etag']
    assert client.put('/api/projects/p/config',json=first).status_code==428


def test_asset_binding_import_and_run_check_the_same_revision(client,monkeypatch):
    file=upload(client,'note.md',b'# context').json()
    imported=upload(client,'config.yaml',b'sources: []\ngoal: {purpose: imported}').json()
    config,tag=current(client);config['goal']['purpose']='run purpose'
    assert client.put('/api/projects/p/config',headers={'If-Match':tag},json=config).status_code==200
    stale={'If-Match':tag}
    session=client.post('/api/projects/p/session',json={}).json()['session_id']
    stale['X-Workbench-Session']=session
    for method,path,kwargs in [
        ('POST','/files',{'files':{'file':('other.md',b'# ignored')}}),
        ('PATCH',f"/files/{file['file_id']}/binding",{'json':{'purpose':'mixed','scope':{}}}),
        ('POST','/config/import',{'json':{'file_id':imported['file_id']}}),
        ('DELETE',f"/files/{file['file_id']}",{}),
        ('DELETE','/sources/missing',{}),
        ('DELETE','/sources/missing/tables',{'json':{'table':'public.t'}}),
        ('POST','/runs',{}),
    ]:
        response=client.request(method,'/api/projects/p'+path,headers=stale,**kwargs)
        assert response.status_code==409,(path,response.text)
    # A run is bound to the current saved revision while the same project lock is held.
    observed=[]
    def start(pid,**kwargs):
        revision=client.app.state.workspace.config_revision(pid)
        observed.append(revision)
        return {'run_id':'test','config_revision':revision}
    monkeypatch.setattr(client.app.state.manager,'start',start)
    _,live=current(client)
    response=client.post('/api/projects/p/runs',headers={'If-Match':live})
    assert response.status_code==202
    assert response.json()['config_revision']==observed[0]==int(live.strip('"'))
    assert client.post('/api/projects/p/runs').status_code==428


def test_failed_atomic_config_write_consumes_revision_without_rolling_it_back(client,monkeypatch):
    workspace=client.app.state.workspace
    config,tag=current(client)
    original=workspace._atomic
    def fail_config(path,data):
        if path.name=='config.yaml':
            raise OSError('simulated')
        original(path,data)
    monkeypatch.setattr(workspace,'_atomic',fail_config)
    with pytest.raises(OSError):
        workspace.save_config('p',config)
    monkeypatch.setattr(workspace,'_atomic',original)
    after,revision=current(client)
    assert after==config
    assert revision!=tag
    assert client.put('/api/projects/p/config',headers={'If-Match':tag},json=config).status_code==409


def test_manifest_upload_validates_source_and_never_grants_runtime_verification(client):
    manifest={'db':{'product':'unknown','version':'unknown','driver':'psycopg_dbapi',
                    'capabilities':{},'consistent_snapshot':False,'actual_database_verified':False}}
    url='/api/projects/p/database/capabilities?source_id=db&kind=gauss_database'
    before=client.get('/api/projects/p').json()['files']
    invalid=client.post(url,headers={'If-Match':current(client)[1]},files={'file':('capabilities.yaml',b'db: {api_key: private}')})
    assert invalid.status_code==400 and 'private' not in invalid.text
    assert client.get('/api/projects/p').json()['files']==before
    response=client.post(url,headers={'If-Match':current(client)[1]},files={'file':('capabilities.yaml',yaml.safe_dump(manifest))})
    assert response.status_code==201,response.text
    result=response.json()
    assert result['runtime_ready'] is False and result['version_matching_required'] is True
    assert 'query_cancel' in result['missing_capabilities']
    assert result['file']['kind']=='yaml'
    assert current(client)[1]==response.headers['etag']
    wrong_source=client.post(url.replace('source_id=db','source_id=other'),headers={'If-Match':current(client)[1]},files={'file':('capabilities.yaml',yaml.safe_dump(manifest))})
    assert wrong_source.status_code==400


def test_revision_headers_are_available_to_allowed_workbench_origins(client):
    response=client.options('/api/projects/p/config',headers={'Origin':'http://127.0.0.1:5173',
        'Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'content-type,if-match'})
    assert response.status_code==200
    response=client.get('/api/projects/p/config',headers={'Origin':'http://127.0.0.1:5173'})
    assert 'ETag' in response.headers['access-control-expose-headers']


@pytest.mark.parametrize(('kind','driver'),[('gauss_database','psycopg/3.2.1'),('mysql_database','PyMySQL/1.1.1')])
def test_capability_upload_accepts_real_versioned_driver_manifest(client,kind,driver):
    from lightgraph.sources.gauss import REQUIRED_RUNTIME
    manifest={'db':{'product':'test-product','version':'test-version','driver':driver,
        'capabilities':{key:'verified' for key in REQUIRED_RUNTIME},
        'consistent_snapshot':True,'actual_database_verified':True}}
    response=client.post(f'/api/projects/p/database/capabilities?source_id=db&kind={kind}',
        headers={'If-Match':current(client)[1]},files={'file':('capabilities.yaml',yaml.safe_dump(manifest))})
    assert response.status_code==201,response.text
    assert response.json()['runtime_ready'] and response.json()['version_matching_required']
    wrong_kind='mysql_database' if kind=='gauss_database' else 'gauss_database'
    response=client.post(f'/api/projects/p/database/capabilities?source_id=db&kind={wrong_kind}',
        headers={'If-Match':current(client)[1]},files={'file':('capabilities.yaml',yaml.safe_dump(manifest))})
    assert response.status_code==400

"""Vue workbench API over project-scoped files and the existing graph engine.

Uploads are user-provided project assets, not cached database query results.
There is no raw-query endpoint; the discovery Agent keeps its internal SQL guard.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import uuid
import re

import yaml
from fastapi import FastAPI, File, Header, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from lightgraph.errors import LightGraphError
from .workspace import Workspace
from .execution import RunManager, read_graph, read_metadata
from .credentials import SessionCredentials
from .database_access import (ConnectionSpec, build_dsn, test_connection,
                              discover_tables, parse_table_list_excel, normalize_selected_tables)

ROOT=Path(__file__).resolve().parents[3]
MAX_REQUEST_BYTES=21*1024*1024
ALLOWED_ORIGINS={f'http://{host}:{port}' for host in ('127.0.0.1','localhost') for port in (8000,5173,4173)}


def error_response(code: str, status: int=400) -> JSONResponse:
    return JSONResponse({'error':{'code':code}},status_code=status,headers={'Cache-Control':'no-store'})


def error_status(code: str) -> int:
    if code.endswith(('_not_found','_missing')) or code in {'project_not_found','file_not_found','run_not_found'}:
        return 404
    if 'too_large' in code or 'size_limit' in code:
        return 413
    if 'active' in code or 'exists' in code or 'running' in code or 'conflict' in code:
        return 409
    return 400


class RequestBoundary:
    """Bound inbound bodies and catch errors before ASGI logs source contents."""
    def __init__(self, app):
        self.app=app

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        headers={k.lower():v for k,v in scope.get('headers',[])}
        origin=headers.get(b'origin',b'').decode('latin-1')
        host=headers.get(b'host',b'').decode('latin-1')
        if scope['method'] not in ('GET','HEAD','OPTIONS') and origin:
            expected=f"{scope.get('scheme','http')}://{host}"
            if origin not in ALLOWED_ORIGINS and origin!=expected:
                return await error_response('origin_forbidden',403)(scope,receive,send)
        try:
            length=int(headers.get(b'content-length',b'0'))
            if length<0 or length>MAX_REQUEST_BYTES:
                return await error_response('request_too_large',413)(scope,receive,send)
        except ValueError:
            return await error_response('invalid_content_length')(scope,receive,send)
        received=0
        started=False
        async def limited_receive():
            nonlocal received
            message=await receive()
            received+=len(message.get('body',b''))
            if received>MAX_REQUEST_BYTES:
                raise LightGraphError('request_too_large')
            return message
        async def tracked_send(message):
            nonlocal started
            if message['type']=='http.response.start':
                started=True
                if scope.get('path','').startswith('/api/'):
                    message['headers']=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control']
                    message['headers'].append((b'cache-control',b'no-store'))
            await send(message)
        try:
            return await self.app(scope,limited_receive,tracked_send)
        except LightGraphError as exc:
            if not started:
                await error_response(exc.code,error_status(exc.code))(scope,receive,send)
        except Exception:
            if not started:
                await error_response('internal_error',500)(scope,receive,send)


class InputModel(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)


class NewProject(InputModel):
    name: str=Field(min_length=1,max_length=120)
    project_id: str|None=None


class RenameProject(InputModel):
    name: str=Field(min_length=1,max_length=120)


class DocumentBinding(InputModel):
    purpose: str
    scope: dict=Field(default_factory=dict)


class ImportConfig(InputModel):
    file_id: str


class ModelCredential(InputModel):
    api_key: SecretStr = Field(min_length=1,max_length=16384)


class DatabaseRequest(InputModel):
    connection: ConnectionSpec


class ConnectDatabase(DatabaseRequest):
    source_id: str = Field(min_length=1,max_length=128)
    allowed_tables: list[str] = Field(min_length=1,max_length=1000)
    deployment_retention_verified: bool | None = None
    capability_manifest: str | None = None


def create_app(workspace_root: str|Path|None=None, *, frontend_dir: str|Path|None=None,
               manager_factory=RunManager) -> FastAPI:
    workspace=Workspace(Path(workspace_root) if workspace_root else ROOT/'workspace')
    manager=manager_factory(workspace)
    credentials=SessionCredentials()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            credentials.clear()
            await run_in_threadpool(manager.shutdown)

    app=FastAPI(title='Data Graph Workbench',version='1.1.0',lifespan=lifespan)
    app.state.workspace=workspace
    app.state.manager=manager
    app.state.credentials=credentials
    app.add_middleware(CORSMiddleware,allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
        allow_headers=['Content-Type','X-Workbench-Session'],allow_credentials=False)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    app.add_middleware(RequestBoundary)

    @app.exception_handler(LightGraphError)
    async def safe_error(request,exc):
        return error_response(exc.code,error_status(exc.code))

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request,exc):
        return error_response('request_invalid',422)

    @app.exception_handler(HTTPException)
    async def http_error(request,exc):
        return error_response('not_found' if exc.status_code==404 else 'request_invalid',exc.status_code)

    def mutable(pid):
        workspace.get(pid)
        if manager.active(pid):raise LightGraphError('project_run_active')

    def resolved_credentials(pid, token):
        if not token:
            return {}, None
        return credentials.resolve(pid,token,workspace.resolved_config(pid))

    def credential_status(pid, token):
        sources, model=resolved_credentials(pid,token)
        return {'database_sources':sorted(sources),'model':model is not None}

    @app.get('/api/health')
    def health():
        return {'status':'ok','service':'data-graph','version':'1.1.0'}

    @app.get('/api/projects')
    def projects():
        return {'projects':workspace.list_projects()}

    @app.post('/api/projects',status_code=201)
    def create_project(data:NewProject):
        return workspace.create(data.name,data.project_id)

    @app.get('/api/projects/{pid}')
    def project(pid:str):
        return workspace.get(pid)

    @app.patch('/api/projects/{pid}')
    def rename_project(pid:str,data:RenameProject):
        with workspace.lock(pid):
            mutable(pid)
            return workspace.rename(pid,data.name)

    @app.post('/api/projects/{pid}/session',status_code=201)
    def create_session(pid:str):
        workspace.get(pid)
        return {'session_id':credentials.create(pid),'expires_in_seconds':credentials.ttl_seconds}

    @app.get('/api/projects/{pid}/credentials')
    def get_credentials(pid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            workspace.get(pid)
            return credential_status(pid,x_workbench_session)

    @app.put('/api/projects/{pid}/credentials/model')
    def model_credential(pid:str,data:ModelCredential,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.set_model(pid,x_workbench_session,workspace.resolved_config(pid),data.api_key.get_secret_value())
            return credential_status(pid,x_workbench_session)

    @app.delete('/api/projects/{pid}/credentials/model')
    def clear_model_credential(pid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.clear_model(pid,x_workbench_session)
            return credential_status(pid,x_workbench_session)

    @app.post('/api/projects/{pid}/database/test')
    def database_test(pid:str,data:DatabaseRequest):
        with workspace.lock(pid):
            mutable(pid)
            return test_connection(data.connection)

    @app.post('/api/projects/{pid}/database/tables')
    def database_tables(pid:str,data:DatabaseRequest):
        with workspace.lock(pid):
            mutable(pid)
            return discover_tables(data.connection)

    @app.post('/api/projects/{pid}/database/table-list')
    async def database_table_list(pid:str,file:UploadFile=File(...)):
        from .database_access import MAX_EXCEL_BYTES
        try:
            content=await file.read(MAX_EXCEL_BYTES+1)
            if len(content)>MAX_EXCEL_BYTES:
                raise LightGraphError('database_table_list_too_large')
            def parse():
                with workspace.lock(pid):
                    mutable(pid)
                    return {'tables':parse_table_list_excel(content)}
            return await run_in_threadpool(parse)
        finally:
            await file.close()

    @app.post('/api/projects/{pid}/database/connect')
    def database_connect(pid:str,data:ConnectDatabase,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid,x_workbench_session)
            old_config=workspace.get_config(pid)
            old_resolved=workspace.resolved_config(pid)
            old_secrets,_=credentials.resolve(pid,x_workbench_session,old_resolved)
            existing=next((s for s in old_resolved.sources if s.id==data.source_id),None)
            if existing is None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',data.source_id):
                raise LightGraphError('source_id_invalid')
            if any(s.id==data.source_id and s.kind not in {'gauss_database','mysql_database'} for s in old_resolved.sources):
                raise LightGraphError('source_id_conflict')
            if existing is not None and existing.kind!=data.connection.kind:
                raise LightGraphError('source_kind_conflict')
            # The metadata adapter validates these exact names. Manual choices
            # must also work beyond the bounded table-discovery first page.
            selected=normalize_selected_tables(data.connection,data.allowed_tables)
            dsn=build_dsn(data.connection)
            config={**old_config,'sources':[s for s in old_config['sources'] if s['id']!=data.source_id]}
            previous=next((s for s in old_config['sources'] if s['id']==data.source_id),None)
            source_config=dict(previous) if previous and previous['kind']==data.connection.kind else {}
            if set(source_config.get('allowed_columns') or {})-set(selected):
                raise LightGraphError('source_column_scope_conflict')
            source_config.update({'id':data.source_id,'kind':data.connection.kind,'dsn_env':None,
                'credential_mode':'session','credential_revision':uuid.uuid4().hex,
                'allowed_tables':selected})
            if 'capability_manifest' in data.model_fields_set:
                source_config['capability_manifest']=data.capability_manifest
            config['sources'].append(source_config)
            if data.deployment_retention_verified is not None:
                config['deployment_retention_verified']=data.deployment_retention_verified
            workspace.save_config(pid,config)
            source=next(s for s in workspace.resolved_config(pid).sources if s.id==data.source_id)
            credentials.set_source(pid,x_workbench_session,source,dsn)
            try:
                secrets_by_source,_=resolved_credentials(pid,x_workbench_session)
                metadata=read_metadata(workspace,pid,refresh=True,source_credentials=secrets_by_source,
                                       source_ids=[data.source_id])
            except Exception:
                workspace.save_config(pid,old_config)
                credentials.clear_source(pid,x_workbench_session,data.source_id)
                old_source=next((s for s in old_resolved.sources if s.id==data.source_id),None)
                if old_source and data.source_id in old_secrets:
                    credentials.set_source(pid,x_workbench_session,old_source,old_secrets[data.source_id])
                raise
            from .metadata_views import _yaml_files
            metadata['files']=_yaml_files(metadata['tables'],metadata.get('sources'))
            return {'config':workspace.get_config(pid),'metadata':metadata,
                    'credentials':credential_status(pid,x_workbench_session)}

    @app.post('/api/projects/{pid}/files',status_code=201)
    async def upload(pid:str,file:UploadFile=File(...)):
        try:
            content=await file.read(20*1024*1024+1)
            if len(content)>20*1024*1024:
                raise LightGraphError('file_too_large')
            def save():
                with workspace.lock(pid):
                    mutable(pid)
                    return workspace.add_file(pid,file.filename or '',content)
            return await run_in_threadpool(save)
        finally:
            await file.close()

    @app.get('/api/projects/{pid}/files/{fid}/content')
    def text_file(pid:str,fid:str):
        return {'content':workspace.text_content(pid,fid)}

    @app.get('/api/projects/{pid}/files/{fid}/metadata')
    def preview_file(pid:str,fid:str):
        from .metadata_views import preview_file_metadata
        with workspace.lock(pid):
            mutable(pid)
            return preview_file_metadata(workspace,pid,fid)

    @app.patch('/api/projects/{pid}/files/{fid}/binding')
    def binding(pid:str,fid:str,data:DocumentBinding):
        with workspace.lock(pid):
            mutable(pid)
            return workspace.document_binding(pid,fid,data.purpose,data.scope)

    @app.get('/api/projects/{pid}/config')
    def config(pid:str):
        return workspace.get_config(pid)

    @app.put('/api/projects/{pid}/config')
    def save_config(pid:str,data:dict):
        with workspace.lock(pid):
            mutable(pid)
            return workspace.save_config(pid,data)

    @app.post('/api/projects/{pid}/config/import')
    def import_config(pid:str,data:ImportConfig):
        with workspace.lock(pid):
            mutable(pid)
            return workspace.import_config(pid,data.file_id)

    @app.get('/api/projects/{pid}/config/export')
    def export_config(pid:str):
        content=yaml.safe_dump(workspace.get_config(pid),allow_unicode=True,sort_keys=False)
        return Response(content,media_type='application/yaml',headers={
            'Content-Disposition':'attachment; filename="config.yaml"'})

    @app.get('/api/projects/{pid}/metadata')
    def metadata(pid:str):
        with workspace.lock(pid):
            workspace.get(pid)
            return read_metadata(workspace,pid)

    @app.get('/api/projects/{pid}/metadata/yaml')
    def metadata_documents(pid:str,source_id:str|None=None,schema:str|None=None,table:str|None=None):
        from .metadata_views import metadata_yaml
        with workspace.lock(pid):
            workspace.get(pid)
            return metadata_yaml(workspace,pid,source_id=source_id,schema=schema,table=table)

    @app.post('/api/projects/{pid}/metadata/refresh')
    def refresh_metadata(pid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            sources,_=resolved_credentials(pid,x_workbench_session)
            options={'source_credentials':sources} if sources else {}
            return read_metadata(workspace,pid,refresh=True,**options)

    @app.get('/api/projects/{pid}/graph')
    def graph(pid:str,layer:str='business'):
        if layer not in ('business','metadata'):raise LightGraphError('graph_layer_invalid')
        with workspace.lock(pid):
            workspace.get(pid)
            return read_graph(workspace,pid,layer)

    @app.get('/api/projects/{pid}/runs')
    def runs(pid:str):
        workspace.get(pid)
        return {'runs':manager.list(pid)}

    @app.post('/api/projects/{pid}/runs',status_code=202)
    def start_run(pid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            sources,model=resolved_credentials(pid,x_workbench_session)
            options={}
            if sources:options['source_credentials']=sources
            if model is not None:options['model_api_key']=model
            return manager.start(pid,**options)

    @app.get('/api/projects/{pid}/runs/{rid}')
    def run_status(pid:str,rid:str):
        workspace.get(pid)
        return manager.get(pid,rid)

    @app.post('/api/projects/{pid}/runs/{rid}/cancel')
    def cancel_run(pid:str,rid:str):
        workspace.get(pid)
        return manager.cancel(pid,rid)

    @app.api_route('/api/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE'])
    def missing_api(path:str):
        return error_response('not_found',404)

    assets=Path(frontend_dir) if frontend_dir else ROOT/'src/workbench/dist'
    if (assets/'index.html').is_file():
        # Vue keeps its page tabs in state; no catch-all filesystem lookup.
        app.mount('/',StaticFiles(directory=assets,html=True),name='workbench')
    else:
        @app.get('/')
        def build_missing():
            return error_response('frontend_build_required',503)
    return app

"""Vue workbench API over project-scoped files and the existing graph engine.

Uploads are user-provided project assets, not cached database query results.
There is no raw-query endpoint; the discovery Agent keeps its internal SQL guard.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import uuid
import re
from typing import Literal

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
from .execution import RunManager, read_graph, read_graph_details, read_metadata, identity_review, resolve_identity
from .credentials import SessionCredentials
from .saved_connections import SavedConnections
from .connection_drafts import SaveConnectionDraft
from .database_access import (ConnectionSpec, build_dsn, test_connection,
                              discover_tables, parse_table_list_excel, normalize_selected_tables)

ROOT=Path(__file__).resolve().parents[3]
MAX_REQUEST_BYTES=21*1024*1024
ALLOWED_ORIGINS={f'http://{host}:{port}' for host in ('127.0.0.1','localhost') for port in (8000,5173,4173)}


def error_response(code: str, status: int=400) -> JSONResponse:
    return JSONResponse({'error':{'code':code}},status_code=status,headers={'Cache-Control':'no-store'})


def error_status(code: str) -> int:
    if code == 'config_revision_required':
        return 428
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


class DeleteConnectionDraft(InputModel):
    revision: int = Field(ge=0,le=9007199254740991)


class DeleteTable(InputModel):
    table: str = Field(min_length=1, max_length=512)


class IdentityRevision(InputModel):
    revision: int = Field(ge=0,le=9007199254740991)


class IdentityDecision(IdentityRevision):
    candidate_id: str = Field(min_length=1,max_length=128)
    verdict: Literal['same','different']


class DiscoveryDecision(IdentityRevision):
    candidate_id: str = Field(pattern=r'^candidate_[a-f0-9]{24}$')
    action: Literal['dismiss','retry','reopen']


class DatabaseRequest(InputModel):
    connection: ConnectionSpec


class ConnectDatabase(DatabaseRequest):
    source_id: str = Field(min_length=1,max_length=128)
    allowed_tables: list[str] = Field(min_length=1,max_length=1000)
    deployment_retention_verified: bool | None = None
    capability_manifest: str | None = None
    remember_connection: bool = False


def create_app(workspace_root: str|Path|None=None, *, frontend_dir: str|Path|None=None,
               manager_factory=RunManager) -> FastAPI:
    workspace=Workspace(Path(workspace_root) if workspace_root else ROOT/'workspace')
    manager=manager_factory(workspace)
    credentials=SessionCredentials()
    saved_connections=SavedConnections(workspace.root)

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
    app.state.saved_connections=saved_connections
    app.add_middleware(CORSMiddleware,allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
        allow_headers=['Content-Type','X-Workbench-Session','If-Match'],expose_headers=['ETag'],allow_credentials=False)
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

    def versioned(pid, value, status=200):
        return JSONResponse(value, status_code=status, headers={"ETag": workspace.config_etag(pid)})

    def mutable(pid):
        workspace.get(pid)
        if manager.active(pid):raise LightGraphError('project_run_active')

    def resolved_credentials(pid, token, *, source_ids=None, profile_errors=None):
        if not token:
            return {}, None
        config=workspace.resolved_config(pid)
        sources,model=credentials.resolve(pid,token,config)
        for source in config.sources:
            if source_ids is not None and source.id not in source_ids:
                continue
            if source.credential_mode == 'session' and source.id not in sources:
                try:
                    saved=saved_connections.load(pid,source)
                except LightGraphError as exc:
                    if profile_errors is None or not exc.code.startswith('saved_connection_'):
                        raise
                    profile_errors[source.id]=exc.code
                    continue
                if saved is not None:
                    credentials.set_source(pid,token,source,saved['dsn'])
                    sources[source.id]=saved['dsn']
        return sources,model

    def credential_status(pid, token):
        # A broken profile must not block editing other sources or model settings.
        # Execution still requires every needed profile to load successfully.
        errors={}
        sources, model=resolved_credentials(pid,token,profile_errors=errors)
        result={'database_sources':sorted(sources),'model':model is not None}
        if errors:
            result['database_errors']=errors
        return result

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

    @app.delete('/api/projects/{pid}')
    def remove_project(pid: str, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        from .deletion import delete_project
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid, x_workbench_session)
            workspace.require_config_revision(pid, if_match)
            result = delete_project(workspace, pid)
            credentials.clear_project(pid)
            return result

    def remove_input(pid, token, expected, **selection):
        from .deletion import delete_input
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid, token)
            workspace.require_config_revision(pid, expected)
            result = delete_input(workspace, pid, **selection)
            for sid in result['removed_source_ids']:
                credentials.clear_source_sessions(pid, sid)
            return versioned(pid, result)

    @app.delete('/api/projects/{pid}/files/{fid}')
    def remove_file(pid: str, fid: str, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        return remove_input(pid, x_workbench_session, if_match, fid=fid)

    @app.delete('/api/projects/{pid}/sources/{sid}')
    def remove_source(pid: str, sid: str, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        return remove_input(pid, x_workbench_session, if_match, sid=sid)

    @app.delete('/api/projects/{pid}/sources/{sid}/tables')
    def remove_table(pid: str, sid: str, data: DeleteTable, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        return remove_input(pid, x_workbench_session, if_match, sid=sid, table=data.table)

    @app.post('/api/projects/{pid}/session',status_code=201)
    def create_session(pid:str):
        with workspace.lock(pid):
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
    def database_connect(pid:str,data:ConnectDatabase,x_workbench_session:str|None=Header(default=None),if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid,x_workbench_session)
            workspace.require_config_revision(pid,if_match)
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
                secrets_by_source,_=resolved_credentials(pid,x_workbench_session,source_ids={data.source_id})
                metadata=read_metadata(workspace,pid,refresh=True,source_credentials=secrets_by_source,
                                       source_ids=[data.source_id])
                # Commit the profile only after metadata succeeds. A failed
                # replacement must keep the previously saved connection usable.
                if data.remember_connection:
                    saved_connections.save(pid,source,data.connection,dsn)
                else:
                    saved_connections.delete(pid,source.id)
            except Exception:
                workspace.save_config(pid,old_config)
                credentials.clear_source(pid,x_workbench_session,data.source_id)
                old_source=next((s for s in old_resolved.sources if s.id==data.source_id),None)
                if old_source and data.source_id in old_secrets:
                    credentials.set_source(pid,x_workbench_session,old_source,old_secrets[data.source_id])
                raise
            from .metadata_views import _yaml_files
            metadata['files']=_yaml_files(metadata['tables'],metadata.get('sources'))
            return versioned(pid, {'config':workspace.get_config(pid),'metadata':metadata,
                    'credentials':credential_status(pid,x_workbench_session)})

    def draft_source(pid, key):
        if key == 'new':
            return None
        if not key.startswith('source-'):
            raise LightGraphError('saved_connection_invalid')
        source=next((s for s in workspace.resolved_config(pid).sources if s.id==key[7:]),None)
        if source is None or source.kind not in {'gauss_database','mysql_database'}:
            raise LightGraphError('source_not_found')
        return source

    @app.get('/api/projects/{pid}/database/drafts/{key}')
    def load_connection_draft(pid:str,key:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            workspace.get(pid)
            credentials.require(pid,x_workbench_session)
            draft_source(pid,key)
            saved=saved_connections.load_draft(pid,key)
            if saved is not None:
                return {'saved':True,**saved}
            revision=saved_connections.load_draft_revision(pid,key)
            return {'saved':False,**({'revision':revision} if revision else {})}

    @app.put('/api/projects/{pid}/database/drafts/{key}')
    def save_connection_draft(pid:str,key:str,data:SaveConnectionDraft,x_workbench_session:str|None=Header(default=None)):
        # Incomplete forms are drafts, not live credentials or extraction config.
        # Saving never tests the connection or changes an active run's inputs.
        with workspace.lock(pid):
            workspace.get(pid)
            credentials.require(pid,x_workbench_session)
            source=draft_source(pid,key)
            if source and (data.draft.form.id!=source.id or data.draft.form.kind!=source.kind):
                raise LightGraphError('saved_connection_invalid')
            revision=saved_connections.save_draft(pid,key,data.draft,data.revision)
            return {'saved':True,'revision':revision}

    @app.delete('/api/projects/{pid}/database/drafts/{key}')
    def delete_connection_draft(pid:str,key:str,data:DeleteConnectionDraft|None=None,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            workspace.get(pid)
            credentials.require(pid,x_workbench_session)
            # Orphaned drafts remain removable after their source was deleted.
            revision=saved_connections.delete_draft(pid,key,data.revision if data else None)
            return {'saved':saved_connections.load_draft(pid,key) is not None,'revision':revision}

    @app.get('/api/projects/{pid}/database/connections/{sid}')
    def load_database_connection(pid:str,sid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            workspace.get(pid)
            credentials.require(pid,x_workbench_session)
            source=next((s for s in workspace.resolved_config(pid).sources if s.id==sid),None)
            if source is None or source.kind not in {'gauss_database','mysql_database'}:
                raise LightGraphError('source_not_found')
            saved=saved_connections.load(pid,source)
            # The explicit edit form needs the saved input. Other config,
            # status, metadata and run endpoints never return these values.
            return {'saved':False} if saved is None else {'saved':True,'connection':saved['connection']}

    @app.delete('/api/projects/{pid}/database/connections/{sid}')
    def forget_database_connection(pid:str,sid:str,x_workbench_session:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid,x_workbench_session)
            if not any(s.id==sid for s in workspace.resolved_config(pid).sources):
                raise LightGraphError('source_not_found')
            saved_connections.delete(pid,sid)
            credentials.clear_source_sessions(pid,sid)
            return {'saved':False}

    @app.post('/api/projects/{pid}/database/capabilities',status_code=201)
    async def upload_capabilities(pid:str,source_id:str,kind:str,file:UploadFile=File(...),if_match:str|None=Header(default=None)):
        from ..contracts import CapabilityManifest
        from .workspace import safe_yaml
        from ..sources.gauss import REQUIRED_RUNTIME
        try:
            content=await file.read(2*1024*1024+1)
            if len(content)>2*1024*1024:
                raise LightGraphError('file_too_large')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',source_id):
                raise LightGraphError('source_id_invalid')
            drivers={'gauss_database':r'(?:psycopg_dbapi|psycopg/[0-9][A-Za-z0-9.+_-]*)',
                     'mysql_database':r'(?:pymysql_dbapi|PyMySQL/[0-9][A-Za-z0-9.+_-]*)'}
            try:
                records=safe_yaml(content.decode('utf-8'))
                manifest=CapabilityManifest.model_validate(records[source_id])
                if not re.fullmatch(drivers[kind],manifest.driver):
                    raise ValueError
            except Exception:
                raise LightGraphError('capability_manifest_invalid') from None
            def save():
                with workspace.lock(pid):
                    mutable(pid)
                    workspace.require_config_revision(pid,if_match)
                    # Save only the validated selected manifest; no extra YAML payload survives.
                    clean=yaml.safe_dump({source_id:manifest.model_dump(mode='json')},allow_unicode=True).encode()
                    uploaded=workspace.add_file(pid,file.filename or 'capabilities.yaml',clean)
                    if uploaded['kind']!='yaml':
                        raise LightGraphError('capability_manifest_invalid')
                    missing=sorted(k for k in REQUIRED_RUNTIME if manifest.capabilities.get(k)!='verified')
                    ready=(not missing and manifest.consistent_snapshot and manifest.actual_database_verified
                           and manifest.product not in ('','unknown') and manifest.version not in ('','unknown'))
                    return versioned(pid,{'file':uploaded,'source_id':source_id,'runtime_ready':ready,
                        'missing_capabilities':missing,'version_matching_required':True},201)
            if not (file.filename or '').lower().endswith(('.yaml','.yml')):
                raise LightGraphError('capability_manifest_invalid')
            return await run_in_threadpool(save)
        finally:
            await file.close()

    @app.post('/api/projects/{pid}/files',status_code=201)
    async def upload(pid:str,file:UploadFile=File(...),if_match:str|None=Header(default=None)):
        try:
            content=await file.read(20*1024*1024+1)
            if len(content)>20*1024*1024:
                raise LightGraphError('file_too_large')
            def save():
                with workspace.lock(pid):
                    mutable(pid)
                    workspace.require_config_revision(pid,if_match)
                    return versioned(pid,workspace.add_file(pid,file.filename or '',content),201)
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
    def binding(pid:str,fid:str,data:DocumentBinding,if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return versioned(pid,workspace.document_binding(pid,fid,data.purpose,data.scope))

    @app.get('/api/projects/{pid}/config')
    def config(pid:str):
        with workspace.lock(pid):
            return versioned(pid,workspace.get_config(pid))

    @app.put('/api/projects/{pid}/config')
    def save_config(pid:str,data:dict,if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return versioned(pid,workspace.save_config(pid,data))

    @app.post('/api/projects/{pid}/config/import')
    def import_config(pid:str,data:ImportConfig,if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return versioned(pid,workspace.import_config(pid,data.file_id))

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
    def graph(pid:str,layer:str='business',include_candidates:bool=False,offset:int=0,limit:int=240,q:str='',kind:str=''):
        if layer not in ('business','metadata'):raise LightGraphError('graph_layer_invalid')
        with workspace.lock(pid):
            workspace.get(pid)
            return read_graph(workspace,pid,layer,include_candidates=include_candidates,offset=offset,limit=limit,q=q,kind=kind)

    @app.get('/api/projects/{pid}/graph/details')
    def graph_details(pid:str,entity_id:str|None=None,fact_id:str|None=None,include_candidates:bool=False,offset:int=0):
        with workspace.lock(pid):
            workspace.get(pid)
            return read_graph_details(workspace,pid,entity_id=entity_id,fact_id=fact_id,
                                      include_candidates=include_candidates,offset=offset)

    @app.get('/api/projects/{pid}/graph/identity-candidates')
    def identity_candidates(pid:str,offset:int=0,decision_offset:int=0):
        with workspace.lock(pid):
            workspace.get(pid)
            return identity_review(workspace,pid,offset=offset,decision_offset=decision_offset)

    @app.post('/api/projects/{pid}/graph/identity-decisions')
    def identity_decision(pid:str,data:IdentityDecision,if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return resolve_identity(workspace,pid,candidate_id=data.candidate_id,verdict=data.verdict,
                                    expected_revision=data.revision)

    @app.post('/api/projects/{pid}/graph/identity-decisions/{decision_id}/revoke')
    def revoke_identity(pid:str,decision_id:str,data:IdentityRevision,if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return resolve_identity(workspace,pid,decision_id=decision_id,expected_revision=data.revision)

    @app.get('/api/projects/{pid}/discovery/candidates')
    def discovery_candidates(pid:str,offset:int=0):
        from .discovery_review import discovery_review
        with workspace.lock(pid):
            workspace.get(pid)
            return discovery_review(workspace,pid,offset=offset)

    @app.post('/api/projects/{pid}/discovery/decisions')
    def discovery_decision(pid:str,data:DiscoveryDecision,if_match:str|None=Header(default=None)):
        from .discovery_review import discovery_review
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            return discovery_review(workspace,pid,decision=data)

    @app.get('/api/projects/{pid}/runs')
    def runs(pid:str):
        workspace.get(pid)
        return {'runs':manager.list(pid)}

    @app.post('/api/projects/{pid}/runs',status_code=202)
    def start_run(pid:str,x_workbench_session:str|None=Header(default=None),if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            sources,model=resolved_credentials(pid,x_workbench_session)
            workspace.require_config_revision(pid,if_match)
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

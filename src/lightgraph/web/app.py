"""Vue workbench API over project-scoped files and the existing graph engine.

Uploads are user-provided project assets, not cached database query results.
There is no raw-query endpoint; the discovery Agent keeps its internal SQL guard.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os
import json
import uuid
import re
from typing import Literal

import yaml
from fastapi import FastAPI, File, Header, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from lightgraph.errors import LightGraphError
from .workspace import Workspace
from . import graph_history
from .execution import RunManager, read_graph, read_graph_details, read_graph_consistently, read_metadata, identity_review, resolve_identity
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
    if code in {'retrieval_index_stale', 'retrieval_graph_required'}:
        return 409
    if code == 'retrieval_jobs_busy':
        return 429
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


class StartRun(InputModel):
    run_mode: Literal["continue", "fresh"] = "continue"


class ResumeRun(InputModel):
    action: Literal['rerun_node', 'publish_artifact', 'recompose_legacy_merge'] = 'rerun_node'
    from_node_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
    artifact_id: str | None = Field(default=None, pattern=r'^artifact_[a-f0-9]{32}$')


class RetrieveSubgraphRequest(InputModel):
    query: str = Field(min_length=1, max_length=4000)
    business_context: dict | None = None
    top_n: int = Field(default=20, ge=1, le=100)
    top_p_seeds: int = Field(default=4, ge=1, le=20)
    hops: int = Field(default=2, ge=1, le=2)
    max_per_rel: int = Field(default=30, ge=1, le=200)
    hub_degree_limit: int = Field(default=150, ge=10, le=5000)
    max_nodes: int = Field(default=80, ge=5, le=500)
    max_edges: int = Field(default=150, ge=5, le=1000)


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


class ExpectedModel(InputModel):
    base_url: str
    model: str


class ModelCredential(InputModel):
    api_key: SecretStr = Field(min_length=1,max_length=16384)
    expected_model: ExpectedModel


class ExpectedEmbedding(InputModel):
    base_url: str
    model: str


class EmbeddingCredential(InputModel):
    api_key: SecretStr = Field(min_length=1, max_length=16384)
    expected_embedding: ExpectedEmbedding


class TestEmbeddingRequest(InputModel):
    provider: Literal['local_hash', 'openai_compatible'] | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    allow_insecure_http: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    dimensions: int | None = Field(default=None, gt=0)
    query_instruction: str | None = Field(default=None, max_length=1000)


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
    allow_fact_data: StrictBool | None = None
    remember_connection: bool = False


def create_app(workspace_root: str|Path|None=None, *, frontend_dir: str|Path|None=None,
               manager_factory=RunManager) -> FastAPI:
    workspace=Workspace(Path(workspace_root) if workspace_root else ROOT/'workspace')
    manager=manager_factory(workspace)
    credentials=SessionCredentials()
    saved_connections=SavedConnections(workspace.root)
    from .retrieval_jobs import RetrievalJobs
    retrieval_jobs = RetrievalJobs(workspace)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            credentials.clear()
            await run_in_threadpool(retrieval_jobs.shutdown)
            await run_in_threadpool(manager.shutdown)

    app=FastAPI(title='Data Graph Workbench',version='1.1.0',lifespan=lifespan)
    app.state.workspace=workspace
    app.state.manager=manager
    app.state.credentials=credentials
    app.state.saved_connections=saved_connections
    app.state.retrieval_jobs=retrieval_jobs
    app.add_middleware(CORSMiddleware,allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
        allow_headers=['Content-Type','X-Workbench-Session','If-Match'],expose_headers=['ETag'],allow_credentials=False)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    app.add_middleware(RequestBoundary)

    @app.exception_handler(LightGraphError)
    async def safe_error(request,exc):
        from .config_validation import ConfigValidationError
        from ..construction.checkpoints import WorkflowCheckpointError
        if isinstance(exc, WorkflowCheckpointError) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', exc.node_id):
            return JSONResponse({'error': {'code': exc.code, 'node_id': exc.node_id}},
                                status_code=error_status(exc.code), headers={'Cache-Control': 'no-store'})
        if isinstance(exc, ConfigValidationError):
            return JSONResponse({'error': {'code': exc.code, 'diagnostics': exc.diagnostics}},
                                status_code=400, headers={'Cache-Control': 'no-store'})
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

    def resolved_credentials(pid, token, *, source_ids=None, profile_errors=None, model_errors=None,
                             include_model=True):
        if not token:
            return {}, None
        config=workspace.resolved_config(pid)
        sources,_=credentials.resolve(pid,token,config)
        # The saved profile is authoritative across refreshes and open tabs.
        # A deleted key must never be revived from another session's cache.
        try:
            model=saved_connections.load_model(pid,config) if include_model else None
        except LightGraphError as exc:
            if model_errors is None or not exc.code.startswith('saved_connection_'):
                raise
            model_errors.append(exc.code)
            model=None
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
        errors={}; model_errors=[]
        sources, model=resolved_credentials(pid,token,profile_errors=errors,model_errors=model_errors)
        result={'database_sources':sorted(sources),'model':model is not None}
        if model_errors:
            result['model_error']=model_errors[0]
        if errors:
            result['database_errors']=errors
        # A server-configured key is usable without a browser-saved profile.
        # Expose presence only; it cannot be deleted by the model-key endpoint.
        environment_name=workspace.resolved_config(pid).resolved_model.api_key_env
        if os.environ.get(environment_name):
            result['model_environment']=True
        return result

    def embedding_credential_status(pid):
        config = workspace.resolved_config(pid)
        err = None
        has_saved = False
        try:
            has_saved = saved_connections.load_embedding(pid, config) is not None
        except LightGraphError as exc:
            if not exc.code.startswith('saved_connection_'):
                raise
            err = exc.code
        result = {'embedding': has_saved}
        if err:
            result['embedding_error'] = err
        env_name = config.embedding.api_key_env
        if env_name and os.environ.get(env_name):
            result['embedding_environment'] = True
        return result

    def resolve_embedding_api_key(pid, token, config):
        if not token:
            return None
        credentials.require(pid, token)
        try:
            return saved_connections.load_embedding(pid, config)
        except LightGraphError as exc:
            if exc.code.startswith('saved_connection_'):
                return None
            raise

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
            if retrieval_jobs.active(pid):
                raise LightGraphError('retrieval_job_active')
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
    def model_credential(pid:str,data:ModelCredential,x_workbench_session:str|None=Header(default=None),if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid,x_workbench_session)
            workspace.require_config_revision(pid,if_match)
            config=workspace.resolved_config(pid)
            model=config.resolved_model
            # The key belongs to the configuration the page saved, not whichever
            # endpoint another tab (or an edited Agent file) selected afterward.
            if (data.expected_model.base_url,data.expected_model.model)!=(model.base_url,model.model):
                raise LightGraphError('model_credential_binding_conflict')
            saved_connections.save_model(pid,config,data.api_key.get_secret_value())
            credentials.clear_model_sessions(pid)
            return credential_status(pid,x_workbench_session)

    @app.delete('/api/projects/{pid}/credentials/model')
    def clear_model_credential(pid:str,x_workbench_session:str|None=Header(default=None),if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid,x_workbench_session)
            workspace.require_config_revision(pid,if_match)
            saved_connections.delete_model(pid)
            credentials.clear_model_sessions(pid)
            return credential_status(pid,x_workbench_session)

    @app.get('/api/projects/{pid}/credentials/embedding')
    def get_embedding_credential(pid: str):
        with workspace.lock(pid):
            workspace.get(pid)
            return embedding_credential_status(pid)

    @app.put('/api/projects/{pid}/credentials/embedding')
    def put_embedding_credential(pid: str, data: EmbeddingCredential, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid, x_workbench_session)
            workspace.require_config_revision(pid, if_match)
            config = workspace.resolved_config(pid)
            emb = config.embedding
            if (data.expected_embedding.base_url, data.expected_embedding.model) != (emb.base_url, emb.model):
                raise LightGraphError('embedding_credential_binding_conflict')
            saved_connections.save_embedding(pid, config, data.api_key.get_secret_value())
            return embedding_credential_status(pid)

    @app.delete('/api/projects/{pid}/credentials/embedding')
    def delete_embedding_credential(pid: str, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            credentials.require(pid, x_workbench_session)
            workspace.require_config_revision(pid, if_match)
            saved_connections.delete_embedding(pid)
            return embedding_credential_status(pid)

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
            if 'allow_fact_data' in data.model_fields_set:
                source_config['allow_fact_data']=data.allow_fact_data
            config['sources'].append(source_config)
            if data.deployment_retention_verified is not None:
                config['deployment_retention_verified']=data.deployment_retention_verified
            workspace.save_config(pid,config)
            source=next(s for s in workspace.resolved_config(pid).sources if s.id==data.source_id)
            credentials.set_source(pid,x_workbench_session,source,dsn)
            try:
                secrets_by_source,_=resolved_credentials(pid,x_workbench_session,source_ids={data.source_id},include_model=False)
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
            return versioned(pid,workspace.get_config(pid, offline=True))

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

    @app.get('/api/projects/{pid}/model-limits')
    def model_limits(pid:str):
        from .model_limits import read_model_limits
        with workspace.lock(pid):
            return versioned(pid,read_model_limits(workspace,pid))

    @app.put('/api/projects/{pid}/model-limits')
    def update_model_limits(pid:str,data:dict,if_match:str|None=Header(default=None),include_config:bool=False):
        from .model_limits import save_model_limits
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid,if_match)
            limits=save_model_limits(workspace,pid,data)
            # The workbench needs the new versioned Agent reference alongside
            # the saved values, without a separate read racing another writer.
            value={'config':workspace.get_config(pid),'limits':limits} if include_config else limits
            return versioned(pid,value)

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
            sources,_=resolved_credentials(pid,x_workbench_session,include_model=False)
            options={'source_credentials':sources} if sources else {}
            return read_metadata(workspace,pid,refresh=True,**options)

    @app.get('/api/projects/{pid}/graph/versions')
    def graph_versions(pid: str):
        return graph_history.read_consistently(workspace, pid, graph_history.list_versions)

    @app.post('/api/projects/{pid}/graph/versions/{version}/migrate')
    def migrate_graph(pid: str, version: str, data: dict, if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            workspace.require_config_revision(pid, if_match)
            return graph_history.migrate_version(workspace, pid, version, data)

    def parse_business_context(value):
        from lightgraph.construction.contexts import normalize_context_selection
        if value is None:
            return None
        try:
            if len(value) > 8192:
                raise ValueError()
            return normalize_context_selection(json.loads(value))
        except (ValueError, TypeError):
            raise LightGraphError('business_context_invalid') from None

    @app.get('/api/projects/{pid}/graph/contexts')
    def graph_contexts(pid: str, version: str | None = None, offset: int = 0, limit: int = 200):
        return graph_history.read_consistently(workspace, pid, graph_history.read_contexts,
                                              version=version, offset=offset, limit=limit)

    @app.get('/api/projects/{pid}/graph')
    def graph(pid:str,layer:str='business',include_candidates:bool=False,offset:int=0,limit:int=240,q:str='',kind:str='',focus:str='',hops:int=1,traversal:str='all',version:str|None=None,business_context:str|None=None):
        if layer not in ('business','metadata'):raise LightGraphError('graph_layer_invalid')
        if hops not in (1,2) or traversal not in ('all','chains'):raise LightGraphError('graph_page_invalid')
        selection = dict(layer=layer, include_candidates=include_candidates, offset=offset, limit=limit,
                         q=q, kind=kind, focus=focus, hops=hops, traversal=traversal,
                         business_context=parse_business_context(business_context))
        if version is not None:
            return graph_history.read_consistently(workspace, pid, graph_history.read_page, version=version, **selection)
        reader = graph_history.read_consistently if layer == 'business' else read_graph_consistently
        return reader(workspace, pid, read_graph, **selection)

    @app.get('/api/projects/{pid}/graph/batches')
    def graph_batches(pid:str,layer:str='business',part:str='nodes',offset:int=0,limit:int=1000,
                      generation:str='',include_candidates:bool=False,focus:str='',hops:int=2,traversal:str='chains',version:str|None=None,business_context:str|None=None):
        from .graph_batches import read_graph_batch
        selection = dict(layer=layer, part=part, offset=offset, limit=limit, generation=generation,
                         include_candidates=include_candidates, focus=focus, hops=hops, traversal=traversal,
                         business_context=parse_business_context(business_context))
        if version is not None:
            return graph_history.read_consistently(workspace, pid, graph_history.read_batch, version=version, **selection)
        reader = graph_history.read_consistently if layer == 'business' else read_graph_consistently
        return reader(workspace, pid, read_graph_batch, **selection)

    @app.get('/api/projects/{pid}/graph/details')
    def graph_details(pid:str,entity_id:str|None=None,fact_id:str|None=None,include_candidates:bool=False,offset:int=0,version:str|None=None,business_context:str|None=None):
        selection = dict(entity_id=entity_id, fact_id=fact_id, include_candidates=include_candidates, offset=offset,
                         business_context=parse_business_context(business_context))
        if version is not None:
            return graph_history.read_consistently(workspace, pid, graph_history.read_details, version=version, **selection)
        return graph_history.read_consistently(workspace, pid, read_graph_details, **selection)

    @app.get('/api/projects/{pid}/graph/structure')
    def graph_structure(pid:str,draft_id:str|None=None,revision:int|None=None):
        from ..construction.structure_preview import load_structure_preview
        def read_structure(_workspace,_pid,*,config,**selection):
            return load_structure_preview(config,**selection)
        return read_graph_consistently(workspace,pid,read_structure,draft_id=draft_id,revision=revision)

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
    def start_run(pid:str,data:StartRun|None=None,x_workbench_session:str|None=Header(default=None),if_match:str|None=Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            sources,model=resolved_credentials(pid,x_workbench_session)
            workspace.require_config_revision(pid,if_match)
            options={}
            if sources:options['source_credentials']=sources
            if model is not None:options['model_api_key']=model
            if data is not None:options['run_mode']=data.run_mode
            return manager.start(pid,**options)

    @app.get('/api/projects/{pid}/runs/{rid}')
    def run_status(pid:str,rid:str):
        workspace.get(pid)
        return manager.get(pid,rid)

    @app.get('/api/projects/{pid}/workflow')
    def workflow_status(pid: str):
        workspace.get(pid)
        return manager.workflow_status(pid)

    @app.post('/api/projects/{pid}/runs/{rid}/resume', status_code=202)
    def resume_workflow(pid: str, rid: str, data: ResumeRun, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid, if_match)
            if data.action == 'publish_artifact':
                if not data.artifact_id or data.from_node_id:
                    raise LightGraphError('invalid_workflow_resume_request')
                return manager.resume(pid, rid, action=data.action, artifact_id=data.artifact_id)
            if not data.from_node_id or data.artifact_id:
                raise LightGraphError('invalid_workflow_resume_request')
            if data.action == 'recompose_legacy_merge':
                return manager.resume(pid, rid, action=data.action, from_node_id=data.from_node_id)
            from ..construction.workflow import resolve_workflow_plan
            from .execution import _workflow_descendants
            config = workspace.resolved_config(pid)
            if not config.workflow or not config.workflow.enabled:
                raise LightGraphError('workflow_not_enabled')
            rerun = _workflow_descendants(resolve_workflow_plan(config.workflow), data.from_node_id)
            workers = [u for u in config.workflow.units if u.unit_id in rerun]
            workers.extend(m for m in config.workflow.merge_nodes if m.node_id in rerun and m.merge_mode == 'merge_and_extract')
            sources, model = {}, None
            if workers:
                all_sources = [s.id for s in config.sources]
                needed = {sid for node in workers for sid in (node.source_ids or all_sources)}
                sources, model = resolved_credentials(pid, x_workbench_session, source_ids=needed)
            return manager.resume(pid, rid, from_node_id=data.from_node_id,
                                  source_credentials=sources, model_api_key=model)

    @app.post('/api/projects/{pid}/workflow/units/{unit_id}/retry', status_code=202)
    def retry_workflow_unit(pid: str, unit_id: str, x_workbench_session: str | None = Header(default=None), if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            sources, model = resolved_credentials(pid, x_workbench_session)
            workspace.require_config_revision(pid, if_match)
            options = {}
            if sources:
                options['source_credentials'] = sources
            if model is not None:
                options['model_api_key'] = model
            return manager.retry_unit(pid, unit_id, **options)

    @app.post('/api/projects/{pid}/workflow/merge')
    def merge_workflow(pid: str, if_match: str | None = Header(default=None)):
        with workspace.lock(pid):
            mutable(pid)
            workspace.require_config_revision(pid, if_match)
            return manager.merge_workflow(pid)

    @app.post('/api/projects/{pid}/retrieval/embedding/test')
    async def test_embedding(pid: str, data: TestEmbeddingRequest | None = None, x_workbench_session: str | None = Header(default=None)):
        from pydantic import ValidationError
        from ..runtime.config import EmbeddingConfig
        from ..retrieval import test_embedding_service
        with workspace.lock(pid):
            workspace.get(pid)
            config = workspace.resolved_config(pid).model_copy(deep=True)
            saved_binding = (config.embedding.base_url, config.embedding.model, config.embedding.api_key_env)
        if data is not None:
            overrides = {k: v for k, v in data.model_dump(exclude={'api_key'}).items() if v is not None}
            merged = {**config.embedding.model_dump(mode='json'), **overrides}
            try:
                config.embedding = EmbeddingConfig.model_validate(merged)
            except ValidationError:
                raise LightGraphError('invalid_embedding_config') from None
        # Test edits must not forward the saved credential to a new endpoint.
        binding = (config.embedding.base_url, config.embedding.model, config.embedding.api_key_env)
        saved_key = resolve_embedding_api_key(pid, x_workbench_session, config) if binding == saved_binding else None
        explicit_key = data.api_key.get_secret_value() if data is not None and data.api_key is not None else (saved_key if binding == saved_binding else '')
        return await run_in_threadpool(test_embedding_service, config, api_key=explicit_key)

    def schedule_vectorization(pid, token):
        from .construction_graph import project_retriever
        with workspace.lock(pid):
            mutable(pid)
            revision = workspace.get(pid)['config_revision']
            frozen = workspace.resolved_config(pid).model_copy(deep=True)
            key = resolve_embedding_api_key(pid, token, frozen)

            def build(_workspace, _pid, *, config):
                if config != frozen:
                    raise LightGraphError('graph_read_conflict')
                _, result = project_retriever(config, refresh=True, embedding_api_key=key)
                return result

            return retrieval_jobs.start(pid, revision, lambda: read_graph_consistently(workspace, pid, build))

    @app.post('/api/projects/{pid}/retrieval/jobs', status_code=202)
    def start_vectorization(pid: str, x_workbench_session: str | None = Header(default=None)):
        job, _ = schedule_vectorization(pid, x_workbench_session)
        return job

    @app.get('/api/projects/{pid}/retrieval/jobs')
    def vectorization_job(pid: str):
        with workspace.lock(pid):
            workspace.get(pid)
            return retrieval_jobs.status(pid)

    @app.post('/api/projects/{pid}/retrieval/sync')
    def sync_retrieval(pid: str, x_workbench_session: str | None = Header(default=None)):
        # Legacy synchronous clients join the same build as the workbench.
        _, future = schedule_vectorization(pid, x_workbench_session)
        return future.result()

    @app.get('/api/projects/{pid}/retrieval/status')
    def get_retrieval_status(pid: str, x_workbench_session: str | None = Header(default=None)):
        from .construction_graph import retrieval_status
        def read(_workspace, _pid, *, config):
            emb_key = resolve_embedding_api_key(pid, x_workbench_session, config)
            return retrieval_status(config, embedding_api_key=emb_key)
        return read_graph_consistently(workspace, pid, read)

    @app.post('/api/projects/{pid}/retrieval/query')
    async def query_retrieval(pid: str, data: RetrieveSubgraphRequest, x_workbench_session: str | None = Header(default=None)):
        from .construction_graph import project_retriever
        def read(_workspace, _pid, *, config):
            emb_key = resolve_embedding_api_key(pid, x_workbench_session, config)
            retriever, _ = project_retriever(config, embedding_api_key=emb_key)
            return retriever.retrieve(
                data.query,
                business_context=data.business_context,
                scopes=list(config.scopes),
                top_n=data.top_n,
                top_p_seeds=data.top_p_seeds,
                hops=data.hops,
                max_per_rel=data.max_per_rel,
                hub_degree_limit=data.hub_degree_limit,
                max_nodes=data.max_nodes,
                max_edges=data.max_edges,
            )
        return await run_in_threadpool(read_graph_consistently, workspace, pid, read)

    @app.post('/api/projects/{pid}/runs/{rid}/cancel')
    def cancel_run(pid:str,rid:str):
        workspace.get(pid)
        return manager.cancel(pid,rid)

    @app.get('/api/projects/{pid}/runs/{rid}/trace')
    def run_trace(pid:str,rid:str,offset:int=0,limit:int=10,view:str='all'):
        from .run_traces import read_run_trace
        with workspace.lock(pid):
            return read_run_trace(manager,pid,rid,offset=offset,limit=limit,view=view)

    @app.get('/api/projects/{pid}/runs/{rid}/worker-diagnostics')
    def worker_diagnostics(pid:str,rid:str):
        from .worker_diagnostics import read_worker_diagnostics
        with workspace.lock(pid):
            return read_worker_diagnostics(manager,pid,rid)

    @app.get('/api/projects/{pid}/runs/{rid}/worker-diagnostics/export')
    def worker_diagnostics_export(pid:str,rid:str):
        from .worker_diagnostics import export_worker_diagnostics
        with workspace.lock(pid):
            content=export_worker_diagnostics(manager,pid,rid)
        return Response(content,media_type='application/x-ndjson',headers={
            'Content-Disposition':f'attachment; filename="{rid}.worker.jsonl"',
            'Cache-Control':'no-store'})

    @app.get('/api/projects/{pid}/runs/{rid}/trace/export')
    def run_trace_export(pid:str,rid:str):
        from .run_traces import export_run_trace, TraceResponse
        with workspace.lock(pid):
            stream=export_run_trace(manager,pid,rid)
        return TraceResponse(stream,media_type='application/x-ndjson',headers={
            'Content-Disposition':f'attachment; filename="{rid}.trace.jsonl"',
            'Cache-Control':'no-store'})

    @app.delete('/api/projects/{pid}/runs/{rid}/trace')
    def delete_run_trace(pid:str,rid:str,if_match:str|None=Header(default=None)):
        from .run_traces import clear_run_trace
        with workspace.lock(pid):
            workspace.require_config_revision(pid,if_match)
            return clear_run_trace(manager,pid,rid)

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

"""Vue workbench API over project-scoped files and the existing graph engine.

Uploads are user-provided project assets, not cached database query results.
There is no raw-query endpoint; the discovery Agent keeps its internal SQL guard.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from lightgraph.errors import LightGraphError
from .workspace import Workspace
from .execution import RunManager, read_graph, read_metadata

ROOT=Path(__file__).resolve().parents[3]
MAX_REQUEST_BYTES=21*1024*1024
ALLOWED_ORIGINS={f'http://{host}:{port}' for host in ('127.0.0.1','localhost') for port in (8000,5173,4173)}


def error_response(code: str, status: int=400) -> JSONResponse:
    return JSONResponse({'error':{'code':code}},status_code=status)


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
            if message['type']=='http.response.start':started=True
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


def create_app(workspace_root: str|Path|None=None, *, frontend_dir: str|Path|None=None,
               manager_factory=RunManager) -> FastAPI:
    workspace=Workspace(Path(workspace_root) if workspace_root else ROOT/'workspace')
    manager=manager_factory(workspace)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await run_in_threadpool(manager.shutdown)

    app=FastAPI(title='Data Graph Workbench',version='1.1.0',lifespan=lifespan)
    app.state.workspace=workspace
    app.state.manager=manager
    app.add_middleware(CORSMiddleware,allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=['GET','POST','PUT','PATCH','OPTIONS'],allow_headers=['Content-Type'],allow_credentials=False)
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

    @app.post('/api/projects/{pid}/metadata/refresh')
    def refresh_metadata(pid:str):
        with workspace.lock(pid):
            mutable(pid)
            return read_metadata(workspace,pid,refresh=True)

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
    def start_run(pid:str):
        with workspace.lock(pid):
            mutable(pid)
            return manager.start(pid)

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

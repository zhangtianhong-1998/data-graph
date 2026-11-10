"""HTTP/stdio MCP bridge exposing only graph construction's seven domain tools."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from typing import Literal

from mcp.server.fastmcp import FastMCP

from lightgraph.runtime.config import load_config
from lightgraph.runtime.locking import run_lock
from .tools import ConstructionTools


def create_server(tools):
    """Register compact schemas; source operation details are read on demand."""
    import anyio
    import threading
    from functools import partial
    # The workspace is the durable session. HTTP requests must not depend on an
    # in-memory MCP session ID surviving a network reconnect.
    # Send response headers immediately and let MCP's native SSE heartbeat keep
    # long validations alive. A deferred JSON response leaves Node fetch waiting
    # for headers and can hit its 300-second limit before our tool deadline.
    # Heartbeats carry no tool data and do not extend the absolute run budget.
    server = FastMCP("lightgraph-construction", stateless_http=True, json_response=False)
    writer = anyio.Lock()
    closing = threading.Event()
    state_lock = threading.Lock()
    pending = 0

    def shutdown():
        with state_lock:
            closing.set()
            needs_cancel = pending > 0
        # Cooperatively stop SQL and CPU projection work. The owner must wait
        # for this server before closing adapters or releasing its writer lock.
        if needs_cancel and hasattr(tools, 'request_shutdown'):
            tools.request_shutdown()

    server.construction_shutdown = shutdown

    async def dispatch(tool_name, **arguments):
        nonlocal pending
        # Validation is synchronous and can take minutes. Keep the HTTP event
        # loop responsive, but retain one writer and wait for work to settle
        # even if the caller disconnects. Cancellation never starts a replay.
        with state_lock:
            if closing.is_set():
                return {'status': 'error', 'code': 'construction_cancelled', 'retryable': False}
            pending += 1
        try:
            async with writer:
                if closing.is_set():
                    return {'status': 'error', 'code': 'construction_cancelled', 'retryable': False}
                with anyio.CancelScope(shield=True):
                    # Domain execution keeps its complete result. The model
                    # receives a bounded view with an immutable material ref;
                    # reading that material never reruns a source query.
                    call = getattr(tools, 'bounded_call', tools.call)
                    return await anyio.to_thread.run_sync(partial(call, tool_name, **arguments),
                                                         abandon_on_cancel=False)
        finally:
            with state_lock:
                pending -= 1

    @server.tool()
    async def describe_sources() -> dict:
        """Read the authorized physical catalog, user goal, document index and saved work."""
        return await dispatch("describe_sources")

    @server.tool()
    async def inspect_source(table: str, operation: str, arguments: dict | None = None) -> dict:
        """Probe data read-only. Get operation arguments from read_material(source_operations)."""
        return await dispatch("inspect_source", table=table, operation=operation, arguments=arguments)

    @server.tool()
    async def read_material(name: str, page: int = 0, section: str = "") -> dict:
        """Read Markdown goal, skill, reference:<name.md>, draft_schema, source_operations,
        document:<id>, evidence:<ref>, draft:<id>, validation:<id>, conflicts:<id>
        or publication_conflicts. For a retained tool result use result:<id>,
        optionally selecting a section with a JSON pointer, such as /data/rows.
        Read only needed sections/pages; follow next_page if more is necessary.
        Result materials and conflicts require no new database query. A page is
        a fragment, not proof that all rows or all material have been read.
        """
        return await dispatch("read_material", name=name, page=page,
                              **({'section': section} if section else {}))

    @server.tool()
    async def save_graph_draft(yaml_text: str) -> dict:
        """Save or revise a YAML candidate with discovered mappings and issued evidence refs."""
        return await dispatch("save_graph_draft", yaml_text=yaml_text)

    @server.tool()
    async def validate_graph_draft(draft_id: str, mode: Literal['instances', 'structure'] | None = None) -> dict:
        """Check structure without querying facts, or validate complete mapped instances.
        Omitted mode uses the project's construction_target. Every attempt retains
        its own YAML artifact. Name quality advice is non-blocking.
        """
        return await dispatch("validate_graph_draft", draft_id=draft_id, mode=mode)

    @server.tool()
    async def read_workspace(draft_id: str | None = None) -> dict:
        """Recover saved drafts, revisions, checks and unresolved items without replaying raw rows."""
        return await dispatch("read_workspace", draft_id=draft_id)

    @server.tool()
    async def publish_graph_draft(draft_id: str, validation_id: str | None = None,
                            mode: Literal['instances', 'structure'] | None = None) -> dict:
        """Publish a checked immutable artifact without rereading database facts.
        Omitted mode follows construction_target. Structure publication contains
        definitions and bindings, not verified instances. validation_id selects
        an applicable successful instance check explicitly when needed.
        """
        return await dispatch("publish_graph_draft", draft_id=draft_id, validation_id=validation_id, mode=mode)

    return server


@contextmanager
def serve_local_tools(tools):
    """Expose this worker's adapters to Harness without copying source secrets.

    A random bearer token protects the loopback-only endpoint. Credentials stay
    in the owning worker; the runtime receives only this ephemeral token.
    """
    import hmac
    import secrets
    import socket
    import threading
    import time
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    token = secrets.token_urlsafe(32)
    domain = create_server(tools)
    app = domain.streamable_http_app()

    class LocalAuthorization(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            value = request.headers.get("authorization", "")
            if not hmac.compare_digest(value, "Bearer " + token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(LocalAuthorization)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    address = listener.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("construction_mcp_start_failed")
            time.sleep(.02)
        yield f"http://{address[0]}:{address[1]}/mcp", token
    finally:
        domain.construction_shutdown()
        server.should_exit = True
        # Do not return while a shielded validation still holds the workspace
        # or adapters. Their caller closes those resources immediately next.
        thread.join()
        listener.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    # This child owns the domain workspace writer. Harness session logs have a
    # separate directory and do not acquire this lock.
    with run_lock(args.workspace):
        tools = ConstructionTools(load_config(args.config), args.workspace)
        try:
            create_server(tools).run(transport="stdio")
        finally:
            tools.close()


if __name__ == "__main__":
    main()

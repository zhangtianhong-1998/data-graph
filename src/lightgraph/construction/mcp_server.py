"""Stdio MCP bridge exposing only graph construction's six domain tools."""
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
    server = FastMCP("lightgraph-construction")

    @server.tool()
    def describe_sources() -> dict:
        """Read the authorized physical catalog, user goal, document index and saved work."""
        return tools.call("describe_sources")

    @server.tool()
    def inspect_source(table: str, operation: str, arguments: dict | None = None) -> dict:
        """Probe data read-only. Get operation arguments from read_material(source_operations)."""
        return tools.call("inspect_source", table=table, operation=operation, arguments=arguments)

    @server.tool()
    def read_material(name: str, page: int = 0) -> dict:
        """Read Markdown goal, skill, reference:<name.md>, draft_schema, source_operations,
        document:<id>, or evidence:<ref>. Follow next_page for long documents.
        """
        return tools.call("read_material", name=name, page=page)

    @server.tool()
    def save_graph_draft(yaml_text: str) -> dict:
        """Save or revise a YAML candidate with discovered mappings and issued evidence refs."""
        return tools.call("save_graph_draft", yaml_text=yaml_text)

    @server.tool()
    def validate_graph_draft(draft_id: str, mode: Literal['instances', 'structure'] | None = None) -> dict:
        """Check structure without querying facts, or validate complete mapped instances.
        Omitted mode uses the project's construction_target. Every attempt retains
        its own YAML artifact. Name quality advice is non-blocking.
        """
        return tools.call("validate_graph_draft", draft_id=draft_id, mode=mode)

    @server.tool()
    def read_workspace(draft_id: str | None = None) -> dict:
        """Recover saved drafts, revisions, checks and unresolved items without replaying raw rows."""
        return tools.call("read_workspace", draft_id=draft_id)

    @server.tool()
    def publish_graph_draft(draft_id: str, validation_id: str | None = None,
                            mode: Literal['instances', 'structure'] | None = None) -> dict:
        """Publish a checked immutable artifact without rereading database facts.
        Omitted mode follows construction_target. Structure publication contains
        definitions and bindings, not verified instances. validation_id selects
        an applicable successful instance check explicitly when needed.
        """
        return tools.call("publish_graph_draft", draft_id=draft_id, validation_id=validation_id, mode=mode)

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
    app = create_server(tools).streamable_http_app()

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
        server.should_exit = True
        thread.join(timeout=5)
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

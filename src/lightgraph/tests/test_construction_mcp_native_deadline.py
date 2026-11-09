"""Opt-in native MCP deadlines remain finite while long tools send SSE pings."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from lightgraph.construction.mcp_server import serve_local_tools


pytestmark = pytest.mark.skipif(
    os.environ.get('LIGHTGRAPH_TEST_HARNESS') != '1',
    reason='native MCP regression requires LIGHTGRAPH_TEST_HARNESS=1 and Harness bootstrap',
)


def test_native_mcp_deadline_survives_heartbeats_and_waits_for_writer_cleanup(monkeypatch):
    from sse_starlette.sse import EventSourceResponse

    client_package = (Path(__file__).resolve().parents[3] / 'vendor' / 'deepseek-harness' /
                      'packages' / 'mcp' / 'mcp-client')
    node = shutil.which('node')
    dependency = client_package / 'node_modules' / '@modelcontextprotocol' / 'client' / 'package.json'
    if node is None or not dependency.is_file():
        pytest.skip('run scripts/bootstrap_harness.py to install the native MCP client')
    assert json.loads(dependency.read_text(encoding='utf-8'))['version'] == '2.0.0'

    # Shorten the heartbeat interval, not the real SDK deadline implementation.
    # Observe bytes sent by the native response without replacing Node fetch.
    monkeypatch.setattr(EventSourceResponse, 'DEFAULT_PING_INTERVAL', .05)
    native_ping = EventSourceResponse._ping
    started, stop, finished = threading.Event(), threading.Event(), threading.Event()
    calls, events, pings = [], [], []

    async def observed_ping(self, send):
        async def observe(message):
            await send(message)
            if started.is_set() and message.get('body', b'').startswith(b': ping'):
                pings.append(time.monotonic())
        await native_ping(self, observe)

    monkeypatch.setattr(EventSourceResponse, '_ping', observed_ping)

    class Tools:
        def request_shutdown(self):
            events.append('shutdown_requested')
            stop.set()

        def call(self, name, **arguments):
            calls.append(name)
            events.append('validation_started')
            started.set()
            assert stop.wait(10), 'test owner did not cancel the outstanding validation'
            time.sleep(.15)  # Stand in for rollback and atomic artifact cleanup.
            events.append('validation_settled')
            finished.set()
            return {'status': 'error', 'code': 'construction_cancelled'}

    script = r'''
const { Client, StreamableHTTPClientTransport } = require('@modelcontextprotocol/client');
const client = new Client({ name: 'lightgraph-deadline-test', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(process.env.GRAPH_TEST_URL), {
  requestInit: { headers: { Authorization: `Bearer ${process.env.GRAPH_TEST_TOKEN}` } },
});
async function main() {
  await client.connect(transport, { timeout: 3000 });
  const started = performance.now();
  let result;
  try {
    await client.callTool({ name: 'validate_graph_draft', arguments: { draft_id: 'deadline-test' } },
      { timeout: 500 });
    result = { status: 'unexpected_success', elapsed_ms: performance.now() - started };
  } catch (error) {
    result = { status: 'rejected', code: error.code, elapsed_ms: performance.now() - started };
  } finally {
    await client.close();
  }
  process.stdout.write(JSON.stringify(result) + '\n');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
'''

    with serve_local_tools(Tools()) as (url, token):
        result = subprocess.run(
            [node, '-e', script], cwd=client_package,
            env={**os.environ, 'GRAPH_TEST_URL': url, 'GRAPH_TEST_TOKEN': token},
            capture_output=True, text=True, timeout=8, check=True,
        )
        outcome = json.loads(result.stdout)
        assert outcome['status'] == 'rejected'
        assert outcome['code'] == 'REQUEST_TIMEOUT'
        assert 450 <= outcome['elapsed_ms'] < 3000
        assert len(pings) >= 3, 'the SDK deadline must expire despite ongoing SSE heartbeats'
        assert started.is_set() and not finished.is_set()
        assert calls == ['validate_graph_draft']
        # A client deadline/close must neither replay the tool nor abandon its writer.
        assert events == ['validation_started']

    events.append('owner_closed_sources')
    assert finished.is_set()
    assert calls == ['validate_graph_draft']
    assert events == ['validation_started', 'shutdown_requested',
                      'validation_settled', 'owner_closed_sources']

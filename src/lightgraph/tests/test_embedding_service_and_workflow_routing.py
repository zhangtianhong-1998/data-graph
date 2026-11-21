"""Tests for replaceable OpenAI-compatible embedding service, local Qwen3 server app, and workflow KB/table routing."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import numpy as np
import pytest

from lightgraph.construction.workflow import build_unit_config
from lightgraph.contracts import ExtractionGoal, QueryLimits
from lightgraph.errors import LightGraphError
from lightgraph.retrieval.local_embed_server import create_embedding_app
from lightgraph.retrieval.vector_local import OpenAIEmbedder, embedder_fingerprint, resolve_embedder
from lightgraph.runtime.config import (
    EmbeddingConfig,
    ModelConfig,
    RunConfig,
    SourceConfig,
    WorkflowConfig,
    WorkflowEdgeConfig,
    WorkflowMergeNodeConfig,
    WorkflowUnitConfig,
)
from lightgraph.web.app import create_app


class _FakeSemanticBackend:
    """Deterministic 1024-dim local backend mimicking Qwen3-Embedding-0.6B behavior."""

    model_name = "Qwen/Qwen3-Embedding-0.6B"
    dim = 1024

    def __init__(self, *, dim: int = 1024, model_name: str = "Qwen/Qwen3-Embedding-0.6B"):
        self.dim = dim
        self.model_name = model_name
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self.dim, dtype=np.float32)
            # Semantic hints + deterministic character features
            lower = text.casefold()
            if "结算" in lower or "settlement" in lower:
                vec[0] += 4.0
            if "合同" in lower or "contract" in lower:
                vec[1] += 4.0
            if "项目" in lower or "project" in lower:
                vec[2] += 4.0
            for ch in lower:
                h = int.from_bytes(hashlib.blake2b(ch.encode("utf-8"), digest_size=4).digest(), "little")
                vec[16 + (h % (self.dim - 16))] += 1.0
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
            out.append(vec.tolist())
        return out


class _LiveEmbedHTTPServer:
    """Real loopback HTTP server wrapping the FastAPI local_embed_server routes."""

    def __init__(self, backend: _FakeSemanticBackend):
        self.backend = backend
        self.fastapi_app = create_embedding_app(backend)
        self.client = TestClient(self.fastapi_app)
        self.seen_auth: list[str] = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                resp = outer.client.get(self.path)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp.content)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.seen_auth.append(self.headers.get("Authorization", ""))
                resp = outer.client.post(
                    self.path,
                    content=body,
                    headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                )
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp.content)

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_local_embed_server_app_supports_matryoshka_and_models():
    backend = _FakeSemanticBackend(dim=1024, model_name="Qwen/Qwen3-Embedding-0.6B")
    client = TestClient(create_embedding_app(backend))

    assert client.get("/health").json() == {"status": "ok", "model": "Qwen/Qwen3-Embedding-0.6B", "dimensions": 1024}
    assert client.get("/v1/models").json()["data"][0]["id"] == "Qwen/Qwen3-Embedding-0.6B"

    full = client.post("/v1/embeddings", json={"input": ["合同", "结算单"]})
    assert full.status_code == 200
    assert len(full.json()["data"][0]["embedding"]) == 1024

    trunc = client.post("/v1/embeddings", json={"input": "合同", "dimensions": 512})
    assert trunc.status_code == 200
    vec = np.asarray(trunc.json()["data"][0]["embedding"], dtype=np.float32)
    assert vec.shape == (512,)
    assert pytest.approx(float(np.linalg.norm(vec)), abs=1e-4) == 1.0


def test_openai_embedder_auto_dim_batching_query_instruction_and_safety():
    backend = _FakeSemanticBackend(dim=1024)
    srv = _LiveEmbedHTTPServer(backend)
    try:
        embedder = OpenAIEmbedder(
            base_url=srv.base_url,
            model="Qwen/Qwen3-Embedding-0.6B",
            api_key="secret-token-xyz",
            batch_size=2,
            query_instruction="Retrieve relevant enterprise entities and tables",
        )
        assert embedder.dim is None
        doc_vecs = embedder.embed(["表A", "表B", "表C", "表D", "表E"])
        assert doc_vecs.shape == (5, 1024)
        assert embedder.dim == 1024
        # Batches of 2, 2, 1; document cards must NOT receive query_instruction prefix
        assert [len(c) for c in backend.calls] == [2, 2, 1]
        assert all(not text.startswith("Instruct:") for call in backend.calls for text in call)

        q_vecs = embedder.embed_queries(["查结算单"])
        assert q_vecs.shape == (1, 1024)
        assert backend.calls[-1][0] == "Instruct: Retrieve relevant enterprise entities and tables\nQuery: 查结算单"
    finally:
        srv.close()

    # Unreachable endpoint raises safe error code without leaking secret
    with pytest.raises(LightGraphError) as exc:
        embedder.embed(["after close"])
    assert exc.value.code == "embedding_service_unavailable"
    assert "secret-token-xyz" not in repr(exc.value)

    # Non-loopback plain HTTP must fail unless allow_insecure_http=True
    cfg = RunConfig(
        goal=ExtractionGoal(purpose="t"),
        sources=[SourceConfig(id="s1", kind="excel_workbook", path="a.xlsx")],
        embedding=EmbeddingConfig(
            provider="openai_compatible",
            base_url="http://10.0.0.5:8002/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        ),
    )
    with pytest.raises(LightGraphError) as insecure_exc:
        resolve_embedder(cfg)
    assert insecure_exc.value.code == "embedding_insecure_endpoint"


def test_embedding_timeout_is_applied_to_each_request(monkeypatch):
    import httpx

    timeouts = []
    def slow_service(_url, **kwargs):
        timeouts.append(kwargs['timeout'])
        raise httpx.ReadTimeout('read timed out')

    monkeypatch.setattr(httpx, 'post', slow_service)
    for settings in ({}, {'timeout_seconds': 37}):
        embedder = OpenAIEmbedder(base_url='http://127.0.0.1/v1', model='test', **settings)
        with pytest.raises(LightGraphError, match='embedding_timeout'):
            embedder.embed(['test'])
    assert timeouts == [EmbeddingConfig().timeout_seconds, 37] == [120, 37]


def test_web_embedding_config_credentials_test_and_retrieval_sync_query(tmp_path):
    backend_a = _FakeSemanticBackend(dim=1024, model_name="Qwen/Qwen3-Embedding-0.6B")
    srv_a = _LiveEmbedHTTPServer(backend_a)
    backend_b = _FakeSemanticBackend(dim=512, model_name="bge-m3-small")
    srv_b = _LiveEmbedHTTPServer(backend_b)
    try:
        app = create_app(tmp_path / "ws")
        client = TestClient(app)
        pid = client.post("/api/projects", json={"name": "向量测试项目"}).json()["project_id"]
        session = client.post(f"/api/projects/{pid}/session").json()["session_id"]

        # Upload sample CSV + save config pointing to srv_a
        r_cfg = client.get(f"/api/projects/{pid}/config")
        etag = r_cfg.headers["ETag"]
        r_up = client.post(
            f"/api/projects/{pid}/files",
            files={"file": ("orders.csv", b"id,name\n1,a\n", "text/csv")},
            headers={"If-Match": etag},
        )
        assert r_up.status_code == 201
        etag = r_up.headers["ETag"]

        # Default credentials endpoint shape remains strictly backward-compatible
        assert client.get(f"/api/projects/{pid}/credentials", headers={"X-Workbench-Session": session}).json() == {
            "database_sources": [],
            "model": False,
        }

        cfg_res = client.get(f"/api/projects/{pid}/config")
        cfg = cfg_res.json()
        etag = cfg_res.headers["ETag"]
        cfg["embedding"] = {
            "provider": "openai_compatible",
            "base_url": srv_a.base_url,
            "model": "Qwen/Qwen3-Embedding-0.6B",
            "batch_size": 4,
            "query_instruction": "根据业务问题匹配实体与关系",
        }
        r_save = client.put(f"/api/projects/{pid}/config", json=cfg, headers={"If-Match": etag})
        assert r_save.status_code == 200
        etag = r_save.headers["ETag"]

        # Save encrypted embedding key bound to (base_url, model)
        r_key = client.put(
            f"/api/projects/{pid}/credentials/embedding",
            json={
                "api_key": "emb-secret-123",
                "expected_embedding": {"base_url": srv_a.base_url, "model": "Qwen/Qwen3-Embedding-0.6B"},
            },
            headers={"X-Workbench-Session": session, "If-Match": etag},
        )
        assert r_key.status_code == 200
        assert r_key.json() == {"embedding": True}

        # Conflict check if expected_embedding differs
        r_conflict = client.put(
            f"/api/projects/{pid}/credentials/embedding",
            json={
                "api_key": "emb-secret-123",
                "expected_embedding": {"base_url": srv_a.base_url, "model": "other-model"},
            },
            headers={"X-Workbench-Session": session, "If-Match": etag},
        )
        assert r_conflict.status_code == 409

        # Live test endpoint detects 1024 dimensions from srv_a and passes saved key
        r_test = client.post(
            f"/api/projects/{pid}/retrieval/embedding/test",
            json={},
            headers={"X-Workbench-Session": session},
        )
        assert r_test.status_code == 200
        assert r_test.json()["dimensions"] == 1024
        assert r_test.json()["model"] == "Qwen/Qwen3-Embedding-0.6B"
        assert "Bearer emb-secret-123" in srv_a.seen_auth

        # Write a small published construction graph so sync + query indexes real nodes
        # First, verify /retrieval/status reports 'no_graph' before any graph exists.
        st0 = client.get(f"/api/projects/{pid}/retrieval/status", headers={"X-Workbench-Session": session})
        assert st0.status_code == 200
        assert st0.json()["status"] == "no_graph"
        assert st0.json()["vectorized"] is False

        from lightgraph.construction.access import access_fingerprint
        from lightgraph.construction.merger import write_sharded_graph

        resolved = app.state.workspace.resolved_config(pid)
        out_dir = Path(resolved.output_dir) / "construction"
        acc = access_fingerprint(resolved)
        write_sharded_graph(
            out_dir,
            {
                "support_visibility_version": 1,
                "access_fingerprint": acc,
                "definitions": {
                    "entities": [{"type": "Contract", "description": "合同实体"}],
                    "relations": [{"type": "SETTLED_BY", "description": "关联结算单"}],
                },
                "nodes": [
                    {
                        "id": "n1",
                        "type": "Contract",
                        "label": "华东设备采购合同",
                        "aliases": ["采购合同"],
                        "english_names": ["east_contract"],
                        "identity": {"contract_code": "HT-001"},
                        "properties": {},
                        "supports": [{"scope_ref": "public", "provenance": {"source": {"source_id": "s1"}}}],
                        "provenance": [{"source": {"source_id": "s1"}}],
                    },
                    {
                        "id": "n2",
                        "type": "Contract",
                        "label": "结算付款单JS-99",
                        "aliases": [],
                        "english_names": ["settlement_99"],
                        "identity": {"settle_code": "JS-99"},
                        "properties": {},
                        "supports": [{"scope_ref": "public", "provenance": {"source": {"source_id": "s1"}}}],
                        "provenance": [{"source": {"source_id": "s1"}}],
                    },
                ],
                "edges": [
                    {
                        "id": "e1",
                        "type": "SETTLED_BY",
                        "source": "n1",
                        "target": "n2",
                        "properties": {},
                        "supports": [{"scope_ref": "public", "provenance": {"source": {"source_id": "s1"}}}],
                        "provenance": [{"source": {"source_id": "s1"}}],
                    }
                ],
            },
            context_fingerprint="ctx-1",
        )

        # Immediately after extraction/publication, graph is NOT vectorized yet!
        st1 = client.get(f"/api/projects/{pid}/retrieval/status", headers={"X-Workbench-Session": session})
        assert st1.status_code == 200
        assert st1.json()["status"] == "not_vectorized"
        assert st1.json()["vectorized"] is False
        assert st1.json()["graph_nodes"] == 2
        assert st1.json()["configured_model"] == "Qwen/Qwen3-Embedding-0.6B"

        r_sync = client.post(f"/api/projects/{pid}/retrieval/sync", headers={"X-Workbench-Session": session})
        assert r_sync.status_code == 200, r_sync.text
        assert r_sync.json()["vector_dim"] == 1024
        assert r_sync.json()["embedder_model"] == "Qwen/Qwen3-Embedding-0.6B"
        assert r_sync.json()["vectorized"] is True

        # Now status reports 'vectorized' with 1024 dims & timestamp!
        st2 = client.get(f"/api/projects/{pid}/retrieval/status", headers={"X-Workbench-Session": session})
        assert st2.status_code == 200
        assert st2.json()["status"] == "vectorized"
        assert st2.json()["vectorized"] is True
        assert st2.json()["vector_dim"] == 1024
        assert st2.json()["synced_at"]

        r_query = client.post(
            f"/api/projects/{pid}/retrieval/query",
            json={"query": "华东设备采购合同 与 结算付款单", "hops": 2, "top_p_seeds": 2},
            headers={"X-Workbench-Session": session},
        )
        assert r_query.status_code == 200, r_query.text
        q_body = r_query.json()
        assert len(q_body["nodes"]) == 2
        assert len(q_body["edges"]) == 1

        # Now replace the local embedding service with srv_b (512-dim model) via config only!
        cfg_res2 = client.get(f"/api/projects/{pid}/config")
        cfg2 = cfg_res2.json()
        etag2 = cfg_res2.headers["ETag"]
        cfg2["embedding"] = {
            "provider": "openai_compatible",
            "base_url": srv_b.base_url,
            "model": "bge-m3-small",
            "batch_size": 4,
            "query_instruction": "",
        }
        r_save2 = client.put(f"/api/projects/{pid}/config", json=cfg2, headers={"If-Match": etag2})
        assert r_save2.status_code == 200

        # Status immediately detects embedder change and reports 'stale'!
        st3 = client.get(f"/api/projects/{pid}/retrieval/status", headers={"X-Workbench-Session": session})
        assert st3.status_code == 200
        assert st3.json()["status"] == "stale"
        assert st3.json()["stale_reason"] == "embedder_changed"

        # A different model needs explicit user consent to build a new index.
        params = {"query": "华东设备采购合同", "hops": 1, "top_p_seeds": 2}
        headers = {"X-Workbench-Session": session}
        denied = client.post(f"/api/projects/{pid}/retrieval/query", json=params, headers=headers)
        assert denied.json()["error"]["code"] == "retrieval_index_stale"
        assert not backend_b.calls
        jobs_url = f"/api/projects/{pid}/retrieval/jobs"
        started = client.post(jobs_url, headers=headers)
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 10
        while (job := client.get(jobs_url).json())["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert job["job_id"] == started.json()["job_id"] and job["status"] == "completed", job
        r_query2 = client.post(f"/api/projects/{pid}/retrieval/query", json=params, headers=headers)
        assert r_query2.status_code == 200, r_query2.text
        assert len(backend_b.calls) >= 2
        manifest = json.loads((Path(resolved.output_dir) / "retrieval" / "current.json").read_text("utf-8"))
        assert manifest["vector_dim"] == 512
        assert manifest["embedder"] == embedder_fingerprint(
            OpenAIEmbedder(base_url=srv_b.base_url, model="bge-m3-small")
        )
        st4 = client.get(f"/api/projects/{pid}/retrieval/status", headers={"X-Workbench-Session": session})
        assert st4.json()["status"] == "vectorized"
        assert st4.json()["vector_dim"] == 512
    finally:
        srv_a.close()
        srv_b.close()


def test_workflow_unit_routing_matches_tables_and_workspace_documents_precisely(tmp_path):
    app = create_app(tmp_path / "ws")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "并行文档路由"}).json()["project_id"]

    etag = client.get(f"/api/projects/{pid}/config").headers["ETag"]
    doc1 = client.post(
        f"/api/projects/{pid}/files",
        files={"file": ("guide.md", b"# Guide A\n", "text/markdown")},
        headers={"If-Match": etag},
    ).json()
    etag = client.get(f"/api/projects/{pid}/config").headers["ETag"]
    doc2 = client.post(
        f"/api/projects/{pid}/files",
        files={"file": ("sub_guide.md", b"# Sub Guide\n", "text/markdown")},
        headers={"If-Match": etag},
    ).json()
    etag = client.get(f"/api/projects/{pid}/config").headers["ETag"]
    doc3 = client.post(
        f"/api/projects/{pid}/files",
        files={"file": ("rules.md", b"# Rules B\n", "text/markdown")},
        headers={"If-Match": etag},
    ).json()

    # Build a synthetic multi-table config matching workspace files + two units: A+B w/ guide.md, A+C w/ rules.md
    resolved = app.state.workspace.resolved_config(pid)
    resolved.sources = [
        SourceConfig(
            id="db1",
            kind="gauss_database",
            dsn_env="TEST_DB_DSN",
            allowed_tables=["dm.table_a", "dm.table_b", "dm.table_c"],
        )
    ]
    resolved.workflow = WorkflowConfig(
        enabled=True,
        max_parallel_workers=2,
        units=[
            WorkflowUnitConfig(
                unit_id="unit_ab",
                label="链路一：A表+B表",
                source_ids=["db1"],
                source_table_overrides={"db1": ["dm.table_a", "dm.table_b"]},
                document_ids=["guide.md"],  # Must match guide.md and NOT sub_guide.md!
            ),
            WorkflowUnitConfig(
                unit_id="unit_ac",
                label="链路二：A表+C表",
                source_ids=["db1"],
                source_table_overrides={"db1": ["dm.table_a", "dm.table_c"]},
                document_ids=[doc3["file_id"]],  # Match by file_id!
            ),
        ],
    )

    cfg_ab = build_unit_config(resolved, resolved.workflow.units[0], unit_output_dir=tmp_path / "u_ab")
    cfg_ac = build_unit_config(resolved, resolved.workflow.units[1], unit_output_dir=tmp_path / "u_ac")

    assert cfg_ab.sources[0].allowed_tables == ["dm.table_a", "dm.table_b"]
    assert [Path(d).name for d in cfg_ab.documents] == [Path(doc1["relative_path"]).name]
    assert "链路一：A表+B表" in cfg_ab.goal_markdown

    assert cfg_ac.sources[0].allowed_tables == ["dm.table_a", "dm.table_c"]
    assert [Path(d).name for d in cfg_ac.documents] == [Path(doc3["relative_path"]).name]
    assert doc2["file_id"] not in "".join(cfg_ab.documents + cfg_ac.documents)


def test_workflow_dag_validation_direct_extract_and_merge_and_extract_execution(tmp_path, monkeypatch):
    import sys
    import time
    import lightgraph.web.execution as execution_mod

    # 1. Cycle validation rejects cyclic DAG wires
    with pytest.raises(ValueError, match="workflow_cycle_detected"):
        WorkflowConfig(
            enabled=True,
            units=[WorkflowUnitConfig(unit_id="u1")],
            merge_nodes=[
                WorkflowMergeNodeConfig(node_id="m1"),
                WorkflowMergeNodeConfig(node_id="m2"),
            ],
            edges=[
                WorkflowEdgeConfig(edge_id="e1", source="m1", target="m2"),
                WorkflowEdgeConfig(edge_id="e2", source="m2", target="m1"),
            ],
        )

    # 2. End-to-end execution:
    #    (a) Extract-only DAG (no merge node) -> direct_output
    #    (b) Two parallel extract nodes (u1, u2) -> merge_and_extract node (m_full) -> output
    worker_script = r'''
import json, sys
from pathlib import Path
from lightgraph.contracts import ColumnSpec, TableSpec, RunReport
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource

result_path = Path(sys.argv[1])
payload = json.load(sys.stdin)
cfg = RunConfig.model_validate(payload['config'])
out = Path(cfg.output_dir)
node_name = out.name

if node_name == 'u2' and (out.parents[2] / 'force_u2_failure').exists():
    result_path.write_text(json.dumps({'status': 'failed', 'cleanup_status': 'completed', 'error_code': 'test_upstream_failed'}))
    sys.exit(1)
if node_name == 'm_full' and (out.parents[2] / 'pause_merge').exists():
    import time
    for _ in range(200):
        if result_path.with_suffix('.cancel').exists():
            result_path.write_text(json.dumps({'status': 'cancelled', 'cleanup_status': 'completed'}))
            sys.exit(0)
        time.sleep(0.05)
if node_name in {'u1', 'twin'}:
    tbl = TableSpec(source_id='s1', name='t_a', columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
    rows = [('C1', '华东合同')]
    draft = {
        'draft_id': 'd_u1',
        'entities': [{
            'type': 'Contract', 'key_names': ['code'],
            'mappings': [{'source_id': 's1', 'table': 't_a', 'key_columns': ['code'], 'label_column': 'name'}],
        }],
    }
elif node_name == 'u2':
    tbl = TableSpec(source_id='s2', name='t_b', columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
    rows = [('S1', '九月结算单')]
    draft = {
        'draft_id': 'd_u2',
        'entities': [{
            'type': 'Settlement', 'key_names': ['code'],
            'mappings': [{'source_id': 's2', 'table': 't_b', 'key_columns': ['code'], 'label_column': 'name'}],
        }],
    }
else:
    # Full merge-and-extract node: receives frozen upstream Contract & Settlement via inherited_graphs!
    tbl = TableSpec(source_id='s3', name='t_rel', columns=[ColumnSpec(name='c_code'), ColumnSpec(name='s_code')])
    rows = [('C1', 'S1')]
    draft = {
        'draft_id': 'd_merge',
        'extends_base_graph': True,
        'inherited_entity_types': ['Contract', 'Settlement'],
        'inherited_entity_key_arities': {'Contract': 1, 'Settlement': 1},
        'relations': [{
            'type': 'SETTLED_BY',
            'source_type': 'Contract',
            'target_type': 'Settlement',
                'mappings': [{
                    'source_id': 's3', 'table': 't_rel',
                    'source_key_columns': ['c_code'], 'target_key_columns': ['s_code'],
                    'source_lookup': {'source_id': '__base_graph__', 'table': 'Contract', 'match_columns': ['code'], 'key_columns': ['code']},
                    'target_lookup': {'source_id': '__base_graph__', 'table': 'Settlement', 'match_columns': ['code'], 'key_columns': ['code']},
                }],
        }],
    }

with MemorySource([tbl], {tbl.qualified_name: rows}) as src:
    tools = ConstructionTools(cfg, out / 'construction', adapters=[src])
    try:
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        assert tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
        assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
    finally:
        tools.close()

rep = RunReport(run_id=result_path.stem, status='COMPLETED', counts={'business_nodes': len(rows), 'business_edges': 1 if node_name == 'm_full' else 0})
result_path.write_text(json.dumps({'status': 'completed', 'cleanup_status': 'completed', 'report': rep.model_dump(mode='json')}))
'''
    worker_file = tmp_path / "dag_worker.py"
    worker_file.write_text(worker_script, encoding="utf-8")
    real_popen = execution_mod.subprocess.Popen
    monkeypatch.setattr(
        execution_mod.subprocess,
        "Popen",
        lambda args, **kw: real_popen([sys.executable, str(worker_file), args[-1]], **kw),
    )

    app = create_app(tmp_path / "ws")
    with TestClient(app) as client:
        # Case A: Extract-only node (no merge node) -> direct_output!
        client.post("/api/projects", json={"name": "DirectExtract", "project_id": "dag_direct"})
        etag = client.get("/api/projects/dag_direct/config").headers["ETag"]
        client.post(
            "/api/projects/dag_direct/files",
            files={"file": ("notes.md", b"# Direct\n", "text/markdown")},
            headers={"If-Match": etag},
        )
        cfg_res = client.get("/api/projects/dag_direct/config")
        cfg_a = cfg_res.json()
        etag = cfg_res.headers["ETag"]
        cfg_a["goal_markdown"] = "# 仅抽取节点直出测试"
        cfg_a["model"] = {"mode": "in_environment", "model": "offline", "base_url": "https://model.invalid/v1"}
        cfg_a["workflow"] = {
            "enabled": True,
            "max_parallel_workers": 2,
            "units": [{"unit_id": "u1", "label": "单独抽取节点"}],
            "merge_nodes": [],
            "edges": [{"edge_id": "e_u1_out", "source": "u1", "target": "output"}],
        }
        r_save_a = client.put("/api/projects/dag_direct/config", json=cfg_a, headers={"If-Match": etag})
        assert r_save_a.status_code == 200, r_save_a.text
        etag = r_save_a.headers["ETag"]

        r_run_a = client.post("/api/projects/dag_direct/runs", json={"run_mode": "continue"}, headers={"If-Match": etag})
        assert r_run_a.status_code == 202
        rid_a = r_run_a.json()["run_id"]
        for _ in range(200):
            cur = client.get(f"/api/projects/dag_direct/runs/{rid_a}").json()
            if cur["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert cur["status"] == "completed"
        wf_a = client.get("/api/projects/dag_direct/workflow").json()
        assert wf_a["plan_mode"] == "direct_single"
        assert wf_a["state"]["merge"]["status"] == "direct_output"
        g_a = client.get("/api/projects/dag_direct/graph").json()
        assert [n["identity"]["code"] for n in g_a["nodes"]] == ["C1"]
        # After extraction finishes, graph is NOT vectorized until user requests!
        st_a = client.get("/api/projects/dag_direct/retrieval/status").json()
        assert st_a["status"] == "not_vectorized"

        # Same identity in two independent branches stays separate even when the
        # project's merge policy permits that identity to be shared at a merge node.
        cfg_a['workflow'].update(units=[{'unit_id': 'u1'}, {'unit_id': 'twin'}], edges=[],
                                 merge_policy={'shared_entity_types': ['Contract']})
        saved_multi = client.put('/api/projects/dag_direct/config', json=cfg_a, headers={'If-Match': etag})
        assert saved_multi.status_code == 200
        etag_multi = saved_multi.headers['ETag']
        multi = client.post('/api/projects/dag_direct/runs', json={'run_mode': 'continue'}, headers={'If-Match': etag_multi})
        assert multi.status_code == 202
        for _ in range(200):
            result = client.get('/api/projects/dag_direct/runs/' + multi.json()['run_id']).json()
            if result['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert result['status'] == 'completed', result
        assert result['workflow_state']['plan_mode'] == 'direct_multi'
        for publish_again in (False, True):
            if publish_again:
                rerun = client.post('/api/projects/dag_direct/workflow/merge', headers={'If-Match': etag_multi})
                assert rerun.json()['mode'] == 'direct_multi_output'
            independent = client.get('/api/projects/dag_direct/graph').json()['nodes']
            assert [n['identity']['code'] for n in independent] == ['C1', 'C1']
            assert len({n['id'] for n in independent}) == 2

        # Case B: u1 + u2 -> m_full (merge_and_extract) -> output!
        client.post("/api/projects", json={"name": "MergeExtractDAG", "project_id": "dag_merge"})
        etag = client.get("/api/projects/dag_merge/config").headers["ETag"]
        client.post(
            "/api/projects/dag_merge/files",
            files={"file": ("notes.md", b"# MergeExtract\n", "text/markdown")},
            headers={"If-Match": etag},
        )
        cfg_res_b = client.get("/api/projects/dag_merge/config")
        cfg_b = cfg_res_b.json()
        etag = cfg_res_b.headers["ETag"]
        cfg_b["goal_markdown"] = "# 分节点抽取 + 全节点合并抽取测试"
        cfg_b["model"] = {"mode": "in_environment", "model": "offline", "base_url": "https://model.invalid/v1"}
        cfg_b["workflow"] = {
            "enabled": True,
            "max_parallel_workers": 2,
            "units": [
                {"unit_id": "u1", "label": "分支1：合同"},
                {"unit_id": "u2", "label": "分支2：结算"},
            ],
            "merge_nodes": [
                {"node_id": "last", "merge_mode": "deterministic"},
                {
                    "node_id": "m_full",
                    "label": "全节点合并抽取",
                    "merge_mode": "merge_and_extract",
                    "goal_markdown": "补全 Contract 与 Settlement 之间的 SETTLED_BY 跨分支关系。",
                }
            ],
            "edges": [
                {"edge_id": "e1", "source": "u1", "target": "m_full"},
                {"edge_id": "e2", "source": "u2", "target": "m_full"},
                {"edge_id": "e3", "source": "m_full", "target": "last"},
                {"edge_id": "e4", "source": "last", "target": "output"},
            ],
        }
        r_save_b = client.put("/api/projects/dag_merge/config", json=cfg_b, headers={"If-Match": etag})
        assert r_save_b.status_code == 200, r_save_b.text
        etag = r_save_b.headers["ETag"]

        out_b = Path(app.state.workspace.resolved_config('dag_merge').output_dir)
        out_b.mkdir(parents=True, exist_ok=True)
        fail_u2 = out_b / 'force_u2_failure'
        fail_u2.touch()
        first = client.post('/api/projects/dag_merge/runs', json={'run_mode': 'continue'}, headers={'If-Match': etag})
        assert first.status_code == 202
        for _ in range(200):
            failed = client.get('/api/projects/dag_merge/runs/' + first.json()['run_id']).json()
            if failed['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert failed['status'] == 'partial', failed
        assert failed['workflow_state']['merge']['status'] == 'skipped'
        assert not (out_b / 'construction' / 'manifest.yaml').exists()
        fail_u2.unlink()
        r_run_b = client.post('/api/projects/dag_merge/workflow/units/u2/retry', headers={'If-Match': etag})
        assert r_run_b.status_code == 202
        rid_b = r_run_b.json()["run_id"]
        for _ in range(200):
            cur_b = client.get(f"/api/projects/dag_merge/runs/{rid_b}").json()
            if cur_b["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert cur_b["status"] == "completed", cur_b
        wf_b = client.get("/api/projects/dag_merge/workflow").json()
        assert wf_b["plan_mode"] == "dag_merge"
        assert wf_b["state"]["units"]["u1"]["status"] == "completed"
        assert wf_b["state"]["units"]["u2"]["status"] == "completed"
        assert wf_b["state"]["merge_nodes"]["m_full"]["status"] == "completed"

        g_b = client.get("/api/projects/dag_merge/graph").json()
        assert {n["identity"]["code"] for n in g_b["nodes"]} == {"C1", "S1"}
        assert len(g_b["edges"]) == 1
        assert g_b["edges"][0]["relation"] == "SETTLED_BY"

        legacy = client.post('/api/projects/dag_merge/workflow/merge', headers={'If-Match': etag})
        assert legacy.json()['error']['code'] == 'workflow_dag_requires_run'
        assert client.get('/api/projects/dag_merge/graph').json()['edges'] == g_b['edges']

        # A failed replacement cannot feed either downstream merge. Keep the last
        # successful graph (including the merge-and-extract relation) visible.
        Path(app.state.workspace.resolved_config('dag_merge').output_dir, 'force_u2_failure').touch()
        retry = client.post('/api/projects/dag_merge/workflow/units/u2/retry', headers={'If-Match': etag})
        assert retry.status_code == 202
        for _ in range(200):
            result = client.get('/api/projects/dag_merge/runs/' + retry.json()['run_id']).json()
            if result['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert result['status'] == 'partial', result
        assert [result['workflow_state']['merge_nodes'][m]['status'] for m in ('m_full', 'last')] == ['skipped'] * 2
        assert result['workflow_state']['merge']['status'] == 'skipped'
        assert client.get('/api/projects/dag_merge/graph').json()['edges'] == g_b['edges']

        # Cancellation while the merge-and-extract subprocess is active must
        # not publish a fallback graph or report a completed downstream merge.
        out = Path(app.state.workspace.resolved_config('dag_merge').output_dir)
        (out / 'force_u2_failure').unlink()
        (out / 'pause_merge').touch()
        retry = client.post('/api/projects/dag_merge/workflow/units/u2/retry', headers={'If-Match': etag})
        rid = retry.json()['run_id']
        for _ in range(200):
            item = client.get('/api/projects/dag_merge/runs/' + rid).json()
            if item['workflow_state']['merge_nodes']['m_full']['status'] == 'running':
                break
            time.sleep(0.05)
        assert item['workflow_state']['merge_nodes']['m_full']['status'] == 'running'
        cancelled = client.post('/api/projects/dag_merge/runs/' + rid + '/cancel').json()
        assert cancelled['status'] == 'cancelled', cancelled
        assert cancelled['workflow_state']['merge']['status'] == 'cancelled'
        assert client.get('/api/projects/dag_merge/graph').json()['edges'] == g_b['edges']


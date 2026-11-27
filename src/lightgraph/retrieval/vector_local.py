"""Local NumPy vector store with strict pre-ranking scope filtering and replaceable embedders."""
from __future__ import annotations

from contextlib import closing
from ipaddress import ip_address
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Protocol, Sequence
from urllib.parse import urlsplit

import numpy as np

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


class Embedder(Protocol):
    dim: int | None

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ''
    if host.rstrip('.').lower() == 'localhost':
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def embedder_fingerprint(embedder: Embedder | None) -> str:
    """Index and query must agree on the vector space and query instruction."""
    if embedder is None:
        return 'lexical_v1'
    configured_dim = getattr(embedder, 'configured_dim', embedder.dim)
    identity = (
        type(embedder).__module__,
        type(embedder).__qualname__,
        configured_dim,
        getattr(embedder, 'model', None),
        getattr(embedder, 'base_url', None),
    )
    instruction = getattr(embedder, 'query_instruction', '')
    # The original v2 hash and HTTP indexes never used a query instruction.
    # Preserve that identity for projects which can keep using those indexes.
    return digest(*identity, instruction) if instruction else digest(*identity)


class OpenAIEmbedder:
    """OpenAI-compatible /v1/embeddings client (works with Qwen3-Embedding, Ollama, vLLM, cloud gateways)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = '',
        timeout_seconds: float = 120.0,
        dim: int | None = None,
        batch_size: int = 32,
        query_instruction: str = '',
    ):
        self.base_url = base_url.rstrip('/')
        self.model = model.strip()
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.configured_dim = dim
        self.dim = dim
        self.batch_size = max(1, int(batch_size))
        self.query_instruction = query_instruction.strip()
        if not self.base_url or not self.model:
            raise LightGraphError('embedding_endpoint_required')

    def _endpoint(self) -> str:
        return self.base_url if self.base_url.endswith('/embeddings') else f'{self.base_url}/embeddings'

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        import httpx
        payload: dict = {'model': self.model, 'input': batch}
        if self.configured_dim is not None:
            payload['dimensions'] = self.configured_dim
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        try:
            resp = httpx.post(
                self._endpoint(),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
                # macOS can supply an OS-level HTTP proxy even with no proxy
                # environment variables. Keep loopback cards and keys local.
                trust_env=not _is_loopback(self.base_url),
            )
            resp.raise_for_status()
            body = resp.json()
        except httpx.TimeoutException:
            raise LightGraphError('embedding_timeout') from None
        except httpx.HTTPError:
            raise LightGraphError('embedding_service_unavailable') from None
        except Exception:
            raise LightGraphError('embedding_response_invalid') from None
        try:
            raw_data = body['data']
            if not isinstance(raw_data, list):
                raise ValueError
            data = sorted(raw_data, key=lambda item: item['index'])
            if [item['index'] for item in data] != list(range(len(batch))):
                raise ValueError
            arr = np.asarray([item['embedding'] for item in data], dtype=np.float32)
            if arr.ndim != 2 or arr.shape[0] != len(batch) or arr.shape[1] <= 0:
                raise ValueError
            if self.dim is not None and arr.shape[1] != self.dim:
                raise ValueError
            if not np.isfinite(arr).all():
                raise ValueError
            # Accumulate in float64 so finite float32 responses cannot overflow
            # the norm into infinity and silently become all-zero vectors.
            norms = np.linalg.norm(arr.astype(np.float64), axis=1, keepdims=True)
            if (norms == 0).any():
                raise ValueError
        except Exception:
            raise LightGraphError('embedding_response_invalid') from None
        self.dim = int(arr.shape[1])
        return (arr / norms).astype(np.float32)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        items = [str(t) for t in texts]
        if not items:
            return np.zeros((0, int(self.dim or 0)), dtype=np.float32)
        chunks = [
            self._embed_batch(items[start:start + self.batch_size])
            for start in range(0, len(items), self.batch_size)
        ]
        return chunks[0] if len(chunks) == 1 else np.vstack(chunks)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        if not self.query_instruction:
            return self.embed(texts)
        formatted = [f'Instruct: {self.query_instruction}\nQuery: {t}' for t in texts]
        return self.embed(formatted)


def resolve_embedder(config, *, api_key: str | None = None) -> Embedder | None:
    """Return the configured model, or None for lexical and graph retrieval."""
    settings = getattr(config, 'embedding', None)
    if settings is None or settings.provider in {'none', 'local_hash'}:
        return None
    if settings.provider != 'openai_compatible':
        raise LightGraphError('embedding_provider_unsupported')
    if not settings.base_url or not settings.model:
        raise LightGraphError('embedding_endpoint_required')
    parsed = urlsplit(settings.base_url)
    if parsed.scheme == 'http' and not _is_loopback(settings.base_url) and not settings.allow_insecure_http:
        raise LightGraphError('embedding_insecure_endpoint')
    secret = settings.api_key.get_secret_value() if settings.api_key else ''
    resolved_key = api_key if api_key is not None else (secret or os.getenv(settings.api_key_env, ''))
    return OpenAIEmbedder(
        base_url=settings.base_url,
        model=settings.model,
        api_key=resolved_key,
        timeout_seconds=settings.timeout_seconds,
        dim=settings.dimensions,
        batch_size=settings.batch_size,
        query_instruction=settings.query_instruction,
    )


def test_embedding_service(config, *, api_key: str | None = None) -> dict:
    """Verify live reachability and detected vector dimension for the configured embedding service."""
    embedder = resolve_embedder(config, api_key=api_key)
    if embedder is None:
        raise LightGraphError('embedding_not_configured')
    started = time.monotonic()
    vec = embedder.embed(['lightgraph embedding health check'])
    elapsed_ms = round(max(0.0, (time.monotonic() - started) * 1000.0), 1)
    dim = int(vec.shape[1]) if vec.ndim == 2 else int(embedder.dim or 0)
    return {
        'status': 'ok',
        'provider': 'openai_compatible',
        'base_url': getattr(embedder, 'base_url', ''),
        'model': embedder.model,
        'dimensions': dim,
        'latency_ms': elapsed_ms,
    }


class LocalNumpyVectorStore:
    """Local exact cosine vector index enforcing scope isolation before similarity scoring."""

    def __init__(self, root_dir: str | Path):
        from lightgraph.construction.paths import io_path
        self.root = io_path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.root / 'vectors.npy'
        self.meta_path = self.root / 'vectors_meta.sqlite'
        if not self.meta_path.exists():
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.meta_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vec_items (
                    row_idx INTEGER PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    field TEXT NOT NULL,
                    card_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vec_scope ON vec_items(scope_ref, field, row_idx);
                CREATE INDEX IF NOT EXISTS idx_vec_node ON vec_items(node_id);
                """
            )

    def replace_all(self, items: Sequence[dict], vectors: np.ndarray) -> None:
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(items):
            raise ValueError('vector_count_mismatch')
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        arr = arr / norms
        tmp_vec = self.root / '.vectors.tmp.npy'
        with tmp_vec.open('wb') as f:
            np.save(f, arr)
        tmp_vec.replace(self.vectors_path)
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('DELETE FROM vec_items')
                conn.executemany(
                    'INSERT INTO vec_items (row_idx, node_id, kind, scope_ref, field, card_text) VALUES (?, ?, ?, ?, ?, ?)',
                    [
                        (idx, str(item['node_id']), str(item['kind']), str(item['scope_ref']),
                         item.get('field', 'all'), str(item['card_text']))
                        for idx, item in enumerate(items)
                    ],
                )
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise

    def search_batch(
        self,
        query_vectors: np.ndarray,
        *,
        scopes: Sequence[str] | set[str],
        top_k: int = 20,
        field: str = 'all',
        positive_only: bool = True,
        with_peer_reference: bool = False,
    ) -> list[list[dict]]:
        scope_list = sorted(set(scopes))
        q = np.asarray(query_vectors, dtype=np.float32)
        if q.ndim == 1:
            q = q.reshape(1, -1)
        if q.ndim != 2:
            raise ValueError('query_vector_shape_invalid')
        if not scope_list or not len(q) or top_k <= 0:
            return [[] for _ in q]
        matrix = np.load(self.vectors_path, mmap_mode='r')
        if matrix.ndim != 2 or matrix.shape[0] == 0:
            return [[] for _ in q]
        if matrix.shape[1] != q.shape[1]:
            raise LightGraphError('retrieval_index_stale')
        norms = np.linalg.norm(q, axis=1, keepdims=True)
        q = q / np.where(norms == 0, 1.0, norms)
        placeholders = 'SELECT value FROM json_each(?)'
        hits = [{} for _ in q]
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                f'SELECT row_idx, node_id, kind, scope_ref FROM vec_items '
                f'WHERE scope_ref IN ({placeholders}) AND field = ? ORDER BY row_idx',
                [json.dumps(scope_list), field],
            )
            # Exact scoring, bounded dense working set. Reduce by node before
            # choosing top K so multiple authorized cards cannot crowd out peers.
            while rows := cursor.fetchmany(512):
                batch = matrix[[row['row_idx'] for row in rows]]
                scores = batch @ q.T
                for col, best in enumerate(hits):
                    for pos, row in enumerate(rows):
                        nid = row['node_id']
                        score = float(scores[pos, col])
                        if (not positive_only or score > 0) and (nid not in best or score > best[nid]['score']):
                            best[nid] = {'node_id': nid, 'kind': row['kind'], 'score': score,
                                         'scope_ref': row['scope_ref']}
                            if with_peer_reference:
                                best[nid]['row_idx'] = row['row_idx']
                    if len(best) > top_k:
                        ranked = sorted(best, key=lambda nid: (-best[nid]['score'], nid))[:top_k]
                        hits[col] = {nid: best[nid] for nid in ranked}
        batches = [sorted(best.values(), key=lambda hit: (-hit['score'], hit['node_id'])) for best in hits]
        if with_peer_reference:
            # Query-local reference among the actual ranked competitors. This uses
            # at most top_k rows, without rescoring the whole authorized cohort.
            for batch in batches:
                if not batch:
                    continue
                references = matrix[[hit.pop('row_idx') for hit in batch]]
                similarity = references @ references.T
                np.fill_diagonal(similarity, -np.inf)
                for hit, row in zip(batch, similarity):
                    hit['peer_score'] = float(row.max()) if len(batch) > 1 else None
        return batches

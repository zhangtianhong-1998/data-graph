"""Local NumPy vector store with strict pre-ranking scope filtering and replaceable embedders."""
from __future__ import annotations

from contextlib import closing
import hashlib
from ipaddress import ip_address
import os
from pathlib import Path
import sqlite3
import time
from typing import Protocol, Sequence
import unicodedata
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


def embedder_fingerprint(embedder: Embedder) -> str:
    """Index and query must agree on the vector space and query instruction."""
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


def _norm_text(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


class LocalHashEmbedder:
    """Offline deterministic char-ngram + token hashed L2-normalized dense embedder."""

    def __init__(self, dim: int = 256):
        if dim < 16:
            raise ValueError('embedder_dim_too_small')
        self.dim = dim
        self.configured_dim = dim
        self.query_instruction = ''

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed(texts)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row_idx, raw in enumerate(texts):
            text = _norm_text(raw)
            if not text:
                continue
            features: list[tuple[str, float]] = []
            compact = text.replace(' ', '')
            for size, weight in ((2, 1.0), (3, 1.3)):
                if len(compact) >= size:
                    for i in range(len(compact) - size + 1):
                        features.append((f'c{size}:{compact[i:i + size]}', weight))
            for tok in text.replace('_', ' ').replace('|', ' ').split():
                if tok:
                    features.append((f'w:{tok}', 2.0))
            if not features:
                features.append((f'c1:{compact}', 1.0))
            vec = matrix[row_idx]
            for feat, weight in features:
                token_digest = hashlib.blake2b(feat.encode('utf-8'), digest_size=8).digest()
                bucket = int.from_bytes(token_digest[:4], 'little') % self.dim
                sign = 1.0 if (token_digest[4] & 1) == 0 else -1.0
                vec[bucket] += sign * weight
            norm = float(np.linalg.norm(vec))
            if norm > 0.0:
                vec /= norm
        return matrix


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


def resolve_embedder(config, *, api_key: str | None = None) -> Embedder:
    """Resolve the project's configured Embedding service (replaceable HTTP endpoint or offline fallback)."""
    settings = getattr(config, 'embedding', None)
    if settings is None or settings.provider == 'local_hash':
        return LocalHashEmbedder()
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
    started = time.monotonic()
    vec = embedder.embed(['lightgraph embedding health check'])
    elapsed_ms = round(max(0.0, (time.monotonic() - started) * 1000.0), 1)
    dim = int(vec.shape[1]) if vec.ndim == 2 else int(embedder.dim or 0)
    return {
        'status': 'ok',
        'provider': 'openai_compatible' if isinstance(embedder, OpenAIEmbedder) else 'local_hash',
        'base_url': getattr(embedder, 'base_url', ''),
        'model': getattr(embedder, 'model', 'local_hash'),
        'dimensions': dim,
        'latency_ms': elapsed_ms,
    }


class LocalNumpyVectorStore:
    """Local exact cosine vector index enforcing scope isolation before similarity scoring."""

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.root / 'vectors.npy'
        self.meta_path = self.root / 'vectors_meta.sqlite'
        if not self.meta_path.exists():
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.meta_path, isolation_level=None)
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
        placeholders = ','.join('?' for _ in scope_list)
        hits = [{} for _ in q]
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                f'SELECT row_idx, node_id, kind, scope_ref FROM vec_items '
                f'WHERE scope_ref IN ({placeholders}) AND field = ? ORDER BY row_idx',
                [*scope_list, field],
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
            # Measure how close the same field of *another permitted node*
            # normally gets to each candidate. Batch only the returned cards.
            cards = {hit['row_idx']: hit['node_id'] for batch in batches for hit in batch}
            if cards:
                peers = self.search_batch(matrix[list(cards)], scopes=scope_list, top_k=2,
                                          field=field, positive_only=False)
                references = {row: next((h['score'] for h in group if h['node_id'] != node_id), None)
                              for (row, node_id), group in zip(cards.items(), peers)}
                for batch in batches:
                    for hit in batch:
                        hit['peer_score'] = references[hit.pop('row_idx')]
        return batches

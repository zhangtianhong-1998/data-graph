"""Local NumPy vector store with strict pre-ranking scope filtering and pluggable embedders."""
from __future__ import annotations

import hashlib
from lightgraph.contracts import digest
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Protocol, Sequence
import unicodedata

import numpy as np



class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def embedder_fingerprint(embedder: Embedder) -> str:
    """Index and query must agree on the vector space, including equal-width models."""
    return digest(type(embedder).__module__, type(embedder).__qualname__, embedder.dim,
                  getattr(embedder, 'model', None), getattr(embedder, 'base_url', None))


def _norm_text(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


class LocalHashEmbedder:
    """Offline deterministic char-ngram + token hashed L2-normalized dense embedder."""

    def __init__(self, dim: int = 256):
        if dim < 16:
            raise ValueError('embedder_dim_too_small')
        self.dim = dim

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
                digest = hashlib.blake2b(feat.encode('utf-8'), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], 'little') % self.dim
                sign = 1.0 if (digest[4] & 1) == 0 else -1.0
                vec[bucket] += sign * weight
            norm = float(np.linalg.norm(vec))
            if norm > 0.0:
                vec /= norm
        return matrix


class OpenAIEmbedder:
    """OpenAI-compatible /v1/embeddings client for production semantic vectors."""

    def __init__(self, *, base_url: str, model: str, api_key: str, timeout_seconds: float = 30.0, dim: int = 1536):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        import httpx
        url = self.base_url if self.base_url.endswith('/embeddings') else f'{self.base_url}/embeddings'
        resp = httpx.post(
            url,
            headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
            json={'model': self.model, 'input': list(texts)},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        data = sorted(resp.json()['data'], key=lambda item: item['index'])
        if [item['index'] for item in data] != list(range(len(texts))):
            raise ValueError('embedding_response_indices_mismatch')
        arr = np.asarray([item['embedding'] for item in data], dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(texts):
            raise ValueError('embedding_response_shape_mismatch')
        if arr.shape[1] != self.dim or not np.isfinite(arr).all():
            raise ValueError('embedding_response_invalid')
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


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
                    card_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vec_scope ON vec_items(scope_ref, row_idx);
                CREATE INDEX IF NOT EXISTS idx_vec_node ON vec_items(node_id);
                """
            )

    def replace_all(self, items: Sequence[dict], vectors: np.ndarray) -> None:
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(items):
            raise ValueError('vector_count_mismatch')
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
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
                    'INSERT INTO vec_items (row_idx, node_id, kind, scope_ref, card_text) VALUES (?, ?, ?, ?, ?)',
                    [
                        (idx, str(item['node_id']), str(item['kind']), str(item['scope_ref']), str(item['card_text']))
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
        if matrix.ndim != 2 or matrix.shape[1] != q.shape[1]:
            raise ValueError('query_vector_dimension_mismatch')
        norms = np.linalg.norm(q, axis=1, keepdims=True)
        q = q / np.where(norms == 0, 1.0, norms)
        placeholders = ','.join('?' for _ in scope_list)
        hits = [{} for _ in q]
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                f'SELECT row_idx, node_id, kind, scope_ref FROM vec_items WHERE scope_ref IN ({placeholders}) ORDER BY row_idx',
                scope_list,
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
                        if score > 0 and (nid not in best or score > best[nid]['score']):
                            best[nid] = {'node_id': nid, 'kind': row['kind'], 'score': score,
                                         'scope_ref': row['scope_ref']}
                    if len(best) > top_k:
                        ranked = sorted(best, key=lambda nid: (-best[nid]['score'], nid))[:top_k]
                        hits[col] = {nid: best[nid] for nid in ranked}
        return [sorted(best.values(), key=lambda hit: (-hit['score'], hit['node_id'])) for best in hits]

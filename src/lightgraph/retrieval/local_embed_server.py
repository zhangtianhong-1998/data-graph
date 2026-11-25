"""Standalone, replaceable OpenAI-compatible local embedding server (e.g. Qwen/Qwen3-Embedding-0.6B).

Start locally on the test/GPU/CPU machine:
    .venv/bin/lightgraph embed-server --model Qwen/Qwen3-Embedding-0.6B --port 8002

Then point the project's Embedding config in the Workbench UI at:
    base_url: http://127.0.0.1:8002/v1
    model:    Qwen/Qwen3-Embedding-0.6B

For remote clients set LIGHTGRAPH_EMBEDDING_SERVER_KEY and configure the same Bearer token on the client.
Without a key only loopback clients can access model inference.

Any OpenAI /v1/embeddings-compatible service (Ollama, vLLM, LM Studio, TEI, cloud API)
can replace this server with zero changes to LightGraph code.
"""
from __future__ import annotations

import hmac
from ipaddress import ip_address
import os
import threading
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from starlette.responses import JSONResponse


class LocalEmbeddingBackend(Protocol):
    model_name: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class _BackendUnavailable(RuntimeError):
    """Only a missing optional dependency permits choosing another backend."""


class SentenceTransformersBackend:
    def __init__(self, model_name: str, *, device: str | None = None, trust_remote_code: bool = False):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise _BackendUnavailable(
                '需要安装 sentence-transformers 与 torch 才能加载本地或 HuggingFace 向量模型：'
                '.venv/bin/python -m pip install -e ".[embedding]"'
            ) from exc
        kwargs: dict[str, Any] = {'trust_remote_code': trust_remote_code}
        if device:
            kwargs['device'] = device
        self._model = SentenceTransformer(model_name, **kwargs)
        self.model_name = model_name
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # The HTTP client owns query instructions. Disable any default prompt
        # stored with a model so it cannot affect documents or double-prefix queries.
        vectors = self._model.encode(texts, prompt='', normalize_embeddings=True, convert_to_numpy=True)
        return vectors.tolist()


class TransformersLastTokenBackend:
    """Fallback HF transformers backend supporting Qwen3-Embedding last-token pooling."""

    def __init__(self, model_name: str, *, device: str | None = None, trust_remote_code: bool = False):
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                '需要安装 torch 与 transformers 才能加载本地向量模型：'
                '.venv/bin/python -m pip install -e ".[embedding]"'
            ) from exc
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name, padding_side='left', trust_remote_code=trust_remote_code
        )
        self._model = AutoModel.from_pretrained(model_name, trust_remote_code=trust_remote_code)
        if getattr(self._model.config, 'model_type', None) != 'qwen3':
            raise RuntimeError('transformers_last_token_backend_requires_qwen3_use_sentence_transformers')
        if device:
            self._device = device
        elif torch.cuda.is_available():
            self._device = 'cuda'
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            self._device = 'mps'
        else:
            self._device = 'cpu'
        self._model.to(self._device)
        self._model.eval()
        self.model_name = model_name
        self.dim = int(getattr(self._model.config, 'hidden_size', 0) or 0)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        torch = self._torch
        batch = self._tokenizer(texts, padding=True, truncation=True, max_length=8192, return_tensors='pt')
        batch = {k: v.to(self._device) for k, v in batch.items()}
        with torch.no_grad():
            outputs = self._model(**batch)
            hidden = outputs.last_hidden_state
            mask = batch['attention_mask']
            left_padding = bool((mask[:, -1].sum() == mask.shape[0]).item())
            if left_padding:
                pooled = hidden[:, -1]
            else:
                seq_lens = mask.sum(dim=1) - 1
                pooled = hidden[torch.arange(hidden.shape[0], device=hidden.device), seq_lens]
            normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return normalized.cpu().tolist()


def load_local_backend(
    model_name: str,
    *,
    backend: str = 'auto',
    device: str | None = None,
    trust_remote_code: bool = False,
) -> LocalEmbeddingBackend:
    if backend not in {'auto', 'sentence_transformers', 'transformers'}:
        raise ValueError('embedding_backend_unsupported')
    if backend == 'sentence_transformers':
        return SentenceTransformersBackend(model_name, device=device, trust_remote_code=trust_remote_code)
    if backend == 'transformers':
        return TransformersLastTokenBackend(model_name, device=device, trust_remote_code=trust_remote_code)
    try:
        return SentenceTransformersBackend(model_name, device=device, trust_remote_code=trust_remote_code)
    except _BackendUnavailable:
        return TransformersLastTokenBackend(model_name, device=device, trust_remote_code=trust_remote_code)


_Text = Annotated[str, Field(max_length=32768)]
_MAX_BODY_BYTES = 2 * 1024 * 1024


class EmbeddingRequest(BaseModel):
    input: _Text | Annotated[list[_Text], Field(max_length=512)]
    model: str | None = None
    dimensions: int | None = Field(default=None, gt=0)


    @model_validator(mode='after')
    def bound_batch(self):
        texts = [self.input] if isinstance(self.input, str) else self.input
        if sum(map(len, texts)) > 131072:
            raise ValueError('embedding_batch_too_large')
        return self


class _InferenceGuard:
    """Bound the incoming body, including chunked requests, before parsing or inference."""
    def __init__(self, app, key):
        self.app, self.key = app, key

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/v1/'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        host = (scope.get('client') or ('',))[0]
        try:
            local = ip_address(host).is_loopback
        except ValueError:
            local = False
        if (self.key and not hmac.compare_digest(headers.get(b'authorization', b''), b'Bearer ' + self.key)
                or not self.key and not local):
            return await JSONResponse({'detail': 'embedding_auth_required'}, 401)(scope, receive, send)
        if scope['path'] != '/v1/embeddings':
            return await self.app(scope, receive, send)
        data = bytearray()
        while True:
            message = await receive()
            if message['type'] != 'http.request':
                return
            chunk = message.get('body', b'')
            if len(data) + len(chunk) > _MAX_BODY_BYTES:
                return await JSONResponse({'detail': 'embedding_batch_too_large'}, 413)(scope, receive, send)
            data.extend(chunk)
            if not message.get('more_body'):
                break
        async def read():
            return {'type': 'http.request', 'body': bytes(data), 'more_body': False}
        await self.app(scope, read, send)


def create_embedding_app(backend: LocalEmbeddingBackend) -> FastAPI:
    app = FastAPI(title='LightGraph Local Embedding Server', version='1.0.0')
    app.add_middleware(_InferenceGuard, key=os.environ.get('LIGHTGRAPH_EMBEDDING_SERVER_KEY', '').encode())
    # FastAPI runs synchronous handlers in a thread pool; serialize inference
    # against the single loaded model to bound memory and avoid device races.
    inference_lock = threading.Lock()

    @app.get('/health')
    def health():
        return {'status': 'ok', 'model': backend.model_name, 'dimensions': backend.dim}

    @app.get('/v1/models')
    def list_models():
        return {
            'object': 'list',
            'data': [{'id': backend.model_name, 'object': 'model', 'owned_by': 'local'}],
        }

    @app.post('/v1/embeddings')
    def create_embeddings(req: EmbeddingRequest):
        if req.model is not None and req.model != backend.model_name:
            raise HTTPException(status_code=404, detail='embedding_model_not_found')
        if req.dimensions is not None and req.dimensions > backend.dim:
            raise HTTPException(status_code=400, detail='requested_dimensions_exceed_model_dim')
        texts = [req.input] if isinstance(req.input, str) else list(req.input)
        if not texts:
            return {'object': 'list', 'data': [], 'model': backend.model_name}
        import numpy as np
        with inference_lock:
            vectors = backend.encode(texts)
        try:
            arr = np.asarray(vectors, dtype=np.float64)
            if arr.shape != (len(texts), backend.dim) or not np.isfinite(arr).all():
                raise ValueError
            if req.dimensions is not None:
                # The caller must choose a model trained for dimension truncation.
                arr = arr[:, :req.dimensions]
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            if (norms == 0).any() or not np.isfinite(norms).all():
                raise ValueError
            vectors = (arr / norms).tolist()
        except (TypeError, ValueError):
            raise HTTPException(status_code=500, detail='embedding_backend_response_invalid') from None
        return {
            'object': 'list',
            'model': backend.model_name,
            'data': [
                {'object': 'embedding', 'index': idx, 'embedding': vec}
                for idx, vec in enumerate(vectors)
            ],
        }

    return app

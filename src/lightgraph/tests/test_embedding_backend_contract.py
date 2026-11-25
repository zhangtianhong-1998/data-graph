"""Embedding protocol and publication regressions; no real model quality claims."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import numpy as np
import pytest

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.retrieval import local_embed_server as server
from lightgraph.retrieval.sync import active_index, sync_graph_to_retrieval
from lightgraph.retrieval.vector_local import OpenAIEmbedder, embedder_fingerprint


class StubBackend:
    model_name = 'stub-model'
    dim = 3

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(texts)
        return [[1.0, 2.0, 3.0] for _ in texts]


def test_server_rejects_wrong_model_before_inference():
    backend = StubBackend()
    with TestClient(server.create_embedding_app(backend), client=('127.0.0.1', 50123)) as client:
        wrong = client.post('/v1/embeddings', json={'model': 'another-model', 'input': 'document'})
        assert wrong.status_code == 404
        assert not backend.calls
        too_wide = client.post('/v1/embeddings', json={'input': 'document', 'dimensions': 4})
        assert too_wide.status_code == 400
        assert not backend.calls
        valid = client.post('/v1/embeddings', json={'model': 'stub-model', 'input': 'document'})
        assert valid.status_code == 200
        assert valid.json()['model'] == 'stub-model'
        # Characters are not tokens: usage is omitted without a token count.
        assert 'usage' not in valid.json()


@pytest.mark.parametrize('vectors', [[], [[1., 2.]], [[0., 0., 0.]], [[float('nan'), 1., 2.]]])
def test_server_does_not_publish_invalid_backend_vectors(vectors):
    backend = StubBackend()
    backend.encode = lambda texts: vectors
    with TestClient(server.create_embedding_app(backend), client=('127.0.0.1', 50123)) as client:
        response = client.post('/v1/embeddings', json={'input': 'document'})
        assert response.status_code == 500
        assert response.json()['detail'] == 'embedding_backend_response_invalid'


def test_sentence_transformers_does_not_apply_implicit_query_prompt(monkeypatch):
    seen = []

    class Model:
        def __init__(self, model, **kwargs):
            pass

        def get_sentence_embedding_dimension(self):
            return 3

        def encode(self, texts, **kwargs):
            seen.append((texts, kwargs))
            return np.ones((len(texts), 3))

    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=Model))
    server.SentenceTransformersBackend('stub-model').encode(['document', 'Instruct: task\nQuery: query'])
    assert seen[0][1]['prompt'] == ''


def test_auto_backend_preserves_real_model_load_failure(monkeypatch):
    fallback_calls = []

    def broken(*args, **kwargs):
        raise RuntimeError('device_out_of_memory')

    monkeypatch.setattr(server, 'SentenceTransformersBackend', broken)
    monkeypatch.setattr(server, 'TransformersLastTokenBackend', lambda *a, **kw: fallback_calls.append(a))
    with pytest.raises(RuntimeError, match='device_out_of_memory'):
        server.load_local_backend('stub-model')
    assert not fallback_calls


def _response(data):
    return httpx.Response(200, json={'data': data}, request=httpx.Request('POST', 'http://localhost/v1/embeddings'))


@pytest.mark.parametrize('data', [
    [{'index': 0, 'embedding': [0., 0.]}],
    [{'index': 1, 'embedding': [1., 2.]}],
    [{'index': 0, 'embedding': []}],
    [{'index': 0, 'embedding': [1., 2.]}, {'index': 0, 'embedding': [1., 2.]}],
])
def test_client_rejects_bad_response_without_recording_detected_dim(monkeypatch, data):
    monkeypatch.setattr(httpx, 'post', lambda *args, **kwargs: _response(data))
    embedder = OpenAIEmbedder(base_url='http://localhost/v1', model='stub-model')
    with pytest.raises(LightGraphError, match='embedding_response_invalid'):
        embedder.embed(['document'])
    assert embedder.dim is None


def test_client_keeps_fingerprint_stable_and_normalizes_large_finite_vectors(monkeypatch):
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: _response([{'index': 0, 'embedding': [3e38, 3e38]}]))
    embedder = OpenAIEmbedder(base_url='http://localhost/v1', model='stub-model')
    fingerprint = embedder_fingerprint(embedder)
    vectors = embedder.embed(['document'])
    assert vectors.dtype == np.float32
    assert np.linalg.norm(vectors[0]) == pytest.approx(1.0)
    assert embedder.dim == 2
    assert embedder_fingerprint(embedder) == fingerprint
    assert embedder_fingerprint(OpenAIEmbedder(base_url=embedder.base_url, model=embedder.model)) == fingerprint


def test_new_and_legacy_lexical_indexes_never_generate_or_search_hash_vectors(tmp_path):
    import json
    from lightgraph.retrieval import SubgraphRetriever, resolve_embedder
    from lightgraph.runtime.config import EmbeddingConfig

    settings = EmbeddingConfig(provider='local_hash')
    assert settings.provider == 'none'
    assert resolve_embedder(SimpleNamespace(embedding=settings)) is None
    graph = {'support_visibility_version': 1,
             'nodes': [{'id': 'n1', 'type': 'Item', 'identity': {'code': 'A1'},
                        'supports': [{'scope_ref': 'public', 'label': '采购', 'properties': {}}]}],
             'edges': []}
    result = sync_graph_to_retrieval(graph, tmp_path, generation='generation')
    assert result['indexed'] and not result['vectorized']
    assert result['vector_rows'] == result['vector_dim'] == result['vector_cards'] == 0
    path, manifest = active_index(tmp_path, 'generation')
    assert not (path / 'vectors').exists()
    reader = SubgraphRetriever(tmp_path, expected_generation='generation')
    assert reader.retrieve('A1', hops=0)['nodes'][0]['id'] == 'n1'
    assert not reader.retrieve('完全无关的随机短语')['nodes']
    with pytest.raises(LightGraphError, match='embedding_not_configured'):
        reader.retrieve('采购', method='vector')

    # Old format 6 already contains scoped terms and topology. Only the matrix
    # is obsolete. A fresh reader can reuse SQLite without mutating the build.
    manifest.update(format=6, embedder_provider='local_hash', embedder_model='local_hash',
                    embedder=digest('lightgraph.retrieval.vector_local', 'LocalHashEmbedder', 256, None, None),
                    vector_dim=256)
    pointer = tmp_path / 'current.json'
    pointer.write_text(json.dumps(manifest))
    old = SubgraphRetriever(tmp_path, expected_generation='generation')
    assert not old.vector_dim and old.vectors is None
    assert [n['id'] for n in old.retrieve('采购')['nodes']] == ['n1']
    assert not old.retrieve('无关的字符串')['nodes']
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        SubgraphRetriever(tmp_path, expected_generation='different')
    assert json.loads(pointer.read_text()) == manifest
    for missing in ('generation', 'embedder_provider', 'embedder', 'cohorts', 'cohort_contexts'):
        pointer.write_text(json.dumps({k: v for k, v in manifest.items() if k != missing}))
        with pytest.raises(LightGraphError, match='retrieval_index_stale'):
            SubgraphRetriever(tmp_path)


def test_manual_rebuild_replaces_vectors_after_service_changes_in_place(tmp_path, monkeypatch):
    graph = {
        'support_visibility_version': 1,
        'nodes': [{'id': 'n1', 'type': 'Entity', 'identity': {'id': '1'},
                   'supports': [{'scope_ref': 'public', 'label': 'item', 'properties': {}, 'provenance': {}}]}],
        'edges': [],
    }
    response_vectors = [[1., 0.]]
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: _response([
        {'index': i, 'embedding': response_vectors[0]} for i, _ in enumerate(kw['json']['input'])
    ]))
    make_embedder = lambda: OpenAIEmbedder(base_url='http://localhost/v1', model='stub-model')
    old = sync_graph_to_retrieval(graph, tmp_path, embedder=make_embedder())
    old_root, _ = active_index(tmp_path)
    response_vectors[0] = [0., 1., 0.]
    new = sync_graph_to_retrieval(graph, tmp_path, embedder=make_embedder())
    new_root, manifest = active_index(tmp_path)
    assert old['generation'] == new['generation']
    assert old['build'] != new['build']
    # This node contributes a combined card and a name vector.
    assert old['vector_rows'] == new['vector_rows'] == 2
    assert np.load(old_root / 'vectors/vectors.npy').shape == (2, 2)
    assert np.load(new_root / 'vectors/vectors.npy').shape == (2, 3)
    assert manifest['vector_dim'] == 3


def test_server_token_and_batch_limits_apply_before_encoding(monkeypatch):
    backend = StubBackend()
    monkeypatch.setenv('LIGHTGRAPH_EMBEDDING_SERVER_KEY', 'only-for-test')
    with TestClient(server.create_embedding_app(backend), client=('127.0.0.1', 50123)) as client:
        assert client.post('/v1/embeddings', json={'input': 'a'}).status_code == 401
        headers = {'Authorization': 'Bearer only-for-test'}
        assert client.post('/v1/embeddings', headers=headers, json={'input': ['x'] * 513}).status_code == 422
        assert client.post('/v1/embeddings', headers=headers, json={'input': 'x' * 33000}).status_code == 422
        assert not backend.calls
        assert client.post('/v1/embeddings', headers=headers, json={'input': 'a'}).status_code == 200

"""Local-first hybrid vector + relational subgraph retrieval MVP."""
from .vector_local import (
    Embedder,
    LocalNumpyVectorStore,
    OpenAIEmbedder,
    resolve_embedder,
    test_embedding_service,
)
from .store_sqlite import SQLiteGraphIndex
from .sync import sync_graph_to_retrieval
from .retriever import QuerySlot, SubgraphRetriever, decompose_query

__all__ = [
    'Embedder',
    'LocalNumpyVectorStore',
    'OpenAIEmbedder',
    'QuerySlot',
    'SQLiteGraphIndex',
    'SubgraphRetriever',
    'decompose_query',
    'resolve_embedder',
    'sync_graph_to_retrieval',
    'test_embedding_service',
]

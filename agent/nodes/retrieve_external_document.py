"""External document retrieval entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.nodes.retrieve_document import retrieve_document
from agent.state import AgentState
from storage.storage_contracts import ChunkStore, VectorSearchIndex
from tools.embedder.embedder import VoyageEmbedder
from tools.retrieval.document_scope import EXTERNAL_SCOPE
from tools.retrieval.hybrid_retriever import KeywordChunkSearchIndex
from tools.retrieval.reranker import LeafReranker


def retrieve_external_document(
    state: AgentState,
    client: Any = None,
    model: str | None = None,
    vector_index: VectorSearchIndex | None = None,
    keyword_index: KeywordChunkSearchIndex | None = None,
    chunk_store: ChunkStore | None = None,
    query_embedder: VoyageEmbedder | None = None,
    doc_embedder: VoyageEmbedder | None = None,
    reranker: LeafReranker | None = None,
    limit: int = 10,
    neighbor_window: int = 1,
    min_relevance_score: float | None = None,
    *,
    timing_context: Mapping[str, object] | None = None,
) -> AgentState:
    return retrieve_document(
        state,
        client=client,
        model=model,
        vector_index=vector_index,
        keyword_index=keyword_index,
        chunk_store=chunk_store,
        query_embedder=query_embedder,
        doc_embedder=doc_embedder,
        reranker=reranker,
        limit=limit,
        document_scope=EXTERNAL_SCOPE,
        neighbor_window=neighbor_window,
        min_relevance_score=min_relevance_score,
        timing_context=timing_context,
    )

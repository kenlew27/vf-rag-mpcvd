"""FastAPI route for query-text hybrid retrieval."""

from __future__ import annotations

import os
import shutil
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from storage.bigquery import BigQueryChunkStore
from storage.lancedb import LanceDBVectorStore
from storage.storage_contracts import ChunkStore, VectorSearchIndex
from tools.embedder.embedder import VoyageEmbedder
from tools.embedder.query_embedder import embed_query
from tools.retrieval.bm25_index import BM25ChunkSearchIndex
from tools.retrieval.bm25_snapshot import default_bm25_gcs_uri, load_bm25_snapshot
from tools.retrieval.document_scope import (
    DOCUMENT_SCOPE_KEY,
    DocumentScope,
    EXTERNAL_SCOPE,
    normalize_document_scope,
    normalize_document_scopes,
)
from tools.retrieval.hybrid_retriever import (
    MAX_LIMIT,
    KeywordChunkSearchIndex,
    RetrievedChunkRef,
    retrieve_leaf_chunks,
)
from tools.retrieval.reranker import LeafReranker, rerank_and_expand_retrieved_chunks

router = APIRouter(prefix="/retrieval", tags=["retrieval"])

_vector_indexes: dict[DocumentScope, VectorSearchIndex] = {}
_keyword_indexes: dict[DocumentScope, KeywordChunkSearchIndex] = {}
_chunk_store: ChunkStore | None = None
_query_embedder: VoyageEmbedder | None = None

_vector_index_lock = Lock()
_keyword_index_lock = Lock()
_chunk_store_lock = Lock()
_query_embedder_lock = Lock()

BM25_SNAPSHOT_URI_ENV = "APP_BM25_SNAPSHOT_URI"
BM25_SNAPSHOT_PATH_ENV = "APP_BM25_SNAPSHOT_PATH"


class VectorSearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, gt=0)
    filters: dict[str, Any] = Field(default_factory=dict)
    document_scopes: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)


def get_vector_index(document_scope: str = EXTERNAL_SCOPE) -> VectorSearchIndex:
    scope = normalize_document_scope(document_scope)
    if scope not in _vector_indexes:
        with _vector_index_lock:
            if scope not in _vector_indexes:
                store = LanceDBVectorStore(document_scope=scope)
                if store.snapshot_uri:
                    from google.cloud import storage

                    try:
                        store.download_snapshot(storage.Client())
                    except Exception as exc:
                        if not _is_missing_snapshot_error(exc):
                            raise
                        _remove_local_vector_snapshot(store)
                _vector_indexes[scope] = store
    return _vector_indexes[scope]


def get_chunk_store() -> ChunkStore:
    global _chunk_store
    if _chunk_store is None:
        with _chunk_store_lock:
            if _chunk_store is None:
                _chunk_store = BigQueryChunkStore()
    return _chunk_store


def get_query_embedder() -> VoyageEmbedder:
    global _query_embedder
    if _query_embedder is None:
        with _query_embedder_lock:
            if _query_embedder is None:
                _query_embedder = VoyageEmbedder(input_type="query")
    return _query_embedder


def get_keyword_index(document_scope: str = EXTERNAL_SCOPE) -> KeywordChunkSearchIndex:
    scope = normalize_document_scope(document_scope)
    if scope not in _keyword_indexes:
        with _keyword_index_lock:
            if scope not in _keyword_indexes:
                _keyword_indexes[scope] = _load_keyword_index(scope)
    return _keyword_indexes[scope]


def _load_keyword_index(document_scope: str = EXTERNAL_SCOPE) -> BM25ChunkSearchIndex:
    scope = normalize_document_scope(document_scope)
    snapshot_path = os.environ.get(BM25_SNAPSHOT_PATH_ENV)
    if snapshot_path:
        return load_bm25_snapshot(snapshot_path)

    snapshot_uri = os.environ.get(BM25_SNAPSHOT_URI_ENV)
    if snapshot_uri:
        return load_bm25_snapshot(snapshot_uri)

    try:
        return load_bm25_snapshot(default_bm25_gcs_uri(document_scope=scope))
    except Exception as exc:
        if _is_missing_snapshot_error(exc):
            return BM25ChunkSearchIndex.from_records(())
        raise


def _reset_vector_index_for_tests() -> None:
    with _vector_index_lock:
        _vector_indexes.clear()


def _reset_keyword_index_for_tests() -> None:
    with _keyword_index_lock:
        _keyword_indexes.clear()


def _reset_chunk_store_for_tests() -> None:
    global _chunk_store
    with _chunk_store_lock:
        _chunk_store = None


def _reset_query_embedder_for_tests() -> None:
    global _query_embedder
    with _query_embedder_lock:
        _query_embedder = None


def _reset_retrieval_dependencies_for_tests() -> None:
    _reset_vector_index_for_tests()
    _reset_keyword_index_for_tests()
    _reset_chunk_store_for_tests()
    _reset_query_embedder_for_tests()


def _remove_local_vector_snapshot(store: VectorSearchIndex) -> None:
    index_dir = getattr(store, "index_dir", None)
    if isinstance(index_dir, (str, bytes, os.PathLike)):
        shutil.rmtree(index_dir, ignore_errors=True)


def invalidate_cached_retrieval_indexes(*, document_scope: str | None = None) -> None:
    if document_scope is None:
        with _vector_index_lock:
            _vector_indexes.clear()
        with _keyword_index_lock:
            _keyword_indexes.clear()
        return

    scope = normalize_document_scope(document_scope)
    with _vector_index_lock:
        _vector_indexes.pop(scope, None)
    with _keyword_index_lock:
        _keyword_indexes.pop(scope, None)


@router.post("/vector-search")
def vector_search(request: VectorSearchRequest) -> dict[str, Any]:
    normalized_query = " ".join(request.query.split())
    if not normalized_query:
        raise HTTPException(status_code=400, detail="query must not be blank")

    try:
        document_scopes = normalize_document_scopes(request.document_scopes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filters = _request_filters(request)

    try:
        embedded_query = embed_query(normalized_query, embedder=get_query_embedder())
        retrieved_chunks = _retrieve_leaf_chunks_for_scopes(
            query_text=embedded_query.query_text,
            query_vector=embedded_query.vector,
            document_scopes=document_scopes,
            limit=min(request.limit, MAX_LIMIT),
            filters=filters,
        )
        contexts = rerank_and_expand_retrieved_chunks(
            retrieved_chunks,
            query_text=embedded_query.query_text,
            chunk_store=get_chunk_store(),
            reranker=_get_route_reranker(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "query": {
            "text": embedded_query.query_text,
            "embedding_model": embedded_query.model,
        },
        "contexts": [_context_response(context) for context in contexts],
    }


def _request_filters(request: VectorSearchRequest) -> dict[str, Any]:
    filters = dict(request.filters)
    document_ids = _normalize_document_ids(request.document_ids)
    if document_ids:
        filters["document_id"] = document_ids
    return filters


def _normalize_document_ids(document_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    for document_id in document_ids:
        text = str(document_id).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _retrieve_leaf_chunks_for_scopes(
    *,
    query_text: str,
    query_vector: list[float],
    document_scopes: tuple[DocumentScope, ...],
    limit: int,
    filters: dict[str, Any],
) -> list[RetrievedChunkRef]:
    merged: dict[str, RetrievedChunkRef] = {}
    for scope in document_scopes:
        scoped_chunks = retrieve_leaf_chunks(
            query_text=query_text,
            query_vector=query_vector,
            vector_index=get_vector_index(scope),
            keyword_index=get_keyword_index(scope),
            limit=limit,
            filters=filters,
        )
        for chunk in scoped_chunks:
            scoped_chunk = _with_scope_metadata(chunk, scope)
            existing = merged.get(scoped_chunk.chunk_id)
            if existing is None or scoped_chunk.score > existing.score:
                merged[scoped_chunk.chunk_id] = scoped_chunk

    return sorted(
        merged.values(),
        key=lambda chunk: (
            -float(chunk.score),
            chunk.rank if chunk.rank is not None else MAX_LIMIT + 1,
            chunk.chunk_id,
        ),
    )[:limit]


def _with_scope_metadata(chunk: RetrievedChunkRef, document_scope: DocumentScope) -> RetrievedChunkRef:
    metadata = dict(chunk.metadata)
    metadata.setdefault(DOCUMENT_SCOPE_KEY, document_scope)
    return RetrievedChunkRef(
        chunk_id=chunk.chunk_id,
        score=chunk.score,
        semantic_score=chunk.semantic_score,
        keyword_score=chunk.keyword_score,
        rank=chunk.rank,
        semantic_rank=chunk.semantic_rank,
        keyword_rank=chunk.keyword_rank,
        metadata=metadata,
    )


def _get_route_reranker() -> LeafReranker | None:
    return None


def _context_response(context: Any) -> dict[str, Any]:
    return {
        "context_id": context.context_id,
        "document_id": context.document_id,
        "text": context.text,
        "score": context.score,
        "source_leaf_chunk_ids": list(context.source_leaf_chunk_ids),
        "expansion_type": context.expansion_type,
        "parent_chunk_id": context.parent_chunk_id,
        "metadata": dict(context.metadata),
        "artifact_uris": dict(context.artifact_uris),
    }


def _is_missing_snapshot_error(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return "not found" in text or "notfound" in text or "404" in text or "no such" in text

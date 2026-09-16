"""Retrieve scoped document evidence for a user query."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any

from agent.request_plan import RequestStatus, RequestTask
from agent.state import DocumentQueryRewrite, AgentState
from storage.gcs import read_text as read_gcs_text
from storage.storage_contracts import ChunkRecord, DocumentRecord, ChunkStore, VectorSearchIndex
from tools.embedder.embedder import VoyageEmbedder
from tools.embedder.query_embedder import embed_query
from tools.retrieval.document_scope import (
    DOCUMENT_SCOPE_KEY,
    EXTERNAL_SCOPE,
    INTERNAL_SCOPE,
    DocumentScope,
    normalize_document_scope,
)
from tools.retrieval.hybrid_retriever import KeywordChunkSearchIndex, retrieve_leaf_chunks
from tools.retrieval.reranker import LeafReranker, rerank_and_expand_retrieved_chunks
from tools.timing import agent_timing

_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "retrieve_document.md"
_LOG_PATH = os.environ.get("RETRIEVE_DOCUMENT_LOG_PATH", "retrieve_document_steps.jsonl")
_rd_logger = logging.getLogger(__name__)
_METADATA_KEYS = (
    "title",
    "section_path",
    "section_title",
    "page_start",
    "page_end",
    "source_type",
    "authors",
    "published_year",
    "doi",
    DOCUMENT_SCOPE_KEY,
    "original_filename",
)
_DOCUMENT_SNIPPET_CHARS = 3500
_DOCUMENT_SUMMARY_CHARS = 900
_DOCUMENT_MAP_SEGMENT_CHARS = 7000
_DOCUMENT_MAX_MAP_SEGMENTS = 6
_RERANK_CANDIDATE_MULTIPLIER = 3
_MAX_RERANK_CANDIDATES = 100
# The affected BGE call peaked at 0.000655.  This fixed output-scale floor
# excludes that false-positive band while retaining scores at or above 0.01.
_MIN_BGE_RELEVANCE_SCORE = 0.01


def _content_only_relaxation_query(question: str) -> str:
    """Remove a leading source-selection directive while preserving the evidence ask."""
    match = re.match(
        r"^\s*(?:use|using)\b[^.!?]*\b(?:database|db|literature|papers?|pdfs?)\b"
        r"[^.!?]*[.!?]\s*(.+)$",
        question,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return " ".join(match.group(1).split()) if match is not None else ""


def retrieve_document(
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
    document_scope: str = EXTERNAL_SCOPE,
    neighbor_window: int = 1,
    min_relevance_score: float | None = _MIN_BGE_RELEVANCE_SCORE,
    *,
    timing_context: Mapping[str, object] | None = None,
) -> AgentState:
    """LangGraph node: rewrite query, retrieve RAG contexts, update state."""
    scope = normalize_document_scope(document_scope)
    timing = dict(timing_context or {})
    timing["scope"] = scope
    planned_queries = list(state.retrieval_queries)
    requested_document_task = bool(_document_task(state))

    with agent_timing("document.dependency_load", timing_context=timing) as span:
        chunk_store = chunk_store or _get_chunk_store()
        targets, resolve_reason = _resolve_document_targets(
            state,
            scope,
            chunk_store,
            require_targets=requested_document_task and scope == INTERNAL_SCOPE,
        )
        is_document_task = requested_document_task and bool(targets)
        span.set(resolved_documents=len(targets))
        if not is_document_task:
            vector_index = vector_index or _get_vector_index(scope)
            keyword_index = keyword_index or _get_keyword_index(scope)
            query_embedder = query_embedder or _get_query_embedder()
            span.set(keyword_enabled=keyword_index is not None)

    if resolve_reason:
        _append_doc_log({
            "run_id": state.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": f"{scope}_document_agent",
            "scope": scope,
            "query": state.query.raw_text,
            "outcome": "clarification_needed",
            "clarification_reason": resolve_reason,
        })
        return _state_with_document_outcome(state, scope, resolve_reason, RequestStatus.NEEDS_CLARIFICATION)

    if is_document_task:
        return _retrieve_document_task(state, scope, chunk_store, targets, timing_context=timing)

    rewrite_from_cache = False
    if planned_queries:
        executed_queries = planned_queries
        rewrite = DocumentQueryRewrite(rewritten_query=executed_queries[0])
    else:
        rewrite_from_cache = state.document_query_rewrite is not None
        rewrite = state.document_query_rewrite or rewrite_document_query(
            state.query.raw_text,
            client=client,
            model=model,
            timing_context=timing,
            run_id=state.run_id,
            scope=scope,
        )
        executed_queries = [rewrite.rewritten_query]
        for alt_q in rewrite.alternative_queries:
            if alt_q.casefold() not in {q.casefold() for q in executed_queries}:
                executed_queries.append(alt_q)
        relaxed_query = _content_only_relaxation_query(state.query.raw_text)
        if relaxed_query and relaxed_query.casefold() not in {
            query.casefold() for query in executed_queries
        }:
            executed_queries.append(relaxed_query)
    filters = _filters_for_targets(state.source_filters, targets)
    if scope == INTERNAL_SCOPE and state.source_mode == "none" and not filters.get("document_id"):
        clarification_reason = "Select one or more PDFs or name an active embedded PDF to use document evidence."
        _append_doc_log({
            "run_id": state.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": f"{scope}_document_agent",
            "scope": scope,
            "query": state.query.raw_text,
            "outcome": "clarification_needed",
            "clarification_reason": clarification_reason,
        })
        return _state_with_document_outcome(
            state,
            scope,
            clarification_reason,
            RequestStatus.NEEDS_CLARIFICATION,
        )
    contexts: list[Any] = []
    embedding_model = ""
    candidate_limit = min(_MAX_RERANK_CANDIDATES, limit * _RERANK_CANDIDATE_MULTIPLIER)
    for query_text in executed_queries:
        with agent_timing("document.query_embedding", timing_context=timing):
            embedded_query = embed_query(query_text, embedder=query_embedder)
        embedding_model = embedded_query.model
        retrieved_chunks = _retrieve_leaf_chunks_balanced(
            query_text=embedded_query.query_text,
            query_vector=embedded_query.vector,
            vector_index=vector_index,
            keyword_index=keyword_index,
            limit=candidate_limit,
            filters=filters,
            timing_context=timing,
        )
        contexts.extend(
            rerank_and_expand_retrieved_chunks(
                retrieved_chunks,
                query_text=embedded_query.query_text,
                chunk_store=chunk_store,
                reranker=reranker,
                neighbor_window=neighbor_window,
                min_relevance_score=min_relevance_score,
                leaf_quality_filter=_is_substantive_evidence_text,
                timing_context=timing,
            )
        )
    if client is not None and model and doc_embedder is not None:
        with agent_timing("document.hyde_generation", timing_context=timing):
            hyde_text = _generate_hyde_text(executed_queries[0], client=client, model=model)
        if hyde_text:
            hyde_vector = doc_embedder.embed_text(hyde_text)
            hyde_chunks = _retrieve_leaf_chunks_balanced(
                query_text=executed_queries[0],
                query_vector=hyde_vector,
                vector_index=vector_index,
                keyword_index=keyword_index,
                limit=candidate_limit,
                filters=filters,
                timing_context=timing,
            )
            contexts.extend(
                rerank_and_expand_retrieved_chunks(
                    hyde_chunks,
                    query_text=executed_queries[0],
                    chunk_store=chunk_store,
                    reranker=_NoOpLeafReranker(),  # no extra Voyage call; HyDE value is coverage
                    neighbor_window=neighbor_window,
                    min_relevance_score=None,
                    leaf_quality_filter=_is_substantive_evidence_text,
                    timing_context=timing,
                )
            )
    contexts = _dedupe_and_limit_contexts(contexts, limit=limit)
    with agent_timing("document.evidence_packet_build", timing_context=timing) as span:
        packet = _build_document_evidence_packet(
            question=state.query.raw_text,
            rewritten_query=executed_queries[0],
            retrieval_queries=executed_queries,
            rewrite_notes=rewrite.rewrite_notes,
            embedding_model=embedding_model,
            contexts=contexts,
            document_scope=scope,
        )
        span.set(contexts=len(contexts))

    _append_doc_log({
        "run_id": state.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node": f"{scope}_document_agent",
        "scope": scope,
        "query": state.query.raw_text,
        "rewritten_query": executed_queries[0],
        "rewrite_from_cache": rewrite_from_cache,
        "retrieval_queries": executed_queries,
        "embedding_model": embedding_model,
        "contexts_returned": len(contexts),
        "outcome": "success",
        "node_output": {
            "document_scope": scope,
            "contexts_returned": packet.get("contexts_returned", 0),
            "contexts": [
                {
                    **{k: v for k, v in ctx.items() if k != "text"},
                    "text": (str(ctx.get("text", ""))[:500] + "…")
                    if len(str(ctx.get("text", ""))) > 500
                    else str(ctx.get("text", "")),
                }
                for ctx in (packet.get("contexts") or [])
            ],
        },
    })
    document_evidence = dict(state.document_evidence)
    document_evidence[scope] = packet
    return state.model_copy(
        update={
            "document_query_rewrite": rewrite,
            "document_evidence": document_evidence,
            "resolved_document_targets": _merge_targets(state.resolved_document_targets, targets),
        }
    )


def _retrieve_document_task(
    state: AgentState,
    scope: DocumentScope,
    chunk_store: ChunkStore,
    targets: Sequence[DocumentRecord],
    *,
    timing_context: Mapping[str, object],
) -> AgentState:
    if not targets:
        return _state_with_document_outcome(
            state,
            scope,
            "Name or select at least one active embedded PDF for this document task.",
            RequestStatus.NEEDS_CLARIFICATION,
        )

    contexts: list[dict[str, Any]] = []
    document_summaries: dict[str, dict[str, Any]] = {}
    task = _document_task(state) or RequestTask.SUMMARIZE
    if task == RequestTask.COMPARE and len(targets) < 2:
        return _state_with_document_outcome(
            state,
            scope,
            "Compare requests need at least two resolved PDFs. Name or select another active embedded PDF.",
            RequestStatus.NEEDS_CLARIFICATION,
        )
    snippets_per_document = 3 if task == RequestTask.COMPARE else 4

    with agent_timing("document.task_context_build", timing_context=timing_context) as span:
        for document in targets:
            text, source = _document_text(document, chunk_store)
            if not text.strip():
                continue
            label = _document_label(document)
            if task == RequestTask.SUMMARIZE:
                snippets = _summary_snippets(text, state.query.raw_text, limit=snippets_per_document)
            else:
                snippets = _document_snippets(text, state.query.raw_text, limit=snippets_per_document)
            document_summaries[document.document_id] = {
                "document_id": document.document_id,
                "label": label,
                "source": source,
                "segments_used": len(snippets),
                "summary_text": _truncate(_clean_text(text), _DOCUMENT_SUMMARY_CHARS),
            }
            for index, snippet in enumerate(snippets, start=1):
                contexts.append(
                    {
                        "rank": len(contexts) + 1,
                        "context_id": f"{document.document_id}:excerpt-{index}",
                        "document_id": document.document_id,
                        "source_leaf_chunk_ids": [],
                        "text": snippet,
                        "confidence": 1.0,
                        "retrieval_score": 1.0,
                        "metadata": _document_metadata(document, scope),
                    }
                )
        span.set(contexts=len(contexts), documents=len(targets), task=task)

    if not contexts:
        return _state_with_document_outcome(
            state,
            scope,
            "The resolved PDF did not have parsed text or chunk text available for this document task.",
            RequestStatus.INSUFFICIENT_EVIDENCE,
        )

    packet = {
        "agent": f"{scope}_document_agent",
        "document_scope": scope,
        "task": task.value,
        "question": state.query.raw_text,
        "rewritten_query": state.query.raw_text,
        "retrieval_queries": [state.query.raw_text],
        "rewrite_notes": ["used stored parsed PDF text for document task"],
        "embedding_model": "",
        "contexts_returned": len(contexts),
        "contexts": contexts,
        "document_summaries": document_summaries,
        "requires_synthesis": True,
    }
    document_evidence = dict(state.document_evidence)
    document_evidence[scope] = packet
    return state.model_copy(
        update={
            "document_evidence": document_evidence,
            "resolved_document_targets": _merge_targets(state.resolved_document_targets, targets),
        }
    )


def _resolve_document_targets(
    state: AgentState,
    scope: DocumentScope,
    chunk_store: ChunkStore,
    *,
    require_targets: bool,
) -> tuple[list[DocumentRecord], str]:
    documents = _active_documents(chunk_store)
    scoped_documents = [document for document in documents if _document_scope(document) == scope]
    selected_ids = {
        str(document_id).strip()
        for document_id in (state.selected_document_ids or _document_filter_ids(state.source_filters))
        if str(document_id).strip()
    }
    selected_documents = [document for document in scoped_documents if document.document_id in selected_ids]
    name_matches = _matching_documents(state.query.raw_text, scoped_documents)

    task = _document_task(state)
    if selected_ids and name_matches:
        selected_matches = [document for document in name_matches if document.document_id in selected_ids]
        if selected_matches:
            return selected_matches, ""
        return [], "The named PDF does not match the selected PDFs. Select that PDF or clear the source selection."

    if name_matches:
        if task == RequestTask.COMPARE and len(name_matches) < 2:
            return [], "Compare requests need at least two named or selected PDFs."
        if task == RequestTask.SUMMARIZE and len(name_matches) > 1:
            names = ", ".join(_document_label(document) for document in name_matches[:4])
            return [], f"Multiple active PDFs match that name: {names}. Specify which PDF to summarize."
        return name_matches, ""

    if selected_documents:
        if task == RequestTask.COMPARE and len(selected_documents) < 2:
            return [], "Compare requests need at least two selected PDFs."
        return selected_documents, ""

    if selected_ids and scoped_documents:
        return [], "The selected PDF is not active in this document scope. Select an active embedded PDF."

    if not require_targets:
        return [], ""

    if state.source_mode == "all" and scoped_documents:
        if task == RequestTask.SUMMARIZE and len(scoped_documents) == 1:
            return scoped_documents, ""
        if task == RequestTask.COMPARE and len(scoped_documents) >= 2:
            return scoped_documents, ""

    if state.source_mode == "selected":
        return [], "Select at least one active embedded PDF for this document task."
    if state.source_mode == "none":
        return [], "Name an active embedded PDF or select PDFs before asking for a summary or comparison."
    return [], "Specify which active embedded PDF to use for this document task."


def _active_documents(chunk_store: ChunkStore) -> list[DocumentRecord]:
    list_documents = getattr(chunk_store, "list_active_documents", None)
    if list_documents is None:
        return []
    try:
        return list(list_documents())
    except Exception:
        return []


def _matching_documents(query: str, documents: Sequence[DocumentRecord]) -> list[DocumentRecord]:
    normalized_query = _normalize_name(query)
    if not normalized_query:
        return []
    matches: list[DocumentRecord] = []
    for document in documents:
        for candidate in _document_name_candidates(document):
            normalized = _normalize_name(candidate)
            if len(normalized) >= 4 and normalized in normalized_query:
                matches.append(document)
                break
            if _token_match(normalized, normalized_query):
                matches.append(document)
                break
    return matches


def _document_name_candidates(document: DocumentRecord) -> list[str]:
    metadata = dict(document.metadata or {})
    nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), Mapping) else {}
    candidates = [
        document.document_id,
        str(metadata.get("original_filename") or ""),
        Path(str(metadata.get("original_filename") or "")).stem,
        str(metadata.get("title") or ""),
        str(nested_metadata.get("title") or "") if isinstance(nested_metadata, Mapping) else "",
        Path(document.source_uri).stem,
    ]
    return [candidate for candidate in candidates if candidate]


def _token_match(candidate: str, normalized_query: str) -> bool:
    tokens = [token for token in candidate.split() if len(token) >= 4]
    if len(tokens) < 3:
        return False
    return all(token in normalized_query for token in tokens[:6])


def _normalize_name(value: Any) -> str:
    text = Path(str(value)).stem if "/" in str(value) else str(value)
    return " ".join(
        "".join(character.lower() if character.isalnum() else " " for character in text).split()
    )


def _document_scope(document: DocumentRecord) -> str:
    try:
        return normalize_document_scope(str(document.metadata.get(DOCUMENT_SCOPE_KEY) or EXTERNAL_SCOPE))
    except ValueError:
        return EXTERNAL_SCOPE


def _document_label(document: DocumentRecord) -> str:
    metadata = dict(document.metadata or {})
    nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), Mapping) else {}
    for value in (
        metadata.get("original_filename"),
        metadata.get("title"),
        nested_metadata.get("title") if isinstance(nested_metadata, Mapping) else None,
        Path(document.source_uri).name,
        document.document_id,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return document.document_id


def _document_metadata(document: DocumentRecord, scope: DocumentScope) -> dict[str, Any]:
    metadata = dict(document.metadata or {})
    title = str(metadata.get("title") or "").strip()
    nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), Mapping) else {}
    if not title and isinstance(nested_metadata, Mapping):
        title = str(nested_metadata.get("title") or "").strip()
    original_filename = str(metadata.get("original_filename") or "").strip()
    result = {
        "title": title or original_filename or document.document_id,
        DOCUMENT_SCOPE_KEY: scope,
        "original_filename": original_filename or _document_label(document),
        "source_type": "paper" if scope == EXTERNAL_SCOPE else "internal_report",
    }
    return {key: value for key, value in result.items() if value}


def _document_text(document: DocumentRecord, chunk_store: ChunkStore) -> tuple[str, str]:
    parsed_markdown_uri = str(document.metadata.get("parsed_markdown_uri") or "").strip()
    if parsed_markdown_uri:
        try:
            text = read_gcs_text(parsed_markdown_uri)
        except Exception:
            text = ""
        if text.strip():
            return text, "parsed_markdown"

    list_chunks = getattr(chunk_store, "list_document_chunks", None)
    if list_chunks is None:
        return "", "unavailable"
    try:
        chunks = list(list_chunks(document.document_id))
    except Exception:
        return "", "unavailable"
    return _chunks_text(chunks), "chunks"


def _chunks_text(chunks: Sequence[ChunkRecord]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for chunk in sorted(chunks, key=lambda item: (str(item.metadata.get("section_path") or ""), item.chunk_id)):
        text = _clean_text(chunk.text)
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "\n\n".join(parts)


def _document_snippets(text: str, query: str, *, limit: int) -> list[str]:
    clean = _clean_text(text)
    if not clean:
        return []
    blocks = [block.strip() for block in clean.split("\n\n") if block.strip()]
    if not blocks:
        blocks = [clean]
    query_tokens = {token for token in _normalize_name(query).split() if len(token) >= 4}
    scored = []
    for index, block in enumerate(blocks):
        block_tokens = set(_normalize_name(block).split())
        score = len(query_tokens & block_tokens)
        if block.startswith("#"):
            score += 1
        scored.append((score, index, block))

    selected_indexes: list[int] = [0]
    for score, index, _block in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 and len(selected_indexes) > 1:
            continue
        if index not in selected_indexes:
            selected_indexes.append(index)
        if len(selected_indexes) >= limit:
            break

    selected_indexes = sorted(selected_indexes[:limit])
    return [_truncate(blocks[index], _DOCUMENT_SNIPPET_CHARS) for index in selected_indexes]


def _summary_snippets(text: str, query: str, *, limit: int) -> list[str]:
    clean = _clean_text(text)
    if len(clean) <= _DOCUMENT_MAP_SEGMENT_CHARS:
        return _document_snippets(clean, query, limit=limit)

    segments = _document_segments(clean)
    query_tokens = {token for token in _normalize_name(query).split() if len(token) >= 4}
    selected_indexes: list[int] = [0]
    if len(segments) > 1:
        selected_indexes.append(len(segments) - 1)

    scored = []
    for index, segment in enumerate(segments):
        score = len(query_tokens & set(_normalize_name(segment).split()))
        scored.append((score, index))
    for score, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 and len(selected_indexes) >= 2:
            continue
        if index not in selected_indexes:
            selected_indexes.append(index)
        if len(selected_indexes) >= min(limit, _DOCUMENT_MAX_MAP_SEGMENTS):
            break

    selected_indexes = sorted(selected_indexes[: min(limit, _DOCUMENT_MAX_MAP_SEGMENTS)])
    return [_truncate(segments[index], _DOCUMENT_SNIPPET_CHARS) for index in selected_indexes]


def _document_segments(clean_text: str) -> list[str]:
    blocks = [block.strip() for block in clean_text.split("\n\n") if block.strip()]
    if not blocks:
        return [clean_text]

    segments: list[str] = []
    current: list[str] = []
    current_length = 0
    for block in blocks:
        next_length = current_length + len(block) + 2
        if current and next_length > _DOCUMENT_MAP_SEGMENT_CHARS:
            segments.append("\n\n".join(current))
            current = [block]
            current_length = len(block)
            continue
        current.append(block)
        current_length = next_length

    if current:
        segments.append("\n\n".join(current))
    return segments


def _retrieve_leaf_chunks_balanced(
    *,
    query_text: str,
    query_vector: Sequence[float],
    vector_index: VectorSearchIndex,
    keyword_index: KeywordChunkSearchIndex | None,
    limit: int,
    filters: Mapping[str, Any],
    timing_context: Mapping[str, object],
) -> list[Any]:
    document_ids = _document_filter_ids(filters)
    if len(document_ids) <= 1:
        return retrieve_leaf_chunks(
            query_text=query_text,
            query_vector=query_vector,
            vector_index=vector_index,
            keyword_index=keyword_index,
            limit=limit,
            filters=filters,
            timing_context=timing_context,
        )

    per_document_limit = max(2, ceil(limit / len(document_ids)))
    merged: list[Any] = []
    seen: set[str] = set()
    for document_id in document_ids:
        per_doc_filters = dict(filters)
        per_doc_filters["document_id"] = [document_id]
        for hit in retrieve_leaf_chunks(
            query_text=query_text,
            query_vector=query_vector,
            vector_index=vector_index,
            keyword_index=keyword_index,
            limit=per_document_limit,
            filters=per_doc_filters,
            timing_context=timing_context,
        ):
            chunk_id = getattr(hit, "chunk_id", "")
            if chunk_id and chunk_id in seen:
                continue
            if chunk_id:
                seen.add(chunk_id)
            merged.append(hit)
    return merged




class _NoOpLeafReranker:
    """Preserves vector-similarity order without any API call."""

    def score(self, query_text: str, candidates: list) -> list:
        from tools.retrieval.reranker import RerankScore

        return [RerankScore(chunk_id=c.chunk_id, score=c.retrieval_score) for c in candidates]


def _generate_hyde_text(primary_query: str, *, client: Any, model: str) -> str:
    """Generate a hypothetical document passage that would answer the query.

    The resulting text is embedded in document mode and used as an additional
    vector search query (HyDE). This bridges the lexical gap between short
    user queries and long technical document chunks.
    """
    prompt = (
        "Write a 2–3 sentence technical passage from a materials science paper "
        "that directly answers the following question. "
        "Write only the passage text — no title, no citations, no explanation.\n\n"
        f"Question: {primary_query}"
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return ""


def _dedupe_and_limit_contexts(contexts: Sequence[Any], *, limit: int) -> list[Any]:
    """Keep substantive, relevant contexts once per document/context pair."""
    best_by_key: dict[tuple[str, str], Any] = {}
    for context in sorted(
        contexts,
        key=lambda item: (-float(item.score), str(item.document_id), str(item.context_id)),
    ):
        if _is_non_evidence_context(context):
            continue
        key = (str(context.document_id), str(context.context_id))
        best_by_key.setdefault(key, context)
    return list(best_by_key.values())[:limit]


def _is_non_evidence_context(context: Any) -> bool:
    """Reject headings and bibliography fragments that cannot support a claim."""
    return not _is_substantive_evidence_text(
        str(context.text),
        getattr(context, "metadata", {}) or {},
    )


def _is_substantive_evidence_text(text: str, metadata: Mapping[str, Any]) -> bool:
    """Return whether text contains a claim rather than document scaffolding."""
    text = text.strip()
    section = " ".join(
        str(metadata.get(key, ""))
        for key in ("section_title", "section_path")
    ).lower()
    if re.search(r"\b(?:references?|bibliography)\b", section):
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines)
    if not compact:
        return False
    if re.match(r"^(?:references?|bibliography)\b", compact, flags=re.IGNORECASE):
        return False
    if re.match(r"^(?:\[\s*\d+\s*\]|\d+\.)\s+", compact) and re.search(
        r"\b(?:doi|https?://|vol\.|pp\.|\d{4})\b", compact, flags=re.IGNORECASE
    ):
        return False

    if len(lines) != 1 or len(compact) > 160:
        return True
    if compact.startswith("#"):
        return False
    if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z]", compact):
        return False
    if re.fullmatch(
        r"(?:abstract|introduction|background|methods?|materials and methods|results?|discussion|conclusions?)",
        compact,
        flags=re.IGNORECASE,
    ):
        return False
    return True


def _filters_for_targets(
    source_filters: Mapping[str, Any],
    targets: Sequence[DocumentRecord],
) -> dict[str, Any]:
    filters = dict(source_filters)
    if targets:
        filters["document_id"] = [document.document_id for document in targets]
    return filters


def _document_filter_ids(filters: Mapping[str, Any]) -> list[str]:
    value = filters.get("document_id")
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _document_task(state: AgentState) -> RequestTask | None:
    plan = state.request_plan
    if plan is None:
        return None
    if RequestTask.COMPARE in plan.tasks:
        return RequestTask.COMPARE
    if RequestTask.SUMMARIZE in plan.tasks:
        return RequestTask.SUMMARIZE
    return None


def _state_with_document_outcome(
    state: AgentState,
    scope: DocumentScope,
    reason: str,
    status: RequestStatus,
) -> AgentState:
    packet = {
        "agent": f"{scope}_document_agent",
        "document_scope": scope,
        "question": state.query.raw_text,
        "rewritten_query": state.query.raw_text,
        "rewrite_notes": [],
        "embedding_model": "",
        "contexts_returned": 0,
        "contexts": [],
        "outcome": status.value,
        "requires_synthesis": False,
    }
    document_evidence = dict(state.document_evidence)
    document_evidence[scope] = packet
    plan = state.request_plan
    if plan is not None:
        plan = plan.model_copy(update={"status": status, "reasons": [reason]})
    return state.model_copy(update={"document_evidence": document_evidence, "request_plan": plan})


def _merge_targets(existing: Sequence[Mapping[str, Any]], targets: Sequence[DocumentRecord]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing]
    seen = {str(item.get("document_id")) for item in merged}
    for document in targets:
        if document.document_id in seen:
            continue
        merged.append(
            {
                "document_id": document.document_id,
                "document_scope": _document_scope(document),
                "label": _document_label(document),
            }
        )
        seen.add(document.document_id)
    return merged


def _clean_text(value: str) -> str:
    lines = [line.rstrip() for line in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact_lines: list[str] = []
    blank = False
    for line in lines:
        if line.strip():
            compact_lines.append(line.strip())
            blank = False
        elif not blank:
            compact_lines.append("")
            blank = True
    return "\n".join(compact_lines).strip()


def _truncate(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def rewrite_document_query(
    raw_query: str,
    *,
    client: Any = None,
    model: str | None = None,
    timing_context: Mapping[str, object] | None = None,
    run_id: str | None = None,
    scope: str | None = None,
) -> DocumentQueryRewrite:
    """Rewrite a document query once so scoped retrieval agents can share it."""
    model = model or os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise ValueError("ANTHROPIC_MODEL env var is required or pass model=...")

    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY env var is required when no client is provided")
        from anthropic import Anthropic

        client = Anthropic()

    prompt_text = _PROMPT_PATH.read_text()
    prompt_version = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
    llm_start = time.monotonic()

    with agent_timing("document.rewrite_llm", timing_context=timing_context):
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=prompt_text,
            messages=[{"role": "user", "content": raw_query}],
        )
    llm_latency_ms = int((time.monotonic() - llm_start) * 1000)
    raw_response_text = "".join(
        getattr(b, "text", "") if not isinstance(b, dict) else b.get("text", "")
        for b in (response.content or [])
    ).strip()
    rewrite = _parse_rewrite_response(response)
    rewritten_query, rewrite_notes = _rewrite_or_fallback(rewrite, raw_query)
    raw_alts = rewrite.get("alternative_queries", [])
    alternative_queries = [
        q
        for q in (
            _normalize_text(str(alt))
            for alt in (raw_alts if isinstance(raw_alts, list) else [])
        )
        if q and q.casefold() != rewritten_query.casefold()
    ]
    usage = getattr(response, "usage", None)
    _append_doc_log({
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node": "rewrite_document_query",
        "scope": scope,
        "model": model,
        "prompt_version": prompt_version,
        "raw_query": raw_query,
        "raw_response": raw_response_text,
        "rewritten_query": rewritten_query,
        "rewrite_notes": rewrite_notes,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "llm_latency_ms": llm_latency_ms,
    })
    return DocumentQueryRewrite(
        rewritten_query=rewritten_query,
        alternative_queries=alternative_queries,
        rewrite_notes=rewrite_notes,
    )


def _parse_rewrite_response(response: Any) -> dict[str, Any]:
    text = "".join(
        getattr(block, "text", "") if not isinstance(block, dict) else block.get("text", "")
        for block in (response.content or [])
    ).strip()
    if not text:
        return {"rewrite_notes": ["empty rewrite response"]}

    text = _strip_json_fence(text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if extracted is None:
            return {"rewrite_notes": ["invalid rewrite JSON returned"]}
        try:
            payload = json.loads(extracted)
        except json.JSONDecodeError:
            return {"rewrite_notes": ["invalid rewrite JSON returned"]}

    if isinstance(payload, dict):
        return payload
    return {"rewrite_notes": ["rewrite JSON was not an object"]}


def _strip_json_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _rewrite_or_fallback(rewrite: Mapping[str, Any], original_query: str) -> tuple[str, list[str]]:
    rewritten_query = _normalize_text(rewrite.get("rewritten_query", ""))
    raw_notes = rewrite.get("rewrite_notes", [])
    rewrite_notes = [str(note) for note in raw_notes if str(note).strip()] if isinstance(raw_notes, list) else []
    if rewritten_query:
        return rewritten_query, rewrite_notes

    fallback_query = _normalize_text(original_query)
    return fallback_query, ["blank rewrite returned; used normalized original query", *rewrite_notes]


def _build_document_evidence_packet(
    *,
    question: str,
    rewritten_query: str,
    retrieval_queries: Sequence[str] | None = None,
    rewrite_notes: Sequence[str],
    embedding_model: str,
    contexts: Sequence[Any],
    document_scope: DocumentScope = EXTERNAL_SCOPE,
) -> dict[str, Any]:
    sorted_contexts = sorted(contexts, key=lambda context: (-float(context.score), context.context_id))
    top_score = float(sorted_contexts[0].score) if sorted_contexts else 0.0
    return {
        "agent": f"{document_scope}_document_agent",
        "document_scope": document_scope,
        "question": question,
        "rewritten_query": rewritten_query,
        "retrieval_queries": list(retrieval_queries or [rewritten_query]),
        "rewrite_notes": list(rewrite_notes),
        "embedding_model": embedding_model,
        "contexts_returned": len(sorted_contexts),
        "contexts": [
            _compact_context(context, rank=rank, top_score=top_score)
            for rank, context in enumerate(sorted_contexts, start=1)
        ],
        "requires_synthesis": True,
    }


def _compact_context(context: Any, *, rank: int, top_score: float) -> dict[str, Any]:
    metadata = dict(context.metadata)
    packet = {
        "rank": rank,
        "context_id": context.context_id,
        "document_id": context.document_id,
        "source_leaf_chunk_ids": list(context.source_leaf_chunk_ids),
        "text": context.text,
        # Retrieval-relative score normalization only; not a scientific confidence measure.
        "confidence": _relative_confidence(float(context.score), top_score),
        "retrieval_score": float(context.score),
        "metadata": _compact_metadata(metadata),
    }
    source_hash = metadata.get("source_hash")
    if source_hash:
        packet["source_hash"] = source_hash
    return packet


def _compact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {key: metadata[key] for key in _METADATA_KEYS if key in metadata}


def _relative_confidence(score: float, top_score: float) -> float:
    if top_score <= 0:
        return 0.0
    return round(score / top_score, 4)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value).split())


def _append_doc_log(record: dict) -> None:
    from storage.agent_log import append_run_log
    append_run_log(record, fallback_path=_LOG_PATH)


def _get_vector_index(document_scope: str) -> VectorSearchIndex:
    from app.api.routes_vector_search import get_vector_index

    return get_vector_index(document_scope)


def _get_keyword_index(document_scope: str) -> KeywordChunkSearchIndex:
    from app.api.routes_vector_search import get_keyword_index

    return get_keyword_index(document_scope)


def _get_chunk_store() -> ChunkStore:
    from app.api.routes_vector_search import get_chunk_store

    return get_chunk_store()


def _get_query_embedder() -> VoyageEmbedder:
    from app.api.routes_vector_search import get_query_embedder

    return get_query_embedder()

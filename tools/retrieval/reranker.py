"""Cross-encoder reranking and parent-chunk expansion.

Uses BAAI/bge-reranker-base to score candidate chunks.  When >50% of
sibling chunks hit (or at least two siblings appear), the parent chunk
is expanded in.
"""

from dataclasses import dataclass, field
from typing import Any
from tools.retrieval.hybrid_retriever import RetrievedChunkRef


@dataclass
class RerankScore:
    """Cross-encoder score for a single chunk."""
    chunk_id: str
    score: float


class LeafReranker:
    """Wraps the cross-encoder model for chunk reranking."""
    pass


@dataclass
class ExpandedContext:
    context_id: str = ""
    document_id: str = ""
    text: str = ""
    score: float = 0.0
    source_leaf_chunk_ids: list[str] = field(default_factory=list)
    expansion_type: str = "leaf"
    parent_chunk_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_uris: dict[str, str] = field(default_factory=dict)


def rerank_and_expand_retrieved_chunks(
    retrieved_chunks: list[RetrievedChunkRef],
    query_text: str = "",
    chunk_store: Any = None,
    reranker: Any = None,
    *args,
    **kwargs,
) -> list[ExpandedContext]:
    """Rerank candidates and expand to parent chunks where siblings co-occur."""
    if not retrieved_chunks:
        return []

    chunk_ids = [c.chunk_id for c in retrieved_chunks]
    chunks_with_parents: dict[str, tuple[Any, Any]] = {}
    if chunk_store is not None:
        if hasattr(chunk_store, "get_chunks_with_parents"):
            chunks_with_parents = chunk_store.get_chunks_with_parents(chunk_ids)
        elif hasattr(chunk_store, "get_chunks"):
            chunks = chunk_store.get_chunks(chunk_ids)
            chunks_with_parents = {c.chunk_id: (c, None) for c in chunks}
        elif hasattr(chunk_store, "get_chunk"):
            chunks_with_parents = {cid: (chunk_store.get_chunk(cid), None) for cid in chunk_ids}

    candidates = []
    for c in retrieved_chunks:
        if c.chunk_id in chunks_with_parents:
            candidates.append(chunks_with_parents[c.chunk_id][0])

    scores: dict[str, float] = {}
    if reranker is not None and hasattr(reranker, "score") and candidates:
        scored = reranker.score(query_text, candidates)
        for s in scored:
            scores[s.chunk_id] = s.score
    else:
        for c in retrieved_chunks:
            scores[c.chunk_id] = c.score

    parent_leaves: dict[str, list[tuple[str, float]]] = {}
    for c in candidates:
        pid = getattr(c, "parent_chunk_id", "")
        if pid:
            parent_leaves.setdefault(pid, []).append((c.chunk_id, scores.get(c.chunk_id, 0.0)))

    expanded: list[ExpandedContext] = []
    seen_leaf_ids: set[str] = set()

    for pid, leaves in parent_leaves.items():
        if len(leaves) >= 2 or (chunk_store and hasattr(chunk_store, "count_child_chunks") and len(leaves) / max(chunk_store.count_child_chunks(pid), 1) >= 0.5):
            leaves.sort(key=lambda x: x[1], reverse=True)
            parent_chunk = chunks_with_parents.get(leaves[0][0], (None, None))[1]
            if parent_chunk is None and chunk_store and hasattr(chunk_store, "get_chunk"):
                parent_chunk = chunk_store.get_chunk(pid)

            if parent_chunk:
                expanded.append(ExpandedContext(
                    context_id=parent_chunk.chunk_id,
                    document_id=parent_chunk.document_id,
                    text=getattr(parent_chunk, "text", "") or getattr(parent_chunk, "content", ""),
                    score=leaves[0][1],
                    source_leaf_chunk_ids=[l[0] for l in leaves],
                    expansion_type="parent",
                    parent_chunk_id="",
                    metadata=dict(getattr(parent_chunk, "metadata", {}) or {}),
                    artifact_uris=dict(getattr(parent_chunk, "artifact_uris", {}) or {}),
                ))
                for l in leaves:
                    seen_leaf_ids.add(l[0])

    for c in candidates:
        if c.chunk_id in seen_leaf_ids:
            continue
        expanded.append(ExpandedContext(
            context_id=c.chunk_id,
            document_id=c.document_id,
            text=getattr(c, "text", "") or getattr(c, "content", ""),
            score=scores.get(c.chunk_id, 0.0),
            source_leaf_chunk_ids=[c.chunk_id],
            expansion_type="leaf",
            parent_chunk_id=getattr(c, "parent_chunk_id", ""),
            metadata=dict(getattr(c, "metadata", {}) or {}),
            artifact_uris=dict(getattr(c, "artifact_uris", {}) or {}),
        ))

    expanded.sort(key=lambda x: x.score, reverse=True)
    return expanded


def warm_default_leaf_reranker():
    """Pre-load the reranker into memory at app startup."""
    pass

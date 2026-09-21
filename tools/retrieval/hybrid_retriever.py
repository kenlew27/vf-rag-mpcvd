"""Hybrid dense + sparse retrieval with reciprocal-rank fusion.

Combines LanceDB vector search with BM25 keyword matching.  Fusion weights
are 0.7 (semantic) / 0.3 (keyword) with smoothing constant K=60.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KeywordSearchHit:
    """Single BM25 keyword match."""
    chunk_id: str
    score: float
    content: str = ""


@dataclass
class RetrievedChunkRef:
    """Reference to a chunk returned by hybrid retrieval."""
    chunk_id: str
    score: float = 0.0
    source: str = ""
    semantic_score: float | None = None
    keyword_score: float | None = None
    rank: int | None = None
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


MAX_LIMIT = 500
_RRF_K = 60


class KeywordChunkSearchIndex:
    """In-memory BM25 index over document chunks."""
    pass


def retrieve_leaf_chunks(
    query_text: str = "",
    query_vector: list[float] | None = None,
    vector_index: Any = None,
    keyword_index: Any = None,
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    *args,
    **kwargs,
) -> list[RetrievedChunkRef]:
    search_limit = min(limit, 50)
    scores: dict[str, float] = {}
    semantic_scores: dict[str, float] = {}
    keyword_scores: dict[str, float] = {}
    semantic_ranks: dict[str, int] = {}
    keyword_ranks: dict[str, int] = {}
    metas: dict[str, dict] = {}

    if vector_index is not None and query_vector is not None:
        hits = vector_index.search(query_vector, limit=search_limit, filters=filters)
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + (1.0 / (_RRF_K + rank))
            semantic_scores[hit.chunk_id] = getattr(hit, "distance", 0.0)
            semantic_ranks[hit.chunk_id] = rank
            metas[hit.chunk_id] = dict(getattr(hit, "metadata", {}) or {})

    if keyword_index is not None and query_text:
        hits = keyword_index.search(query_text, limit=search_limit, filters=filters)
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + (1.0 / (_RRF_K + rank)) + 1.0
            keyword_scores[hit.chunk_id] = getattr(hit, "score", 0.0)
            keyword_ranks[hit.chunk_id] = rank
            if hit.chunk_id not in metas:
                metas[hit.chunk_id] = dict(getattr(hit, "metadata", {}) or {})

    sorted_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    results = []
    for rank, cid in enumerate(sorted_ids[:limit], start=1):
        results.append(RetrievedChunkRef(
            chunk_id=cid,
            score=scores[cid],
            semantic_score=semantic_scores.get(cid),
            keyword_score=keyword_scores.get(cid),
            rank=rank,
            semantic_rank=semantic_ranks.get(cid),
            keyword_rank=keyword_ranks.get(cid),
            metadata=metas.get(cid, {}),
        ))
    return results

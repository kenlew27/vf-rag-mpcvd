"""Cross-encoder reranking and parent-chunk expansion.

Uses BAAI/bge-reranker-base to score candidate chunks.  When >50% of
sibling chunks hit (or at least two siblings appear), the parent chunk
is expanded in.
"""

from dataclasses import dataclass


@dataclass
class RerankScore:
    """Cross-encoder score for a single chunk."""
    chunk_id: str
    score: float


class LeafReranker:
    """Wraps the cross-encoder model for chunk reranking."""
    pass


def rerank_and_expand_retrieved_chunks(*args, **kwargs):
    """Rerank candidates and expand to parent chunks where siblings co-occur."""
    raise NotImplementedError("requires the cross-encoder model weights")


def warm_default_leaf_reranker():
    """Pre-load the reranker into memory at app startup."""

"""Hybrid dense + sparse retrieval with reciprocal-rank fusion.

Combines LanceDB vector search with BM25 keyword matching.  Fusion weights
are 0.7 (semantic) / 0.3 (keyword) with smoothing constant K=60.
"""

from dataclasses import dataclass


@dataclass
class KeywordSearchHit:
    """Single BM25 keyword match."""
    chunk_id: str
    score: float
    content: str = ""


class KeywordChunkSearchIndex:
    """In-memory BM25 index over document chunks."""
    pass


def retrieve_leaf_chunks(*args, **kwargs):
    """Run hybrid retrieval and return fused leaf chunks."""
    raise NotImplementedError("requires deployed LanceDB and BM25 indices")

"""Abstract storage interfaces for chunks and vector indices."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ChunkRecord:
    """A single document chunk with its metadata."""
    chunk_id: str = ""
    document_id: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentRecord:
    """Top-level document metadata."""
    document_id: str = ""
    title: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ChunkStore(Protocol):
    """Read interface for the chunk store (PostgreSQL or BigQuery)."""
    def get_chunk(self, chunk_id: str) -> ChunkRecord: ...
    def get_chunks(self, chunk_ids: list[str]) -> list[ChunkRecord]: ...


@dataclass
class VectorSearchHit:
    """Single result from a vector similarity search."""
    chunk_id: str = ""
    distance: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorSearchIndex(Protocol):
    """Read interface for the LanceDB vector index."""
    def search(self, query_vector: list[float], top_k: int = 5) -> list[ChunkRecord]: ...

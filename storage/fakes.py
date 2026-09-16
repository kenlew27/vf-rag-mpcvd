"""In-memory fakes for testing."""

from dataclasses import dataclass, field
from typing import Any

from storage.storage_contracts import ChunkRecord, DocumentRecord


class FakeChunkStore:
    """In-memory chunk store for unit tests."""

    def __init__(self, chunks: dict[str, ChunkRecord] | None = None):
        self._chunks = chunks or {}

    def get_chunk(self, chunk_id: str) -> ChunkRecord:
        return self._chunks.get(chunk_id, ChunkRecord(chunk_id=chunk_id))

    def get_chunks(self, chunk_ids: list[str]) -> list[ChunkRecord]:
        return [self.get_chunk(cid) for cid in chunk_ids]

"""In-memory fakes for testing."""

from dataclasses import dataclass, field
from typing import Any

from storage.storage_contracts import ChunkRecord, DocumentRecord


class FakeChunkStore:
    """In-memory chunk store for unit tests."""

    def __init__(self, chunks: dict[str, ChunkRecord] | None = None):
        self._chunks = chunks or {}
        self._documents: dict[str, DocumentRecord] = {}

    def get_chunk(self, chunk_id: str) -> ChunkRecord:
        return self._chunks.get(chunk_id, ChunkRecord(chunk_id=chunk_id))

    def get_chunks(self, chunk_ids: list[str]) -> list[ChunkRecord]:
        return [self.get_chunk(cid) for cid in chunk_ids]

    def put_document(self, document: DocumentRecord) -> None:
        self._documents[document.document_id] = document

    def get_document(self, document_id: str) -> DocumentRecord:
        if document_id not in self._documents:
            raise KeyError(document_id)
        return self._documents[document_id]

    def get_active_documents_by_filename_scope(self, *, original_filename: str, document_scope: str):
        return [
            d for d in self._documents.values()
            if d.metadata.get("original_filename") == original_filename
            and d.metadata.get("document_scope") == document_scope
        ]

    def list_active_documents(self):
        return list(self._documents.values())

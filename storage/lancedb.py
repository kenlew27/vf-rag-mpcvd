"""LanceDB vector store wrapper."""

from typing import Any
from storage.storage_contracts import ChunkRecord


class LanceDBVectorStore:
    """Manages a LanceDB table for vector similarity search."""

    def __init__(self, uri: str = "", table_name: str = "chunks", document_scope: Any = "external", *args, **kwargs):
        self.uri = uri
        self.table_name = table_name
        self.document_scope = document_scope
        self.snapshot_uri: str = ""

    def download_snapshot(self, client: Any = None) -> None:
        pass

    def search(self, query_vector: list[float], *, limit: int = 100, filters=None) -> list[ChunkRecord]:
        raise NotImplementedError("requires LanceDB index")

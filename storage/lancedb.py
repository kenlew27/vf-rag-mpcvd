"""LanceDB vector store wrapper."""

from storage.storage_contracts import ChunkRecord


class LanceDBVectorStore:
    """Manages a LanceDB table for vector similarity search."""

    def __init__(self, uri: str = "", table_name: str = "chunks"):
        self.uri = uri
        self.table_name = table_name
        self.snapshot_uri: str = ""

    def search(self, query_vector: list[float], *, limit: int = 100, filters=None) -> list[ChunkRecord]:
        raise NotImplementedError("requires LanceDB index")

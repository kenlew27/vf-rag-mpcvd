"""BigQuery-backed chunk store."""

from storage.storage_contracts import ChunkRecord, ChunkStore


class BigQueryChunkStore:
    """Reads document chunks from a BigQuery table."""

    def __init__(self, project: str = "", dataset: str = "", table: str = ""):
        self.project = project
        self.dataset = dataset
        self.table = table

    def get_chunk(self, chunk_id: str) -> ChunkRecord:
        raise NotImplementedError("requires BigQuery credentials")

    def get_chunks(self, chunk_ids: list[str]) -> list[ChunkRecord]:
        raise NotImplementedError("requires BigQuery credentials")

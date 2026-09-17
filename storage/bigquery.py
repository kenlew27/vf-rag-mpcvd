"""BigQuery-backed chunk store."""

from storage.storage_contracts import ChunkRecord, DocumentRecord


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

    def get_document(self, document_id: str) -> DocumentRecord:
        raise NotImplementedError("requires BigQuery credentials")

    def get_active_documents_by_filename_scope(self, *, original_filename: str, document_scope: str):
        raise NotImplementedError("requires BigQuery credentials")

    def list_active_documents(self):
        raise NotImplementedError("requires BigQuery credentials")

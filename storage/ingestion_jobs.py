"""Ingestion job tracking and persistence."""

from dataclasses import dataclass, field
from typing import Protocol

ACTIVE_JOB_STATUSES = ("queued", "processing", "running", "awaiting_worker")
DURABLE_REFERENCE_STATUSES = ("queued", "processing", "completed", "running")


@dataclass
class IngestionJob:
    """Tracks the lifecycle of a single document ingestion."""
    job_id: str = ""
    document_id: str = ""
    document_scope: str = "external"
    status: str = "queued"
    stage: str = "awaiting_worker"
    content_hash: str = ""
    raw_gcs_uri: str = ""
    staging_gcs_uri: str = ""
    owner_session_id: str = ""
    batch_id: str = ""
    original_filename: str = ""
    size_bytes: int = 0
    replaces_document_id: str | None = None
    error_message: str = ""
    metadata: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "document_id": self.document_id,
            "document_scope": self.document_scope,
            "status": self.status,
            "stage": self.stage,
            "content_hash": self.content_hash,
            "raw_gcs_uri": self.raw_gcs_uri,
            "staging_gcs_uri": self.staging_gcs_uri,
            "owner_session_id": self.owner_session_id,
            "batch_id": self.batch_id,
            "original_filename": self.original_filename,
            "size_bytes": self.size_bytes,
            "replaces_document_id": self.replaces_document_id,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


class IngestionJobStore(Protocol):
    """Persistent store for ingestion jobs."""
    def get(self, job_id: str) -> IngestionJob | None: ...
    def save(self, job: IngestionJob) -> None: ...
    def find_by_content_hash(self, content_hash: str, scope: str) -> IngestionJob | None: ...
    def find_by_document_id(self, document_id: str) -> list[IngestionJob]: ...


class PostgresIngestionJobStore:
    """PostgreSQL-backed ingestion job store."""

    def __init__(self, dsn: str = ""):
        self.dsn = dsn

    def get(self, job_id: str) -> IngestionJob | None:
        raise NotImplementedError("requires PostgreSQL")

    def save(self, job: IngestionJob) -> None:
        raise NotImplementedError("requires PostgreSQL")

    def find_by_content_hash(self, content_hash: str, scope: str) -> IngestionJob | None:
        raise NotImplementedError("requires PostgreSQL")

    def find_by_document_id(self, document_id: str) -> list[IngestionJob]:
        raise NotImplementedError("requires PostgreSQL")

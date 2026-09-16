"""Ingestion job tracking and persistence."""

from dataclasses import dataclass, field
from typing import Protocol

ACTIVE_JOB_STATUSES = ("queued", "processing")
DURABLE_REFERENCE_STATUSES = ("queued", "processing", "completed")


@dataclass
class IngestionJob:
    """Tracks the lifecycle of a single document ingestion."""
    job_id: str = ""
    document_id: str = ""
    document_scope: str = "external"
    status: str = "queued"
    content_hash: str = ""
    raw_gcs_uri: str = ""
    staging_gcs_uri: str = ""
    owner_session_id: str = ""
    batch_id: str = ""
    error_message: str = ""
    metadata: dict = field(default_factory=dict)


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

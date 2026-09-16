"""Helpers for reading and staging objects in Google Cloud Storage."""

import os
from dataclasses import dataclass


class PayloadTooLargeError(Exception):
    """Upload exceeds the maximum allowed size."""


@dataclass
class StagedPdf:
    """A PDF blob staged for processing."""
    gcs_uri: str = ""
    generation: int = 0
    content_hash: str = ""
    staging_gcs_uri: str = ""


@dataclass
class PromotedPdf:
    """A staged PDF promoted to permanent storage."""
    raw_gcs_uri: str = ""
    generation: int = 0
    content_hash: str = ""


def is_configured() -> bool:
    """Return True if GCS credentials and bucket are available."""
    return bool(os.environ.get("APP_GCS_BUCKET"))


def read_text(gcs_uri: str) -> str:
    """Read a UTF-8 text blob from GCS."""
    raise NotImplementedError("requires GCS credentials")


def read_bytes(gcs_uri: str) -> bytes:
    """Read raw bytes from a GCS blob."""
    raise NotImplementedError("requires GCS credentials")


def put_raw(document_id: str, ext: str, data: bytes) -> str:
    """Upload raw document bytes to GCS, return the GCS URI."""
    raise NotImplementedError("requires GCS credentials")


def stage_pdf(upload_id: str, data: bytes) -> StagedPdf:
    """Stage a PDF upload in GCS for processing."""
    raise NotImplementedError("requires GCS credentials")


def promote_pdf(staged: StagedPdf) -> PromotedPdf:
    """Promote a staged PDF to permanent raw storage."""
    raise NotImplementedError("requires GCS credentials")


def delete_prefix(prefix: str) -> int:
    """Delete all blobs under a GCS prefix, return count deleted."""
    raise NotImplementedError("requires GCS credentials")


def delete_generation(gcs_uri: str, generation: int) -> None:
    """Delete a specific generation of a GCS blob."""
    raise NotImplementedError("requires GCS credentials")

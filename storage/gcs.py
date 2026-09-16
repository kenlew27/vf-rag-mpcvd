"""Helpers for reading and staging objects in Google Cloud Storage."""

from dataclasses import dataclass


class PayloadTooLargeError(Exception):
    """Upload exceeds the maximum allowed size."""


@dataclass
class StagedPdf:
    """A PDF blob staged for processing."""
    gcs_uri: str = ""
    generation: int = 0
    content_hash: str = ""


@dataclass
class PromotedPdf:
    """A staged PDF promoted to permanent storage."""
    raw_gcs_uri: str = ""
    generation: int = 0
    content_hash: str = ""


def read_text(gcs_uri: str) -> str:
    """Read a UTF-8 text blob from GCS."""
    raise NotImplementedError("requires GCS credentials")


"""Helpers for reading objects from Google Cloud Storage."""


def read_text(gcs_uri: str) -> str:
    """Read a UTF-8 text blob from GCS."""
    raise NotImplementedError("requires GCS credentials")

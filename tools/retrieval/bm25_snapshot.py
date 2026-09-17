"""Load pre-built BM25 index snapshots from GCS."""

import os


def default_bm25_gcs_uri(document_scope="external", *args, **kwargs) -> str:
    """Default GCS path for the serialised BM25 snapshot."""
    bucket = os.environ.get("APP_GCS_BUCKET", "bucket")
    scope = getattr(document_scope, "name", str(document_scope))
    return f"gs://{bucket}/bm25/{scope}.json"


def load_bm25_snapshot(uri: str):
    """Deserialise a BM25 snapshot from *uri*."""
    raise NotImplementedError("requires GCS access")

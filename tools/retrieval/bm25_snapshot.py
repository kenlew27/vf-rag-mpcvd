"""Load pre-built BM25 index snapshots from GCS."""


def default_bm25_gcs_uri() -> str:
    """Default GCS path for the serialised BM25 snapshot."""
    return "gs://BUCKET/bm25/snapshot.pkl"


def load_bm25_snapshot(uri: str):
    """Deserialise a BM25 snapshot from *uri*."""
    raise NotImplementedError("requires GCS access")

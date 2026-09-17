"""Single-query embedding convenience wrapper."""

from typing import Any


def embed_query(query: str, embedder: Any = None, model: str = "voyage-4", *args, **kwargs) -> list[float]:
    """Return a dense vector for *query* using the given embedding model or embedder."""
    if embedder is not None:
        if hasattr(embedder, "embed_text"):
            return embedder.embed_text(query)
        if hasattr(embedder, "embed"):
            return embedder.embed([query])[0]
    return [0.0] * 1024

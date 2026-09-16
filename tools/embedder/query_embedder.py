"""Single-query embedding convenience wrapper."""


def embed_query(query: str, model: str = "voyage-4") -> list[float]:
    """Return a dense vector for *query* using the given embedding model."""
    raise NotImplementedError("requires deployed Voyage API credentials")

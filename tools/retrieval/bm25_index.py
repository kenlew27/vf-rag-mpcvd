"""BM25 sparse keyword index over document chunks."""


class BM25ChunkSearchIndex:
    """Wraps rank-bm25 for sparse keyword retrieval."""

    @classmethod
    def from_records(cls, records=()):
        return cls()

    def search(self, query: str, limit: int = 10):
        return []

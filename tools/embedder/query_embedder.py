"""Single-query embedding convenience wrapper."""

from dataclasses import dataclass
from typing import Any


@dataclass
class EmbeddedQuery:
    """Represents an embedded search query with text, vector, and model name."""
    query_text: str
    vector: list[float]
    model: str = "voyage-4"

    def __iter__(self):
        return iter(self.vector)

    def __len__(self):
        return len(self.vector)

    def __getitem__(self, item):
        return self.vector[item]


_DEFAULT_EMBEDDING_DIM = 1024

def embed_query(query: str, embedder: Any = None, model: str = "voyage-4", *args, **kwargs) -> EmbeddedQuery:
    """Return an EmbeddedQuery for *query* using the given embedding model or embedder."""
    vec = [0.0] * _DEFAULT_EMBEDDING_DIM
    if embedder is not None:
        if hasattr(embedder, "embed_text"):
            vec = embedder.embed_text(query)
        elif hasattr(embedder, "embed"):
            res = embedder.embed([query])
            if res:
                vec = res[0]
        model = getattr(embedder, "model", model)
    return EmbeddedQuery(query_text=query, vector=list(vec), model=model)

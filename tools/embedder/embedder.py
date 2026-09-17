"""Voyage AI dense embedding client.

Wraps the Voyage API to produce 1024-dim embeddings (voyage-4 model).
"""


class VoyageEmbedder:
    """Batched embedding client backed by the Voyage AI API."""

    def __init__(self, model: str = "voyage-4", dimension: int = 1024, input_type: str = "document", *args, **kwargs):
        self.model = model
        self.dimension = dimension
        self.input_type = input_type

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("requires deployed Voyage API credentials")

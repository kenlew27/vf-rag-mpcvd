"""Delete document artifacts from storage backends."""


def delete_document_artifacts(document_id: str, *, scope: str = "external") -> None:
    """Remove all stored artifacts (chunks, vectors, GCS blobs) for a document."""
    raise NotImplementedError("requires storage backend access")

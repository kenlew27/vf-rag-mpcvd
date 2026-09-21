"""Delete document artifacts from storage backends."""


from typing import Any

def delete_document_artifacts(document_id: str, *, document_scope: str = "external", chunk_store: Any = None) -> Any:
    """Remove all stored artifacts (chunks, vectors, GCS blobs) for a document."""
    raise NotImplementedError("requires storage backend access")

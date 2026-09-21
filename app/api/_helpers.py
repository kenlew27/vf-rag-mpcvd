"""Shared helper functions for the FastAPI application layer."""

def normalize_document_ids(document_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    for document_id in document_ids:
        text = str(document_id).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized

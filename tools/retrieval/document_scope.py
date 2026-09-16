"""Document scope constants and normalisation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentScope:
    """Identifies an internal or external document scope for retrieval."""
    name: str
    description: str = ""


INTERNAL_SCOPE = DocumentScope(name="internal", description="Internal company documents")
EXTERNAL_SCOPE = DocumentScope(name="external", description="External published literature")
DOCUMENT_SCOPE_KEY = "document_scope"


def normalize_document_scope(scope: str) -> DocumentScope:
    """Map a scope string like ``"internal"`` to its :class:`DocumentScope`."""
    if scope.lower() in ("internal", "int"):
        return INTERNAL_SCOPE
    if scope.lower() in ("external", "ext"):
        return EXTERNAL_SCOPE
    raise ValueError(f"unknown document scope: {scope!r}")


def normalize_document_scopes(scopes: list[str]) -> list[DocumentScope]:
    """Map a list of scope strings to :class:`DocumentScope` objects."""
    return [normalize_document_scope(s) for s in scopes]

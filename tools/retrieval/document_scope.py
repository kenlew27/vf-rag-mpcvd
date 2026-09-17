"""Document scope constants and normalisation."""


class DocumentScope(str):
    """Identifies an internal or external document scope for retrieval."""
    name: str
    description: str

    def __new__(cls, name: str, description: str = ""):
        obj = str.__new__(cls, name)
        obj.name = name
        obj.description = description
        return obj

    def __repr__(self) -> str:
        return f"DocumentScope(name={self.name!r}, description={self.description!r})"


INTERNAL_SCOPE = DocumentScope("internal", "Internal company documents")
EXTERNAL_SCOPE = DocumentScope("external", "External published literature")
DOCUMENT_SCOPE_KEY = "document_scope"


def normalize_document_scope(scope) -> DocumentScope:
    """Map a scope string like ``"internal"`` to its :class:`DocumentScope`."""
    if isinstance(scope, DocumentScope):
        return scope
    if isinstance(scope, str):
        s = scope.lower()
        if s in ("internal", "int"):
            return INTERNAL_SCOPE
        if s in ("external", "ext"):
            return EXTERNAL_SCOPE
    raise ValueError(f"unknown document scope: {scope!r}")


def normalize_document_scopes(scopes: list) -> list[DocumentScope]:
    """Map a list of scope strings to :class:`DocumentScope` objects."""
    return [normalize_document_scope(s) for s in scopes]

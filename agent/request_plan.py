"""Validated request-planning contract for the materials agent."""

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from tools.retrieval.document_scope import DocumentScope


class RequestTask(StrEnum):
    """Operations the agent should perform for the request."""

    LOOKUP = "lookup"
    SUMMARIZE = "summarize"
    COMPARE = "compare"
    GENERATE_HYPOTHESIS = "generate_hypothesis"


_CANONICAL_TASK_ORDER = (
    RequestTask.LOOKUP,
    RequestTask.SUMMARIZE,
    RequestTask.COMPARE,
    RequestTask.GENERATE_HYPOTHESIS,
)


class KnowledgeSource(StrEnum):
    """Evidence or reasoning lanes the request may use."""

    GENERAL_KNOWLEDGE = "general_knowledge"
    STRUCTURED_DATA = "structured_data"
    INTERNAL_DOCUMENTS = "internal_documents"
    EXTERNAL_LITERATURE = "external_literature"


class RequestStatus(StrEnum):
    """Current outcome for planning and execution."""

    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED_REQUEST = "unsupported_request"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXECUTION_FAILED = "execution_failed"


_STRUCTURED_ONLY_RE = re.compile(
    r"\b(?:only\s+use|use\s+only)\s+(?:the\s+)?"
    r"(?:run\s+data|structured\s+data|database(?:\s+data)?)\b"
    r"|\b(?:run\s+data|structured\s+data|database)[-\s]+only\b",
    re.IGNORECASE,
)
_DOCUMENT_REFERENCE_RE = re.compile(
    r"\.pdf\b|\b(?:paper|papers|document|documents|literature|research|study|studies|publication|publications)\b",
    re.IGNORECASE,
)
_LITERATURE_CLAUSE_RE = re.compile(
    r"(?:\s*(?:,|;|\band\b)\s*)?(?:then\s+)?(?:compare|contrast|assess|interpret|explain|discuss|"
    r"evaluate|relate|reconcile|identify|highlight)\b[^.!?;]*(?:\.pdf\b|\b(?:paper|papers|document|documents|literature|research|study|studies|publication|publications)\b)[^.!?;]*",
    re.IGNORECASE,
)
_REACTOR_REFERENCE_RE = re.compile(
    r"\breactors?\s+((?:[A-Za-z0-9]+-[A-Za-z0-9]+)(?:\s*(?:,|and|or)\s*(?:[A-Za-z0-9]+-[A-Za-z0-9]+))*)",
    re.IGNORECASE,
)


class RequestPlan(BaseModel):
    """Ordered, validated plan emitted by the request planner."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[RequestTask] = Field(default_factory=list)
    knowledge_sources: list[KnowledgeSource] = Field(default_factory=list)
    status: RequestStatus
    reasons: list[str] = Field(default_factory=list)
    structured_data_question: str | None = None

    @field_validator("structured_data_question")
    @classmethod
    def _normalize_structured_data_question(cls, question: str | None) -> str | None:
        if question is None:
            return None
        normalized = " ".join(question.split())
        if not normalized:
            return None
        if len(normalized) > 1000:
            raise ValueError("structured_data_question must be 1000 characters or fewer")
        return normalized

    @field_validator("reasons")
    @classmethod
    def _concise_reasons(cls, reasons: list[str]) -> list[str]:
        normalized: list[str] = []
        for reason in reasons:
            clean = reason.strip()
            if not clean:
                continue
            if len(clean) > 300:
                raise ValueError("reasons must be concise (300 characters or fewer)")
            if clean not in normalized:
                normalized.append(clean)
        return normalized

    @model_validator(mode="after")
    def _validate_status_and_dedupe(self) -> "RequestPlan":
        self.tasks = sorted(_dedupe(self.tasks), key=_CANONICAL_TASK_ORDER.index)
        self.knowledge_sources = _dedupe(self.knowledge_sources)
        if self.status is RequestStatus.READY:
            if not self.tasks or not self.knowledge_sources:
                raise ValueError("ready requests require at least one task and knowledge source")
        elif not self.reasons:
            raise ValueError("non-ready requests require at least one concise reason")
        return self


def is_explicit_structured_only_request(query: str) -> bool:
    """Return whether the user unambiguously restricted evidence to run data."""
    return _STRUCTURED_ONLY_RE.search(query) is not None


def database_only_question(query: str, planned_question: str | None) -> str:
    """Keep document comparison clauses out of the database-agent question."""
    if planned_question:
        cleaned = _LITERATURE_CLAUSE_RE.sub("", planned_question).strip(" ,;.")
        if cleaned and _DOCUMENT_REFERENCE_RE.search(cleaned) is None:
            return _preserve_explicit_entity_terms(query, cleaned)
        if not cleaned:
            return _preserve_explicit_entity_terms(query, database_reference_question(query))
    clauses = re.split(r"(?<=[.!?])\s+|;\s*", query)
    database_clauses = [
        clause.strip()
        for clause in clauses
        if clause.strip()
        and not is_explicit_structured_only_request(clause)
        and _DOCUMENT_REFERENCE_RE.search(clause) is None
    ]
    return _preserve_explicit_entity_terms(
        query,
        " ".join(database_clauses) or database_reference_question(query),
    )


def database_reference_question(query: str) -> str:
    """Extract only request clauses that can constrain database retrieval."""
    clauses = re.split(r"(?<=[.!?])\s+|;\s*", query)
    references: list[str] = []
    for clause in clauses:
        cleaned = _LITERATURE_CLAUSE_RE.sub("", clause).strip(" ,;.")
        if cleaned and _DOCUMENT_REFERENCE_RE.search(cleaned) is None:
            references.append(cleaned)
    return " ".join(references)


def _preserve_explicit_entity_terms(query: str, planned_question: str) -> str:
    """Prevent a planner rewrite from changing explicit reactor identifiers to samples."""
    match = _REACTOR_REFERENCE_RE.search(query)
    if match is None:
        return planned_question

    identifiers = " ".join(match.group(1).split())
    noun = "reactor" if len(re.findall(r"[A-Za-z0-9]+-[A-Za-z0-9]+", identifiers)) == 1 else "reactors"
    normalized = re.sub(r"\bsamples?\b", noun, planned_question, flags=re.IGNORECASE)
    if re.search(r"\breactors?\b", normalized, re.IGNORECASE) is None:
        normalized = f"{normalized.rstrip('.')} for {noun} {identifiers}."
    if all(identifier.lower() in normalized.lower() for identifier in re.findall(r"[A-Za-z0-9]+-[A-Za-z0-9]+", identifiers)):
        return normalized
    return f"{normalized.rstrip('.')} ({noun} {identifiers})."


def request_plan_output_schema() -> dict:
    """Require the nullable lane question in model output to avoid silent omission."""

    schema = RequestPlan.model_json_schema()
    required = list(schema.get("required", []))
    if "structured_data_question" not in required:
        required.append("structured_data_question")
    schema["required"] = required
    return schema


def document_scopes_for_sources(sources: list[KnowledgeSource]) -> list[str]:
    """Derive document execution scopes from selected knowledge sources."""

    source_to_scope = {
        KnowledgeSource.INTERNAL_DOCUMENTS: "internal",
        KnowledgeSource.EXTERNAL_LITERATURE: "external",
    }
    return _dedupe([source_to_scope[source] for source in sources if source in source_to_scope])


def _dedupe(values: list[StrEnum]) -> list[StrEnum]:
    return list(dict.fromkeys(values))

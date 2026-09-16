"""Pydantic contracts for synthesis-only evidence planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Answerability = Literal["answerable", "partially_answerable", "not_answerable", "unknown"]
CompletenessStatus = Literal["complete", "partial", "empty", "unknown", "unavailable"]
EvidenceKind = Literal["document", "database"]


class NormalizedEvidence(BaseModel):
    """One synthesis-ready evidence item with upstream packet shape preserved."""

    evidence_id: str
    evidence_kind: EvidenceKind
    source_agent: str | None = None
    question: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NormalizedDocumentEvidence(NormalizedEvidence):
    evidence_kind: Literal["document"] = "document"
    document_scope: str | None = None
    context_id: str | None = None
    document_id: str | None = None
    relative_relevance: float | None = None
    retrieval_score_raw: float | None = None


class NormalizedDatabaseEvidence(NormalizedEvidence):
    evidence_kind: Literal["database"] = "database"
    row_index: int
    row: dict[str, Any] = Field(default_factory=dict)


class DatabaseCompleteness(BaseModel):
    """Best-effort inference from database packet metadata."""

    status: CompletenessStatus = "unavailable"
    dry_run_passed: bool | None = None
    rows_returned: int = 0
    row_count_before_limit: int | None = None
    result_limited: bool | None = None
    reasons: list[str] = Field(default_factory=list)


class NormalizedEvidenceBundle(BaseModel):
    question: str | None = None
    document_packets: list[dict[str, Any]] = Field(default_factory=list)
    database_packets: list[dict[str, Any]] = Field(default_factory=list)
    document_evidence: list[NormalizedDocumentEvidence] = Field(default_factory=list)
    database_evidence: list[NormalizedDatabaseEvidence] = Field(default_factory=list)
    database_completeness: DatabaseCompleteness = Field(default_factory=DatabaseCompleteness)
    missing_evidence: list[str] = Field(default_factory=list)

    @property
    def evidence(self) -> list[NormalizedEvidence]:
        return [*self.document_evidence, *self.database_evidence]

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.evidence]


class EvidencePlanningRequest(BaseModel):
    question: str
    evidence_bundle: NormalizedEvidenceBundle


class EvidencePlanStep(BaseModel):
    description: str
    needed_evidence: list[str] = Field(default_factory=list)
    available_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class EvidencePlan(BaseModel):
    question: str | None = None
    steps: list[EvidencePlanStep] = Field(default_factory=list)
    available_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    answerability: Answerability = "unknown"


class SynthesisClaim(BaseModel):
    claim_id: str
    claim: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    answerability: Answerability = "unknown"


class SynthesisHypothesis(BaseModel):
    hypothesis_id: str
    claims: list[SynthesisClaim] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    answerability: Answerability = "unknown"


class SynthesisRequest(BaseModel):
    question: str
    evidence_bundle: NormalizedEvidenceBundle
    evidence_plan: EvidencePlan | None = None


class SynthesisPipelineResult(BaseModel):
    evidence_bundle: NormalizedEvidenceBundle
    evidence_plan: EvidencePlan | None = None
    synthesis_hypothesis: SynthesisHypothesis
    planner_metadata: dict[str, Any] = Field(default_factory=dict)

"""Agent state definitions."""

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from agent.request_plan import RequestPlan
from agent.schemas import DocumentQueryPlan, FinalAnswer
SourceMode = Literal["none", "selected", "all"]


class UserQuery(BaseModel):
    """Description: Original user request context.

    Input: Raw user text and optional formula.
    Output: Validated query payload for agent state.
    """

    raw_text: str  # Original raw user request text.
    formula: str | None = None  # Formula text or identifier supplied with the request.


class ContextProfile(BaseModel):
    """Description: Extracted material goal.

    Input: User request context after request planning.
    Output: Material goals, constraints, and tests for retrieval/answering.
    """

    improve: list[str] = Field(default_factory=list)  # Material properties or outcomes to improve.
    preserve: list[str] = Field(default_factory=list)  # Material properties or outcomes to keep stable.
    constraints: list[str] = Field(default_factory=list)  # Hard limits or requirements answers must respect.
    required_tests: list[str] = Field(default_factory=list)  # Tests needed to validate goal or answer.


class EvidenceItem(BaseModel):
    """Description: Raw retrieved evidence.

    Input: Retrieved source snippet or row from retrieval nodes.
    Output: Evidence packet ready for reranking and citation validation.
    """

    evidence_id: str  # Stable internal ID for evidence item.
    source_type: Literal["bigquery", "pdf", "table", "figure", "paper", "internal_report"]  # Source family.
    summary: str  # Short factual summary of evidence.
    citation: str  # Citation label or source locator.
    confidence: Literal["high", "medium", "low"]  # Confidence level assigned to evidence.
    limitations: list[str] = Field(default_factory=list)  # Caveats that limit how strongly evidence applies.


class Hypothesis(BaseModel):
    """Description: Evidence-backed working claim.

    Input: Reranked evidence and target profile.
    Output: Claim for answer generation or candidate experiment planning.
    """

    claim: str  # Proposed material reasoning claim.
    supporting_evidence_ids: list[str] = Field(default_factory=list)  # Evidence IDs backing claim.
    confidence: Literal["high", "medium", "low"]  # Confidence in claim.


class DocumentQueryRewrite(BaseModel):
    """Description: Shared document retrieval query rewrite.

    Input: Original user query after document rewrite prompt.
    Output: Rewritten query and notes reused by scoped document agents.
    """

    rewritten_query: str  # Query used for document embedding, keyword search, and reranking.
    alternative_queries: list[str] = Field(default_factory=list)  # Additional search angles from the rewrite LLM.
    rewrite_notes: list[str] = Field(default_factory=list)  # Rewrite rationale or fallback notes.


class AgentState(BaseModel):
    """Description: Shared state passed between agent graph nodes.

    Input: User query plus node outputs accumulated through graph execution.
    Output: Current graph state for routing, retrieval, validation, and answering.
    """

    run_id: str = Field(default_factory=lambda: str(uuid4()))  # Stable per-request ID shared across all nodes.
    query: UserQuery  # Original user/request context.
    include_debug_trace: bool = False  # Opt-in structured database stage trace for evaluation harnesses.
    request_plan: RequestPlan | None = None  # Task, knowledge-source, and status plan for this request.
    target_profile: ContextProfile | None = None  # Extracted material goal.
    bigquery_results: list[dict] = Field(default_factory=list)  # Raw rows from BigQuery retrieval nodes.
    database_evidence: dict = Field(default_factory=dict)  # Structured evidence packet from the database agent.
    document_scopes: list[Literal["internal", "external"]] = Field(default_factory=list)  # Derived document execution scopes from request_plan sources.
    document_query_plan: DocumentQueryPlan | None = None  # Typed DB-informed document retrieval plan for combined requests.
    document_query_rewrite: DocumentQueryRewrite | None = None  # Shared document rewrite for scoped agents.
    document_evidence: dict = Field(default_factory=dict)  # Structured document evidence packets keyed by scope.
    source_mode: SourceMode = "all"  # PDF context mode requested by the client; missing legacy requests default to all.
    selected_document_ids: list[str] = Field(default_factory=list)  # Client-selected document IDs when source_mode=selected.
    resolved_document_targets: list[dict[str, Any]] = Field(default_factory=list)  # Document records resolved for doc tasks.
    source_filters: dict[str, Any] = Field(default_factory=dict)  # Docs/sources allowed for retrieval.
    retrieval_queries: list[str] = Field(default_factory=list)  # Document search strings consumed by retrieval nodes.
    evidence: list[EvidenceItem] = Field(default_factory=list)  # Raw retrieved evidence.
    reranked_evidence: list[EvidenceItem] = Field(default_factory=list)  # Best evidence for answer generation.
    hypotheses: list[Hypothesis] = Field(default_factory=list)  # Evidence-backed working claims.
    citation_errors: list[str] = Field(default_factory=list)  # Citation validator failures.
    supervisor_meta: dict = Field(default_factory=dict)  # Routing decisions written by supervisor; consumed by synthesize_answer for lane tracking.
    evidence_packet: dict | None = None  # Serialized EvidencePacket (set by synthesize_answer, reused on retry).
    synthesis_retry_count: int = Field(default=0, ge=0, le=1)  # Verifier permits exactly one revision attempt.
    final_answer: FinalAnswer | None = None  # Structured user-facing response (set by synthesize_answer).

"""
agent/schemas.py

Reasoning agent contracts: evidence packet, LLM output, FinalAnswer, log record.

Design decisions encoded here:
  - Citations are evidence IDs, never free-form bibliographic text.
  - The LLM-facing output schema (SynthesisOutput) is separate from the stored
    FinalAnswer: code stamps run_id / prompt_version / model / packet_hash,
    the model never writes them.
  - Confidence is ordinal with a required basis; `not_assessable` pairs with
    a scoped abstention.
  - Causal-sounding conclusions must carry a causality_class.
  - Evidence content is extractive: numbers, units, and field names verbatim.

Requires: pydantic >= 2.5
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.request_plan import KnowledgeSource, RequestStatus, RequestTask

SCHEMA_VERSION = "1.2.0"

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    """Six-category source taxonomy. Letter is embedded in evidence IDs."""

    STRUCTURED = "S"    # structured DB fields (experiment_records etc.)
    TABLE = "T"         # table chunks from ingested documents
    COMMENT = "C"       # scientist comments / notes
    DOCUMENT = "D"      # document text chunks (internal docs)
    LITERATURE = "L"    # external literature chunks
    MEASUREMENT = "M"   # measurement / image-derived records (QBIR etc.)


class EvidenceRole(str, Enum):
    """Set upstream (retrieval lane / critique agent), not by synthesis."""

    SUPPORTING = "supporting"
    CONTRADICTORY = "contradictory"
    CONTEXT = "context"
    UNASSESSED = "unassessed"


class ConfidenceLabel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOT_ASSESSABLE = "not_assessable"


EVIDENCE_ID_PATTERN = re.compile(r"^EV-[STCDLM]-\d{3,}$")


# ---------------------------------------------------------------------------
# Evidence packet (input side — built by packet assembler, consumed by LLM)
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where a parcel came from. Carried through from subagents; audit only."""

    origin_agent: str                          # e.g. "table_agent", "doc_agent_lit"
    retrieval_query: Optional[str] = None      # the subquery that surfaced it
    retriever_rank: Optional[int] = None
    reranker_score: Optional[float] = None     # ranking signal only — never a probability
    source_ref: Optional[str] = None           # doc id, BQ table, GCS path, run id...
    page_or_row: Optional[str] = None          # page number, row key, chunk index


class EvidenceItem(BaseModel):
    """One immutable parcel. Content is extractive — no paraphrased numbers."""

    evidence_id: str = Field(
        ..., description="Stable ID, format EV-{S|T|C|D|L|M}-{seq}, e.g. EV-D-003"
    )
    source_type: SourceType
    source_label: str = Field(
        ..., description="Human-readable label the renderer shows, e.g. 'Run EXP-001 (experiment_records)'"
    )
    content: str = Field(
        ...,
        description=(
            "Extractive content. Field names, values, and units verbatim from source. "
            "Tables as compact key: value lines, not prose."
        ),
    )
    role: EvidenceRole = EvidenceRole.UNASSESSED
    conditions: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured conditions (material, pressure, recipe id...) for condition-match checks",
    )
    provenance: Provenance

    @field_validator("evidence_id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not EVIDENCE_ID_PATTERN.match(v):
            raise ValueError(f"evidence_id '{v}' must match EV-<lane letter>-<seq>")
        return v

    @model_validator(mode="after")
    def _id_matches_source_type(self) -> "EvidenceItem":
        letter = self.evidence_id.split("-")[1]
        if letter != self.source_type.value:
            raise ValueError(
                f"evidence_id lane letter '{letter}' does not match source_type '{self.source_type.value}'"
            )
        return self


class LaneFailure(BaseModel):
    """A selected retrieval lane that failed rather than returning no evidence."""

    model_config = ConfigDict(extra="forbid")

    lane: str
    agent: str
    reason: str


class EvidencePacket(BaseModel):
    """Everything the reasoning agent sees. Raw BQ rows are NOT included."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    query_raw: str
    tasks: list[RequestTask]
    knowledge_sources: list[KnowledgeSource]
    request_status: RequestStatus
    items: list[EvidenceItem] = Field(default_factory=list)
    lanes_attempted: list[str] = Field(
        default_factory=list,
        description="Retrieval lanes the supervisor ran, incl. ones that returned nothing — needed for honest abstention",
    )
    lanes_empty: list[str] = Field(default_factory=list)
    lane_failures: list[LaneFailure] = Field(default_factory=list)
    database_completeness: dict[str, Any] = Field(
        default_factory=dict,
        description="Deterministic database result coverage metadata for synthesis scope claims.",
    )
    critique_notes: list[str] = Field(
        default_factory=list,
        description="Pre-synthesis critique agent output, if run (e.g. 'EV-S-002 contradicts EV-L-007')",
    )

    def packet_hash(self) -> str:
        """Deterministic hash for the log record."""
        canonical = self.model_dump_json(exclude_none=True)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def evidence_ids(self) -> set[str]:
        return {item.evidence_id for item in self.items}

    def is_empty(self) -> bool:
        """If True, skip the Sonnet call and return a direct abstention."""
        return len(self.items) == 0


# ---------------------------------------------------------------------------
# DB-informed document query planning
# ---------------------------------------------------------------------------


_PLANNER_EVIDENCE_ID_PATTERN = re.compile(r"^EV-S-[A-Za-z0-9][A-Za-z0-9._-]*$")


class _StrictPlannerModel(BaseModel):
    """Fail-closed base for the document query planner boundary."""

    model_config = ConfigDict(extra="forbid")


class DocumentQueryScope(str, Enum):
    INTERNAL = "internal"
    LITERATURE = "literature"
    BOTH = "both"


class DocumentQueryIntent(str, Enum):
    MECHANISM = "mechanism"
    FAILURE_MODE = "failure_mode"
    MITIGATION = "mitigation"
    MEASUREMENT_METHOD = "measurement_method"
    BACKGROUND = "background"


class PlannerTableColumn(_StrictPlannerModel):
    """Readable metadata for one database planner-table column."""

    name: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    unit: str | None
    source_column: str | None


class PlannerTableRow(_StrictPlannerModel):
    """One database row supplied as retrieval context, with dynamic columns."""

    model_config = ConfigDict(extra="allow")

    evidence_id: str = Field(..., min_length=1)
    process_id: Any | None

    @field_validator("evidence_id")
    @classmethod
    def _structured_evidence_id(cls, value: str) -> str:
        if not _PLANNER_EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("planner row evidence_id must match EV-S-*")
        return value


class DatabasePlannerTable(_StrictPlannerModel):
    """Bounded BigQuery rows and only the metadata needed to interpret them."""

    columns: list[PlannerTableColumn]
    rows: list[PlannerTableRow]
    rows_returned: int = Field(..., ge=0, strict=True)
    result_limit: int = Field(..., gt=0, strict=True)
    truncated: bool = Field(..., strict=True)
    schema_caveats: list[str]

    @model_validator(mode="after")
    def _validate_result_metadata(self) -> "DatabasePlannerTable":
        if self.rows_returned != len(self.rows):
            raise ValueError("rows_returned must equal the number of planner rows")
        if self.rows_returned > self.result_limit:
            raise ValueError("rows_returned cannot exceed result_limit")
        expected_truncated = self.rows_returned == self.result_limit
        if self.truncated != expected_truncated:
            raise ValueError(
                "truncated must be true exactly when rows_returned reaches result_limit"
            )

        evidence_ids = [row.evidence_id for row in self.rows]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("planner row evidence_id values must be unique")

        column_names = [column.name for column in self.columns]
        if len(column_names) != len(set(column_names)):
            raise ValueError("planner table column names must be unique")
        declared_columns = set(column_names)
        missing_required = {"evidence_id", "process_id"} - declared_columns
        if missing_required:
            raise ValueError(
                "planner table columns must declare: "
                + ", ".join(sorted(missing_required))
            )
        for row in self.rows:
            undeclared = set(row.model_dump()) - declared_columns
            if undeclared:
                raise ValueError(
                    "planner row fields must be declared in columns: "
                    + ", ".join(sorted(undeclared))
                )
        return self


class DocumentQueryPlanningRequest(_StrictPlannerModel):
    """Whitelisted input contract for DB-informed document query planning."""

    question: str = Field(..., min_length=1)
    planner_table: DatabasePlannerTable
    database_limitations: list[str]

    @field_validator("question")
    @classmethod
    def _nonblank_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class DocumentSearchQuery(_StrictPlannerModel):
    """One grounded search to send to internal and/or literature retrieval."""

    query_id: str = Field(..., pattern=r"^DQ-\d{3}$")
    query: str = Field(..., min_length=1)
    scope: DocumentQueryScope
    intent: DocumentQueryIntent
    linked_evidence_ids: list[str] = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)
    priority: int = Field(..., ge=1, le=3, strict=True)

    @field_validator("query", "rationale")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query and rationale must not be blank")
        return value

    @field_validator("scope", mode="before")
    @classmethod
    def _parse_scope(cls, value: Any) -> Any:
        if isinstance(value, str):
            return DocumentQueryScope(value)
        return value

    @field_validator("intent", mode="before")
    @classmethod
    def _parse_intent(cls, value: Any) -> Any:
        if isinstance(value, str):
            return DocumentQueryIntent(value)
        return value

    @field_validator("linked_evidence_ids")
    @classmethod
    def _linked_ids_are_structured(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _PLANNER_EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError("linked_evidence_ids entries must match EV-S-*")
        if len(values) != len(set(values)):
            raise ValueError("linked_evidence_ids must not contain duplicates")
        return values


class DocumentQueryDataGap(_StrictPlannerModel):
    """Information absent from the database evidence that retrieval should fill."""

    description: str = Field(..., min_length=1)
    linked_evidence_ids: list[str]

    @field_validator("description")
    @classmethod
    def _nonblank_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be blank")
        return value

    @field_validator("linked_evidence_ids")
    @classmethod
    def _linked_ids_are_structured(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _PLANNER_EVIDENCE_ID_PATTERN.fullmatch(value):
                raise ValueError("linked_evidence_ids entries must match EV-S-*")
        if len(values) != len(set(values)):
            raise ValueError("linked_evidence_ids must not contain duplicates")
        return values


class DocumentQueryPlan(_StrictPlannerModel):
    """Validated retrieval plan returned by the document query planner."""

    queries: list[DocumentSearchQuery] = Field(..., max_length=3)
    data_gaps: list[DocumentQueryDataGap]

    @model_validator(mode="after")
    def _validate_plan(self) -> "DocumentQueryPlan":
        query_ids = [query.query_id for query in self.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("query_id values must be unique")
        if not self.queries and not self.data_gaps:
            raise ValueError("an empty query plan requires at least one data gap")
        return self


# ---------------------------------------------------------------------------
# LLM output (what Claude returns — no metadata fields, code can't trust it)
# ---------------------------------------------------------------------------


class ScopedAbstention(BaseModel):
    """The three-part abstention: dead ends become partially useful answers."""

    cannot_conclude: list[str] = Field(
        ..., description="What the evidence does not establish"
    )
    can_still_state: list[str] = Field(
        default_factory=list, description="Weaker statements that ARE supported"
    )
    would_resolve: list[str] = Field(
        default_factory=list,
        description="Missing data, runs, or literature that would close the gap",
    )


class SynthesisOutput(BaseModel):
    """Exactly what the model must return (strict structured output).
    No run_id, no prompt_version, no timestamps — code stamps those."""

    answer: str = Field(
        ...,
        description="Markdown prose. Inline citations as [EV-X-NNN] immediately after the sentence they support.",
    )
    citations: list[str] = Field(
        ..., description="Every evidence ID cited in the answer, deduplicated"
    )
    confidence: ConfidenceLabel
    confidence_basis: list[str] = Field(
        ...,
        min_length=1,
        description="Why this band: evidence count/independence, condition match, contradictions, gaps",
    )
    contradictions_noted: list[str] = Field(
        default_factory=list,
        description="Contradictory evidence acknowledged in the answer, by evidence ID with one-line note",
    )
    abstention: Optional[ScopedAbstention] = Field(
        default=None,
        description="Required when confidence is not_assessable; allowed for partial abstention otherwise",
    )

    @model_validator(mode="after")
    def _abstention_consistency(self) -> "SynthesisOutput":
        if self.confidence == ConfidenceLabel.NOT_ASSESSABLE and self.abstention is None:
            raise ValueError("confidence=not_assessable requires an abstention object")
        return self

    @field_validator("citations")
    @classmethod
    def _citation_format(cls, v: list[str]) -> list[str]:
        for cid in v:
            if not EVIDENCE_ID_PATTERN.match(cid):
                raise ValueError(f"citation '{cid}' is not a valid evidence ID")
        return v


class ExecutiveSummary(BaseModel):
    """Post-verification, user-facing synthesis of supported claims only."""

    answer: str = Field(..., min_length=1)
    citations: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str

    @field_validator("citations")
    @classmethod
    def _citation_format(cls, v: list[str]) -> list[str]:
        for cid in v:
            if not EVIDENCE_ID_PATTERN.match(cid):
                raise ValueError(f"citation '{cid}' is not a valid evidence ID")
        if len(v) != len(set(v)):
            raise ValueError("executive-summary citations must not contain duplicates")
        return v


# ---------------------------------------------------------------------------
# FinalAnswer (stored / returned to frontend — SynthesisOutput + stamped metadata)
# ---------------------------------------------------------------------------


class FinalAnswer(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    response_id: str = Field(default_factory=lambda: f"RESP-{uuid4().hex[:12]}")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tasks: list[RequestTask]
    knowledge_sources: list[KnowledgeSource]
    request_status: RequestStatus
    model: str                  # e.g. "claude-sonnet-4-6"
    prompt_version: str         # hash or semver of synthesis_prompt.md
    packet_hash: str

    # the model's output, verbatim after validation
    synthesis: SynthesisOutput

    # code-computed, not model-claimed
    citation_check: "CitationCheck"

    # populated by verify_answer node; None until the verifier runs
    verification: Optional["VerificationResult"] = None

    # Populated after a terminal verifier result. The draft remains available
    # for audit while this is the user-facing prose summary.
    executive_summary: Optional[ExecutiveSummary] = None

    # Deterministic database-schema coverage, kept outside model-generated and
    # verifier-evaluated prose.
    database_coverage_notice: Optional[str] = None

    @classmethod
    def from_synthesis(
        cls,
        synthesis: SynthesisOutput,
        packet: EvidencePacket,
        model: str,
        prompt_version: str,
    ) -> "FinalAnswer":
        return cls(
            run_id=packet.run_id,
            tasks=packet.tasks,
            knowledge_sources=packet.knowledge_sources,
            request_status=packet.request_status,
            model=model,
            prompt_version=prompt_version,
            packet_hash=packet.packet_hash(),
            synthesis=synthesis,
            citation_check=validate_citations(synthesis, packet),
        )


class CitationCheck(BaseModel):
    """Deterministic checks only. Entailment checking is Phase 2 — this just
    guarantees the citations are real and complete at the ID level."""

    all_cited_ids_exist: bool
    unknown_ids: list[str] = Field(default_factory=list)               # cited but not in packet
    inline_not_in_citations: list[str] = Field(default_factory=list)   # in answer text, missing from citations list
    citations_not_inline: list[str] = Field(default_factory=list)      # in citations list, never used in answer
    uncited_answer: bool = False   # answer has zero inline citations but is not an abstention


# ---------------------------------------------------------------------------
# Verifier
#
# The LLM owns one evidence status per claim. Code owns actions, workflow
# outcomes, retry count, and terminal rendering.
# ---------------------------------------------------------------------------


class ClaimUnit(BaseModel):
    """One verifiable unit extracted from answer prose."""

    claim_id: str                       # CLM-{seq}, minted at extraction
    claim_text: str
    cited_evidence_ids: list[str] = Field(default_factory=list)
    context_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Bounded nearby evidence for an uncited prose unit; does not change citation status.",
    )
    origin: str = Field(
        default="cited_sentence",
        description="'cited_sentence' | 'causal_claim' | 'uncited_factual' (flagged by heuristic)",
    )


class ClaimAction(str, Enum):
    ALLOW = "allow"
    INTERVENE = "intervene"


class WorkflowOutcome(str, Enum):
    PASSED = "passed"
    RETRY_TRIGGERED = "retry_triggered"
    PASSED_AFTER_RETRY = "passed_after_retry"
    PARTIAL_AFTER_RETRY = "partial_after_retry"


class _StrictVerifierModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifierEvidenceParcel(_StrictVerifierModel):
    evidence_id: str
    content: str


class VerifierInputClaim(_StrictVerifierModel):
    claim_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    evidence: list[VerifierEvidenceParcel]


class ClaimVerificationStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


class LLMPropositionCheck(_StrictVerifierModel):
    """One evidence verdict for a material proposition within a claim."""

    proposition: str = Field(min_length=1)
    status: ClaimVerificationStatus
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_evidence_ids(self) -> "LLMPropositionCheck":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("proposition evidence_ids must not contain duplicates")
        if self.status in {
            ClaimVerificationStatus.SUPPORTED,
            ClaimVerificationStatus.CONTRADICTED,
        } and not self.evidence_ids:
            raise ValueError(
                f"{self.status.value} propositions require at least one evidence_id"
            )
        return self


class LLMClaimCheck(_StrictVerifierModel):
    """Material-proposition verdicts for one complete isolated claim."""

    claim_id: str = Field(min_length=1)
    propositions: list[LLMPropositionCheck] = Field(min_length=1)


class LLMVerifierResponse(_StrictVerifierModel):
    checks: list[LLMClaimCheck]


class ClaimAssessment(BaseModel):
    claim_id: str
    claim_text: str
    status: ClaimVerificationStatus
    evidence_ids: list[str] = Field(default_factory=list)
    propositions: list[LLMPropositionCheck] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """Deterministic claim-level verification record."""

    verifier_model: Optional[str] = None
    verifier_prompt_version: Optional[str] = None
    status: WorkflowOutcome
    action: ClaimAction
    claims_checked: int = Field(ge=0)
    claims_llm_checked: int = Field(default=0, ge=0)
    assessments: list[ClaimAssessment] = Field(default_factory=list)
    retry_used: bool = False
    latency_ms: Optional[int] = None
    @model_validator(mode="after")
    def _validate_action(self) -> "VerificationResult":
        allowed = {WorkflowOutcome.PASSED, WorkflowOutcome.PASSED_AFTER_RETRY}
        expected_action = (
            ClaimAction.ALLOW if self.status in allowed else ClaimAction.INTERVENE
        )
        if self.action is not expected_action:
            raise ValueError(f"{self.status.value} requires action={expected_action.value}")
        after_retry = self.status in {
            WorkflowOutcome.PASSED_AFTER_RETRY,
            WorkflowOutcome.PARTIAL_AFTER_RETRY,
        }
        if self.retry_used is not after_retry:
            raise ValueError("retry_used must match an after-retry workflow outcome")
        return self


FinalAnswer.model_rebuild()


# ---------------------------------------------------------------------------
# Log record (one line in reasoning_steps.jsonl)
# ---------------------------------------------------------------------------


class ReasoningLogRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    response_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tasks: list[RequestTask]
    knowledge_sources: list[KnowledgeSource]
    request_status: RequestStatus
    model: str
    prompt_version: str
    packet_hash: str
    n_evidence_items: int
    lanes_empty: list[str] = Field(default_factory=list)
    lane_failures: list[LaneFailure] = Field(default_factory=list)

    thinking_budget_tokens: int
    thinking_trace: Optional[str] = None    # logged, never returned to user
    raw_response: Optional[str] = None      # pre-parse model output, for debugging parse failures

    evidence_packet: Optional[dict] = None  # full EvidencePacket for offline claim faithfulness eval
    final_answer: Optional[FinalAnswer] = None  # None if the call failed terminally
    parse_retries: int = 0
    error: Optional[str] = None

    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    latency_ms: Optional[int] = None

    def to_jsonl_line(self) -> str:
        return self.model_dump_json(exclude_none=True)


# ---------------------------------------------------------------------------
# Deterministic citation validation
# ---------------------------------------------------------------------------

_EVIDENCE_ID = re.compile(r"EV-[STCDLM]-\d{3,}")
_INLINE_CITE = re.compile(
    r"\[\s*EV-[STCDLM]-\d{3,}(?:\s*,\s*EV-[STCDLM]-\d{3,})*\s*\]"
)


def _inline_citation_ids(text: str) -> list[str]:
    return [
        evidence_id
        for citation_group in _INLINE_CITE.findall(text)
        for evidence_id in _EVIDENCE_ID.findall(citation_group)
    ]


def validate_citations(synthesis: SynthesisOutput, packet: EvidencePacket) -> CitationCheck:
    """ID-level checks only. Does NOT check semantic support."""
    known = packet.evidence_ids
    listed = set(synthesis.citations)
    inline = set(_inline_citation_ids(synthesis.answer))

    unknown = sorted((listed | inline) - known)
    missing_from_list = sorted(inline - listed)
    never_used = sorted(listed - inline)

    uncited = (
        len(inline) == 0
        and synthesis.abstention is None
        and synthesis.confidence != ConfidenceLabel.NOT_ASSESSABLE
    )

    return CitationCheck(
        all_cited_ids_exist=len(unknown) == 0,
        unknown_ids=unknown,
        inline_not_in_citations=missing_from_list,
        citations_not_inline=never_used,
        uncited_answer=uncited,
    )


# ---------------------------------------------------------------------------
# Verifier layer 2: claim extraction (deterministic, no model call)
# ---------------------------------------------------------------------------

_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_MARKDOWN_BULLET = re.compile(r"^(?:[-*+] |\d+[.)]\s+)")
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^\|?(?:\s*:?-+:?\s*\|)+\s*$")
_MARKDOWN_HORIZONTAL_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_MARKDOWN_BOLD_HEADING = re.compile(r"^(?:\d+[.)]\s+)?\*\*[^*]+\*\*$")
_MARKDOWN_INLINE_BOLD_HEADING = re.compile(r"^\*\*(?:\d+[.)]\s*)?[^*]+\*\*\s*")
_MARKDOWN_BOLD_OPENING_FRAGMENT = re.compile(r"^\*\*\s*\d+[.)]?\s*$")
_MARKDOWN_BOLD_CLOSING_FRAGMENT = re.compile(r"^[^.!?\[]+\*\*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

def _is_formatting_fragment(text: str) -> bool:
    return bool(
        _MARKDOWN_BOLD_HEADING.match(text)
        or _MARKDOWN_BOLD_OPENING_FRAGMENT.match(text)
        or _MARKDOWN_BOLD_CLOSING_FRAGMENT.match(text)
    )


def _answer_segments(answer: str) -> list[tuple[str, int, int]]:
    """Split Markdown into material units, retaining their block and section."""
    segments: list[tuple[str, int, int]] = []
    section_index = 0
    for block_index, block in enumerate(re.split(r"\n\s*\n", answer.strip())):
        prose_lines: list[str] = []

        def _flush_prose() -> None:
            if prose_lines:
                paragraph = " ".join(prose_lines)
                for sentence in _SENTENCE_SPLIT.split(paragraph):
                    sentence = sentence.strip()
                    if sentence and not _is_formatting_fragment(sentence):
                        segments.append((sentence, block_index, section_index))
                prose_lines.clear()

        lines = [raw_line.strip() for raw_line in block.splitlines()]
        for index, line in enumerate(lines):
            inline_heading = _MARKDOWN_INLINE_BOLD_HEADING.match(line)
            if inline_heading and inline_heading.end() < len(line):
                _flush_prose()
                section_index += 1
                line = line[inline_heading.end():].strip()
            is_table_header = (
                line.startswith("|") and line.endswith("|")
                and index + 1 < len(lines)
                and _MARKDOWN_TABLE_SEPARATOR.match(lines[index + 1]) is not None
            )
            is_section_heading = _MARKDOWN_HEADING.match(line) or _is_formatting_fragment(line)
            if is_section_heading:
                _flush_prose()
                section_index += 1
                continue
            if (
                not line
                or _MARKDOWN_TABLE_SEPARATOR.match(line)
                or _MARKDOWN_HORIZONTAL_RULE.match(line)
                or is_table_header
            ):
                continue
            if _MARKDOWN_BULLET.match(line):
                _flush_prose()
                bullet_text = _MARKDOWN_BULLET.sub("", line, count=1).strip()
                if not _is_formatting_fragment(bullet_text):
                    segments.append((bullet_text, block_index, section_index))
            elif line.startswith("|") and line.endswith("|"):
                _flush_prose()
                segments.append((line, block_index, section_index))
            else:
                prose_lines.append(line)
        _flush_prose()
    return segments


def extract_claim_units(synthesis: SynthesisOutput) -> list[ClaimUnit]:
    """Material Markdown units plus structured causal-claim statements."""
    units: list[ClaimUnit] = []
    seq = 0

    def _mint() -> str:
        nonlocal seq
        seq += 1
        return f"CLM-{seq:03d}"

    segments = _answer_segments(synthesis.answer)
    cited_by_block: dict[int, list[str]] = {}
    cited_by_section: dict[int, list[str]] = {}
    for segment, block_index, section_index in segments:
        for evidence_id in _inline_citation_ids(segment):
            cited_by_block.setdefault(block_index, []).append(evidence_id)
            cited_by_section.setdefault(section_index, []).append(evidence_id)

    for segment, block_index, section_index in segments:
        cited = _inline_citation_ids(segment)
        if cited:
            units.append(ClaimUnit(claim_id=_mint(), claim_text=segment, cited_evidence_ids=cited))
        else:
            section_context = cited_by_section.get(section_index, [])
            if not section_context:
                section_context = (
                    cited_by_section.get(section_index + 1, [])
                    or cited_by_section.get(section_index - 1, [])
                )
            units.append(
                ClaimUnit(
                    claim_id=_mint(),
                    claim_text=segment,
                    cited_evidence_ids=[],
                    context_evidence_ids=list(dict.fromkeys(
                        cited_by_block.get(block_index, []) + section_context
                    ))[:4],
                    origin="uncited_factual",
                )
            )

    return units


def build_llm_check_payload(
    units: list[ClaimUnit],
    packet: EvidencePacket,
) -> list[dict]:
    """Isolated {claim, evidence_content} pairs for the verifier call.
    The verifier must never see the full draft answer, only these pairs."""
    by_id = {i.evidence_id: i for i in packet.items}
    payload = []
    for unit in units:
        evidence_ids = unit.cited_evidence_ids or unit.context_evidence_ids
        evidence = [
            {"evidence_id": eid, "content": by_id[eid].content}
            for eid in evidence_ids
            if eid in by_id
        ]
        payload.append({
            "claim_id": unit.claim_id,
            "claim_text": unit.claim_text,
            "evidence": evidence,
        })
    return payload


# ---------------------------------------------------------------------------
# Helper: mint evidence IDs in the packet assembler
# ---------------------------------------------------------------------------


class EvidenceIdMinter:
    """Per-run counter so IDs are stable within a run and readable in logs.

        minter = EvidenceIdMinter()
        eid = minter.mint(SourceType.DOCUMENT)   # 'EV-D-001'
    """

    def __init__(self) -> None:
        self._counters: dict[SourceType, int] = {}

    def mint(self, source_type: SourceType) -> str:
        n = self._counters.get(source_type, 0) + 1
        self._counters[source_type] = n
        return f"EV-{source_type.value}-{n:03d}"


# ---------------------------------------------------------------------------
# JSON Schema export for the Anthropic structured-output tool definition
# ---------------------------------------------------------------------------


def synthesis_output_json_schema() -> dict:
    """Feed this to the API as the output schema so Claude is constrained
    to SynthesisOutput. FinalAnswer fields are stamped by code afterward."""
    return SynthesisOutput.model_json_schema()

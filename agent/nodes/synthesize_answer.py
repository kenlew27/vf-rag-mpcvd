"""Synthesis node — builds EvidencePacket from subagent parcels and calls Sonnet 4.6."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from agent.request_plan import KnowledgeSource, RequestStatus, RequestTask
from agent.schemas import (
    ConfidenceLabel,
    EvidenceIdMinter,
    EvidenceItem,
    EvidencePacket,
    FinalAnswer,
    LaneFailure,
    Provenance,
    ReasoningLogRecord,
    ScopedAbstention,
    SourceType,
    SynthesisOutput,
    validate_citations,
)
from agent.state import AgentState
from agent.synthesis.normalization import normalize_evidence_packets
from agent.verifier_feedback import build_revision_feedback

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "synthesize_answer.md"
_GENERAL_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "general_answer.md"
_LOG_PATH = os.environ.get("REASONING_LOG_PATH", "reasoning_steps.jsonl")

_DEFAULT_MODEL = "claude-sonnet-4-6"

_THINKING_BUDGET: dict[RequestTask, int] = {
    RequestTask.GENERATE_HYPOTHESIS: 10000,
    RequestTask.COMPARE: 10000,
}
_DEFAULT_BUDGET = 3000


def synthesize_answer(state: AgentState, client: Any = None, model: str | None = None) -> dict:
    """Build EvidencePacket, call Sonnet 4.6 with extended thinking, create and log FinalAnswer."""
    model = model or os.getenv("ANTHROPIC_MODEL") or _DEFAULT_MODEL

    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
        from anthropic import Anthropic
        client = Anthropic()

    plan = state.request_plan
    if plan is None:
        raise ValueError("A request plan is required before synthesis")
    thinking_budget = max(
        (_THINKING_BUDGET.get(task, _DEFAULT_BUDGET) for task in plan.tasks),
        default=_DEFAULT_BUDGET,
    )

    # Reuse packet on retry so evidence is consistent across attempts
    if state.evidence_packet is not None:
        packet = EvidencePacket.model_validate(state.evidence_packet)
    else:
        packet = _build_packet(state)

    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt_version = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]

    if plan.status is not RequestStatus.READY:
        final_answer = _with_database_coverage_notice(
            _status_answer(packet, plan.status, plan.reasons, model, prompt_version),
            state.database_evidence,
        )
        _append_log(ReasoningLogRecord(
            run_id=packet.run_id,
            response_id=final_answer.response_id,
            tasks=packet.tasks,
            knowledge_sources=packet.knowledge_sources,
            request_status=packet.request_status,
            model=model,
            prompt_version=prompt_version,
            packet_hash=packet.packet_hash(),
            n_evidence_items=0,
            lanes_empty=packet.lanes_empty,
            lane_failures=packet.lane_failures,
            thinking_budget_tokens=thinking_budget,
            final_answer=final_answer,
        ))
        return {
            "final_answer": final_answer,
            "evidence_packet": json.loads(packet.model_dump_json()),
        }

    if packet.is_empty() and KnowledgeSource.GENERAL_KNOWLEDGE in plan.knowledge_sources:
        return _general_answer(state, packet, client, model, thinking_budget)

    if packet.is_empty():
        final_answer = _with_database_coverage_notice(
            _empty_abstention(packet, model, prompt_version),
            state.database_evidence,
        )
        _append_log(ReasoningLogRecord(
            run_id=packet.run_id,
            response_id=final_answer.response_id,
            tasks=packet.tasks,
            knowledge_sources=packet.knowledge_sources,
            request_status=packet.request_status,
            model=model,
            prompt_version=prompt_version,
            packet_hash=packet.packet_hash(),
            n_evidence_items=0,
            lanes_empty=packet.lanes_empty,
            lane_failures=packet.lane_failures,
            thinking_budget_tokens=thinking_budget,
            final_answer=final_answer,
        ))
        return {
            "final_answer": final_answer,
            "evidence_packet": json.loads(packet.model_dump_json()),
        }

    prior_verification = (
        state.final_answer.verification if state.final_answer is not None else None
    )
    user_content = _build_user_message(packet, prior_verification)

    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": thinking_budget},
        system=prompt_text,
        messages=[{"role": "user", "content": user_content}],
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    thinking_trace = ""
    raw_response = ""
    for block in getattr(response, "content", []):
        btype = _attr(block, "type")
        if btype == "thinking":
            thinking_trace = _attr(block, "thinking") or ""
        elif btype == "text":
            raw_response = _attr(block, "text") or ""

    synthesis = _parse_synthesis_output(raw_response)
    final_answer = _with_database_coverage_notice(
        FinalAnswer.from_synthesis(synthesis, packet, model, prompt_version),
        state.database_evidence,
    )

    usage = getattr(response, "usage", None)
    _append_log(ReasoningLogRecord(
        run_id=packet.run_id,
        response_id=final_answer.response_id,
        tasks=packet.tasks,
        knowledge_sources=packet.knowledge_sources,
        request_status=packet.request_status,
        model=model,
        prompt_version=prompt_version,
        packet_hash=packet.packet_hash(),
        n_evidence_items=len(packet.items),
        lanes_empty=packet.lanes_empty,
        lane_failures=packet.lane_failures,
        thinking_budget_tokens=thinking_budget,
        thinking_trace=thinking_trace,
        raw_response=raw_response,
        evidence_packet=json.loads(packet.model_dump_json()),
        final_answer=final_answer,
        parse_retries=0,
        usage_input_tokens=_attr(usage, "input_tokens"),
        usage_output_tokens=_attr(usage, "output_tokens"),
        latency_ms=latency_ms,
    ))

    logger.info(
        "synthesize_answer | run_id: %s | tasks: %s | items: %d | confidence: %s | latency_ms: %d",
        packet.run_id,
        [task.value for task in packet.tasks],
        len(packet.items),
        synthesis.confidence.value,
        latency_ms,
    )

    return {
        "final_answer": final_answer,
        "evidence_packet": json.loads(packet.model_dump_json()),
    }


def _build_packet(state: AgentState) -> EvidencePacket:
    minter = EvidenceIdMinter()
    items: list[EvidenceItem] = []

    # Use supervisor_meta for authoritative lane tracking when available.
    # supervisor_meta.agents_paged = what was called; agents_empty = called but returned nothing.
    meta = state.supervisor_meta or {}
    agents_paged = meta.get("agents_paged", [])
    agents_empty = meta.get("agents_empty", [])
    lane_failures = [
        LaneFailure.model_validate(item)
        for item in meta.get("lane_failures", [])
    ]

    # Convert agent names → lane names for EvidencePacket
    def _agent_to_lane(agent_name: str) -> str:
        if agent_name == "table_agent":
            return "structured"
        return agent_name.replace("_document_agent", "")

    if agents_paged:
        lanes_attempted = [_agent_to_lane(a) for a in agents_paged]
        lanes_empty = [_agent_to_lane(a) for a in agents_empty]
    else:
        # Fallback: infer from state if supervisor_meta not populated
        lanes_attempted = []
        lanes_empty = []
        if state.database_evidence:
            lanes_attempted.append("structured")
        for scope in (state.document_evidence or {}):
            lanes_attempted.append(scope)

    db_evidence = state.database_evidence or {}
    database_completeness: dict[str, Any] = {}
    if db_evidence:
        completeness = normalize_evidence_packets(
            None,
            db_evidence,
            question=state.query.raw_text,
        ).database_completeness.model_dump(mode="json")
        planner_table = db_evidence.get("planner_table")
        if isinstance(planner_table, Mapping):
            for key in ("result_limit", "truncated"):
                if key in planner_table:
                    completeness[key] = planner_table[key]
        database_completeness = completeness
        evidence_rows = db_evidence.get("evidence_rows") or state.bigquery_results or []
        context_rows = db_evidence.get("context_rows") or []
        rows = [
            {
                **(dict(context_rows[index]) if index < len(context_rows) and isinstance(context_rows[index], Mapping) else {}),
                **dict(row),
            }
            for index, row in enumerate(evidence_rows)
            if isinstance(row, Mapping)
        ]
        for i, row in enumerate(rows):
            content = "\n".join(f"{k}: {v}" for k, v in row.items() if v is not None)
            items.append(EvidenceItem(
                evidence_id=minter.mint(SourceType.STRUCTURED),
                source_type=SourceType.STRUCTURED,
                source_label=row.get("Sample ID") or f"BigQuery row {i + 1}",
                content=content,
                provenance=Provenance(
                    origin_agent=db_evidence.get("agent", "table_agent"),
                    retrieval_query=db_evidence.get("question"),
                    source_ref="bigquery",
                    page_or_row=str(i + 1),
                ),
            ))

    for scope, parcel in (state.document_evidence or {}).items():
        source_type = SourceType.DOCUMENT if scope == "internal" else SourceType.LITERATURE
        contexts = parcel.get("contexts") or []
        for ctx in contexts:
            ctx_meta = ctx.get("metadata") or {}
            items.append(EvidenceItem(
                evidence_id=minter.mint(source_type),
                source_type=source_type,
                source_label=ctx_meta.get("title") or f"{scope} doc {ctx.get('context_id', '?')}",
                content=ctx.get("text", ""),
                provenance=Provenance(
                    origin_agent=parcel.get("agent", f"{scope}_document_agent"),
                    retrieval_query=parcel.get("rewritten_query") or parcel.get("question"),
                    retriever_rank=ctx.get("rank"),
                    reranker_score=ctx.get("retrieval_score"),
                    source_ref=ctx.get("document_id"),
                    page_or_row=str(ctx.get("context_id", "")),
                ),
            ))

    return EvidencePacket(
        run_id=state.run_id,
        query_raw=state.query.raw_text,
        tasks=state.request_plan.tasks,
        knowledge_sources=state.request_plan.knowledge_sources,
        request_status=state.request_plan.status,
        items=items,
        lanes_attempted=lanes_attempted,
        lanes_empty=lanes_empty,
        lane_failures=lane_failures,
        database_completeness=database_completeness,
    )


def _with_database_coverage_notice(
    final_answer: FinalAnswer,
    database_evidence: Mapping[str, Any] | None,
) -> FinalAnswer:
    resolution = (database_evidence or {}).get("output_resolution") or {}
    if not resolution.get("partial"):
        return final_answer
    available = [str(item.get("requested")) for item in resolution.get("resolved", []) if item.get("requested")]
    unavailable = [str(item) for item in resolution.get("unavailable", []) if str(item).strip()]
    if not available or not unavailable:
        return final_answer
    return final_answer.model_copy(update={
        "database_coverage_notice": (
            "Database field coverage: available—"
            + ", ".join(available)
            + "; unavailable—"
            + ", ".join(unavailable)
            + "."
        )
    })


def _build_user_message(packet: EvidencePacket, verification: Any | None) -> str:
    feedback = (
        [item.model_dump(mode="json") for item in build_revision_feedback(verification)]
        if verification is not None
        else []
    )
    return json.dumps(
        {
            "evidence_packet": packet.model_dump(mode="json"),
            "revision_feedback": feedback,
        },
        ensure_ascii=False,
    )


def _empty_abstention(packet: EvidencePacket, model: str, prompt_version: str) -> FinalAnswer:
    if packet.lane_failures:
        reasons = [failure.reason for failure in packet.lane_failures]
        synthesis = SynthesisOutput(
            answer="Evidence retrieval failed before a supported answer could be produced.\n"
            + "\n".join(f"- {reason}" for reason in reasons),
            citations=[],
            confidence=ConfidenceLabel.NOT_ASSESSABLE,
            confidence_basis=["One or more selected retrieval lanes failed."],
            abstention=ScopedAbstention(
                cannot_conclude=["The requested conclusion cannot be supported from the incomplete retrieval."],
                can_still_state=[],
                would_resolve=["Retry the failed retrieval lane or restore its infrastructure."],
            ),
        )
        return FinalAnswer.from_synthesis(synthesis, packet, model, prompt_version)
    synthesis = SynthesisOutput(
        answer="No evidence was retrieved for this query. Unable to provide a supported answer.",
        citations=[],
        confidence=ConfidenceLabel.NOT_ASSESSABLE,
        confidence_basis=["No evidence items were retrieved from any retrieval lane."],
        abstention=ScopedAbstention(
            cannot_conclude=["Anything — no evidence was retrieved."],
            can_still_state=[],
            would_resolve=["Successful data retrieval from BigQuery and/or document stores."],
        ),
    )
    return FinalAnswer.from_synthesis(synthesis, packet, model, prompt_version)


def _status_answer(
    packet: EvidencePacket,
    status: RequestStatus,
    reasons: list[str],
    model: str,
    prompt_version: str,
) -> FinalAnswer:
    status_details = {
        RequestStatus.NEEDS_CLARIFICATION: (
            "The request needs clarification before evidence can be gathered.",
            ["Specify the document, material, or comparison target needed to proceed."],
        ),
        RequestStatus.UNSUPPORTED_REQUEST: (
            "This request is outside the supported MDSAA and materials-research workflow.",
            ["Submit a MDSAA or materials-research request with a supported evidence source."],
        ),
        RequestStatus.INSUFFICIENT_EVIDENCE: (
            "The selected knowledge sources did not provide usable evidence for this request.",
            ["Provide additional source material or broaden the selected evidence sources."],
        ),
        RequestStatus.EXECUTION_FAILED: (
            "A selected evidence retrieval lane failed before the request could be completed.",
            ["Retry the request; if the failure persists, contact the materials support team."],
        ),
    }
    summary, would_resolve = status_details[status]
    details = reasons or [summary]
    synthesis = SynthesisOutput(
        answer=summary + "\n" + "\n".join(f"- {reason}" for reason in details),
        citations=[],
        confidence=ConfidenceLabel.NOT_ASSESSABLE,
        confidence_basis=[summary],
        abstention=ScopedAbstention(
            cannot_conclude=details,
            can_still_state=[],
            would_resolve=would_resolve,
        ),
    )
    return FinalAnswer.from_synthesis(synthesis, packet, model, prompt_version)


def _general_answer(
    state: AgentState,
    packet: EvidencePacket,
    client: Any,
    model: str,
    thinking_budget: int,
) -> dict:
    prompt_text = _GENERAL_PROMPT_PATH.read_text(encoding="utf-8")
    prompt_version = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
    start = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=prompt_text,
        messages=[{"role": "user", "content": state.query.raw_text}],
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    raw_response = "".join(_attr(block, "text") or "" for block in getattr(response, "content", []))
    synthesis = _parse_synthesis_output(raw_response)
    final_answer = _with_database_coverage_notice(
        FinalAnswer.from_synthesis(synthesis, packet, model, prompt_version),
        state.database_evidence,
    )
    final_answer = final_answer.model_copy(
        update={
            "citation_check": final_answer.citation_check.model_copy(update={"uncited_answer": False})
        }
    )
    usage = getattr(response, "usage", None)
    _append_log(ReasoningLogRecord(
        run_id=packet.run_id,
        response_id=final_answer.response_id,
        tasks=packet.tasks,
        knowledge_sources=packet.knowledge_sources,
        request_status=packet.request_status,
        model=model,
        prompt_version=prompt_version,
        packet_hash=packet.packet_hash(),
        n_evidence_items=0,
        lanes_empty=packet.lanes_empty,
        lane_failures=packet.lane_failures,
        thinking_budget_tokens=thinking_budget,
        raw_response=raw_response,
        evidence_packet=json.loads(packet.model_dump_json()),
        final_answer=final_answer,
        usage_input_tokens=_attr(usage, "input_tokens"),
        usage_output_tokens=_attr(usage, "output_tokens"),
        latency_ms=latency_ms,
    ))
    return {
        "final_answer": final_answer,
        "evidence_packet": json.loads(packet.model_dump_json()),
    }


def _append_log(record: ReasoningLogRecord) -> None:
    from storage.agent_log import append_run_log
    try:
        append_run_log(
            json.loads(record.to_jsonl_line()),
            fallback_path=_LOG_PATH,
        )
    except Exception as exc:
        logger.warning("synthesize_answer | could not write reasoning log: %s", exc)


def _parse_synthesis_output(raw_response: str) -> SynthesisOutput:
    text = _strip_json_fence(raw_response)
    try:
        return SynthesisOutput.model_validate_json(text)
    except ValidationError as exc:
        repaired = _repair_malformed_answer_string(text)
        if repaired is None:
            raise
        try:
            output = SynthesisOutput.model_validate(json.loads(repaired))
        except (ValidationError, json.JSONDecodeError) as repair_exc:
            raise exc from repair_exc
        logger.warning("synthesize_answer | repaired malformed JSON answer string")
        return output


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped

    opener = lines[0].strip().lower()
    if opener not in {"```", "```json"}:
        return stripped

    return "\n".join(lines[1:-1]).strip()


def _repair_malformed_answer_string(text: str) -> str | None:
    prefix_match = re.search(r'("answer"\s*:\s*)"', text)
    if prefix_match is None:
        return None

    value_start = prefix_match.end()
    delimiter_match = re.search(r',\s*"citations"\s*:', text[value_start:])
    if delimiter_match is None:
        return None

    value_end = value_start + delimiter_match.start()
    raw_value = text[value_start:value_end].strip()
    if raw_value.endswith('"'):
        raw_value = raw_value[:-1]

    answer = _decode_malformed_json_string_fragment(raw_value)
    return text[:prefix_match.start()] + prefix_match.group(1) + json.dumps(answer) + text[value_end:]


def _decode_malformed_json_string_fragment(fragment: str) -> str:
    escaped_fragment = _escape_json_string_fragment(fragment)
    try:
        return json.loads(f'"{escaped_fragment}"')
    except json.JSONDecodeError:
        return fragment.replace("\r\n", "\n").replace("\r", "\n")


def _escape_json_string_fragment(fragment: str) -> str:
    chars: list[str] = []
    escaped = False
    for char in fragment:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            chars.append(char)
            escaped = True
            continue
        if char == '"':
            chars.append('\\"')
        elif char == "\n":
            chars.append("\\n")
        elif char == "\r":
            chars.append("\\n")
        elif ord(char) < 0x20:
            chars.append(f"\\u{ord(char):04x}")
        else:
            chars.append(char)
    if escaped:
        chars.append("\\")
    return "".join(chars)


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)

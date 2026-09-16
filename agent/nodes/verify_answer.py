"""Claim-level evidence verifier with deterministic workflow policy."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent.request_plan import KnowledgeSource, RequestStatus
from agent.schemas import (
    ClaimAction,
    ClaimAssessment,
    ClaimUnit,
    EvidencePacket,
    LLMVerifierResponse,
    ClaimVerificationStatus,
    VerifierInputClaim,
    VerificationResult,
    WorkflowOutcome,
    build_llm_check_payload,
    extract_claim_units,
    validate_citations,
)
from agent.state import AgentState
from agent.verifier_terminal import build_terminal_synthesis

logger = logging.getLogger(__name__)
_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "verifier_prompt.md"
_LOG_PATH = os.environ.get("VERIFICATION_LOG_PATH", "verification_steps.jsonl")
_DEFAULT_VERIFIER_MODEL = "claude-haiku-4-5-20251001"
VERIFIER_BATCH_SIZE = 2
_VERIFIER_MAX_TOKENS = 4096
_VERIFIER_THINKING = {"type": "enabled", "budget_tokens": 2048, "display": "omitted"}
_RECOVERY_INSTRUCTION = """

## Structured-output recovery

Your previous response failed schema, claim-coverage, or evidence-boundary
validation. Re-evaluate the same input and return one complete, schema-valid
check per claim. For every check, `evidence_ids` may only contain IDs provided
in the input `evidence` list for that same `claim_id`; never copy or carry an
ID from another claim. For `unsupported` propositions with supplied evidence,
cite the parcel(s) you examined. Do not omit, duplicate, reorder, or add claim
IDs.
"""

def verify_answer(state: AgentState, client: Any = None, verifier_model: str | None = None) -> dict:
    """Verify visible answer claims; policy and retry outcome are deterministic."""
    if state.final_answer is None or state.evidence_packet is None:
        return {}
    if _uses_general_knowledge_only(state):
        result = VerificationResult(status=WorkflowOutcome.PASSED, action=ClaimAction.ALLOW, claims_checked=0)
        _append_log(state, result)
        return {"final_answer": state.final_answer.model_copy(update={"verification": result})}

    verifier_model = verifier_model or _DEFAULT_VERIFIER_MODEL
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    start = time.monotonic()
    packet = EvidencePacket.model_validate(state.evidence_packet)
    units = extract_claim_units(state.final_answer.synthesis)
    payload = build_llm_check_payload(units, packet)
    if payload:
        try:
            assessments, checked, _raw = run_verifier_payload(
                payload, client=client, verifier_model=verifier_model, _prompt_text=prompt
            )
            action = (
                ClaimAction.INTERVENE
                if any(a.status is not ClaimVerificationStatus.SUPPORTED for a in assessments)
                else ClaimAction.ALLOW
            )
        except RuntimeError as exc:
            logger.error("verify_answer | verifier exhausted all retries, blocking answer: %s", exc)
            assessments, checked = [], len(units)
            action = ClaimAction.INTERVENE
    else:
        assessments, checked = [], 0
        action = ClaimAction.ALLOW
    retry = state.synthesis_retry_count > 0
    if action is ClaimAction.ALLOW:
        outcome = WorkflowOutcome.PASSED_AFTER_RETRY if retry else WorkflowOutcome.PASSED
    elif retry:
        outcome = WorkflowOutcome.PARTIAL_AFTER_RETRY
    else:
        outcome = WorkflowOutcome.RETRY_TRIGGERED
    safe_synthesis = None
    if outcome is WorkflowOutcome.PARTIAL_AFTER_RETRY:
        safe_synthesis = build_terminal_synthesis(assessments, state.final_answer.synthesis)
    result = VerificationResult(
        verifier_model=verifier_model,
        verifier_prompt_version=hashlib.sha256(prompt.encode()).hexdigest()[:12],
        status=outcome,
        action=action,
        claims_checked=len(units),
        claims_llm_checked=checked,
        assessments=assessments,
        retry_used=retry,
        latency_ms=int((time.monotonic() - start) * 1000),
    )
    if safe_synthesis is None:
        answer = state.final_answer.model_copy(update={"verification": result})
    else:
        answer = state.final_answer.model_copy(update={
            "synthesis": safe_synthesis,
            "citation_check": validate_citations(safe_synthesis, packet),
            "verification": result,
        })
    _append_log(state, result)
    if outcome is WorkflowOutcome.RETRY_TRIGGERED:
        return {"final_answer": answer, "synthesis_retry_count": 1}
    return {"final_answer": answer}


def run_verifier_payload(
    payload: list[dict], *, client: Any = None, verifier_model: str | None = None,
    _units_by_id: dict[str, ClaimUnit] | None = None, _prompt_text: str | None = None,
) -> tuple[list[ClaimAssessment], int, str]:
    """Validate the exact input, invoke the LLM, then deterministically derive policy."""
    inputs = _parse_input_payload(payload)
    if client is None and os.environ.get("ANTHROPIC_API_KEY"):
        client = _build_anthropic_client()
    if client is None:
        raise RuntimeError("verifier LLM is unavailable; inject a verifier client")
    prompt = _prompt_text or _PROMPT_PATH.read_text(encoding="utf-8")
    assessments: list[ClaimAssessment] = []
    raw_responses: list[str] = []
    for start in range(0, len(inputs), VERIFIER_BATCH_SIZE):
        end = start + VERIFIER_BATCH_SIZE
        batch_assessments, raw = _run_verifier_batch(
            inputs[start:end],
            payload[start:end],
            client=client,
            verifier_model=verifier_model,
            prompt=prompt,
        )
        assessments.extend(batch_assessments)
        raw_responses.append(raw)
    return assessments, len(assessments), "\n".join(raw_responses)


def _run_verifier_batch(
    inputs: list[VerifierInputClaim],
    payload: list[dict],
    *,
    client: Any,
    verifier_model: str | None,
    prompt: str,
) -> tuple[list[ClaimAssessment], str]:
    """Verify one bounded claim batch, retrying invalid results with isolation."""
    by_id = {claim.claim_id: claim for claim in inputs}
    last_error: RuntimeError | None = None
    for attempt in range(2):
        try:
            response = client.messages.parse(
                model=verifier_model or _DEFAULT_VERIFIER_MODEL,
                max_tokens=_VERIFIER_MAX_TOKENS,
                thinking=_VERIFIER_THINKING,
                system=prompt if attempt == 0 else prompt + _RECOVERY_INSTRUCTION,
                messages=[{"role": "user", "content": json.dumps(payload)}],
                output_format=LLMVerifierResponse,
            )
            raw = "".join(
                (_attr(block, "text") or "")
                for block in getattr(response, "content", [])
            )
            parsed_output = getattr(response, "parsed_output", None)
            if parsed_output is None:
                raise RuntimeError(
                    "verifier LLM response was invalid: missing parsed output"
                )
            checks = parsed_output.checks
            _validate_claim_coverage(inputs, checks)
            assessments = [_derive_assessment(check.claim_id, by_id[check.claim_id], check)
                           for check in checks]
            return assessments, raw
        except ValidationError as exc:
            last_error = RuntimeError(f"verifier LLM response was invalid: {exc}")
        except RuntimeError as exc:
            last_error = exc
        if attempt == 0:
            logger.warning(
                "verifier batch output was invalid; retrying once: %s",
                last_error,
            )
    assert last_error is not None
    if len(inputs) > 1:
        logger.warning(
            "verifier batch remained invalid; retrying each claim independently: %s",
            last_error,
        )
        isolated_results = [
            _run_verifier_batch(
                [claim],
                [raw_claim],
                client=client,
                verifier_model=verifier_model,
                prompt=prompt,
            )
            for claim, raw_claim in zip(inputs, payload)
        ]
        return (
            [assessment for assessments, _raw in isolated_results for assessment in assessments],
            "\n".join(raw for _assessments, raw in isolated_results),
        )
    raise last_error


def _parse_input_payload(payload: list[dict]) -> list[VerifierInputClaim]:
    if not isinstance(payload, list):
        raise ValueError("verifier input must be a JSON array")
    claims: list[VerifierInputClaim] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        try:
            claim = VerifierInputClaim.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"invalid verifier claim {index}: {exc}") from exc
        if claim.claim_id in seen:
            raise ValueError(f"duplicate verifier claim_id: {claim.claim_id}")
        seen.add(claim.claim_id)
        claims.append(claim)
    return claims


def _validate_claim_coverage(inputs: list[VerifierInputClaim], checks: list[Any]) -> None:
    expected_ids = [claim.claim_id for claim in inputs]
    expected = set(expected_ids)
    ids = [check.claim_id for check in checks]
    received = set(ids)
    missing, unexpected = sorted(expected - received), sorted(received - expected)
    duplicate = sorted({claim_id for claim_id in ids if ids.count(claim_id) > 1})
    if missing or unexpected or duplicate:
        parts = []
        if missing: parts.append("missing claim checks: " + ", ".join(missing))
        if unexpected: parts.append("unexpected claim checks: " + ", ".join(unexpected))
        if duplicate: parts.append("duplicate claim checks: " + ", ".join(duplicate))
        raise RuntimeError("verifier LLM response was incomplete: " + "; ".join(parts))
    if ids != expected_ids:
        raise RuntimeError("verifier LLM response reordered claim checks")


def _derive_assessment(
    claim_id: str,
    claim: VerifierInputClaim,
    check: Any,
) -> ClaimAssessment:
    allowed_ids = {parcel.evidence_id for parcel in claim.evidence}
    for proposition in check.propositions:
        unknown = sorted(set(proposition.evidence_ids) - allowed_ids)
        if unknown:
            raise RuntimeError(
                f"verifier proposition for {claim_id} referenced evidence outside its claim: "
                + ", ".join(unknown)
            )
        if (
            proposition.status is ClaimVerificationStatus.UNSUPPORTED
            and allowed_ids
            and not proposition.evidence_ids
        ):
            raise RuntimeError(
                f"{proposition.status.value} proposition for {claim_id} must cite "
                "at least one searched evidence parcel"
            )

    contradicted = [
        proposition for proposition in check.propositions
        if proposition.status is ClaimVerificationStatus.CONTRADICTED
    ]
    supported = [
        proposition for proposition in check.propositions
        if proposition.status is ClaimVerificationStatus.SUPPORTED
    ]
    if contradicted:
        status = ClaimVerificationStatus.CONTRADICTED
        evidence_ids = _proposition_evidence_ids(contradicted)
    elif len(supported) == len(check.propositions):
        status = ClaimVerificationStatus.SUPPORTED
        evidence_ids = _proposition_evidence_ids(supported)
    else:
        status = ClaimVerificationStatus.UNSUPPORTED
        evidence_ids = _proposition_evidence_ids([
            proposition for proposition in check.propositions
            if proposition.status is ClaimVerificationStatus.UNSUPPORTED
        ])
    return ClaimAssessment(
        claim_id=claim_id,
        claim_text=claim.claim_text,
        status=status,
        evidence_ids=evidence_ids,
        propositions=check.propositions,
    )


def _proposition_evidence_ids(propositions: list[Any]) -> list[str]:
    return list(dict.fromkeys(
        evidence_id
        for proposition in propositions
        for evidence_id in proposition.evidence_ids
    ))


def _uses_general_knowledge_only(state: AgentState) -> bool:
    plan = state.request_plan
    return bool(plan and plan.status is RequestStatus.READY and plan.knowledge_sources == [KnowledgeSource.GENERAL_KNOWLEDGE])


def _build_anthropic_client() -> Any:
    from anthropic import Anthropic
    return Anthropic()


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _append_log(state: AgentState, result: VerificationResult) -> None:
    record = {
        "run_id": state.final_answer.run_id if state.final_answer else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **result.model_dump(mode="json", exclude_none=True),
    }
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("verify_answer | could not write verification log: %s", exc)

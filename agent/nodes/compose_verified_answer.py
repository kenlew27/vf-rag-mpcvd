"""Compose concise prose from the final verifier's supported claims."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from agent.schemas import ClaimVerificationStatus, ExecutiveSummary, FinalAnswer, WorkflowOutcome
from agent.state import AgentState
from agent._utils import HASH_PREFIX_LEN, attr as _attr, strip_json_fence as _strip_json_fence

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "compose_verified_answer.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_INLINE_CITATION = re.compile(
    r"\[\s*(EV-[STCDLM]-\d{3,}(?:\s*,\s*EV-[STCDLM]-\d{3,})*)\s*\]"
)
_EVIDENCE_ID = re.compile(r"EV-[STCDLM]-\d{3,}")


def compose_verified_answer(
    state: AgentState,
    client: Any = None,
    model: str | None = None,
) -> dict:
    """Attach an executive summary without changing the verifier record."""
    final_answer = state.final_answer
    verification = final_answer.verification if final_answer is not None else None
    if final_answer is None or verification is None or verification.status == WorkflowOutcome.RETRY_TRIGGERED:
        return {}

    supported = [
        assessment
        for assessment in verification.assessments
        if assessment.status is ClaimVerificationStatus.SUPPORTED and assessment.evidence_ids
    ]
    if not supported:
        return {}

    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt_version = hashlib.sha256(prompt.encode()).hexdigest()[:HASH_PREFIX_LEN]
    summary_model = model or final_answer.model or _DEFAULT_MODEL
    try:
        if client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("summary LLM is unavailable")
            from anthropic import Anthropic
            client = Anthropic()
        response = client.messages.create(
            model=summary_model,
            max_tokens=2048,
            system=prompt,
            messages=[{
                "role": "user",
                "content": json.dumps(_summary_input(state, supported), ensure_ascii=False),
            }],
        )
        summary = _parse_summary(_response_text(response), _allowed_evidence_ids(supported), summary_model, prompt_version)
    except Exception as exc:
        logger.warning("compose_verified_answer | summary unavailable: %s", exc)
        return {}
    return {"final_answer": final_answer.model_copy(update={"executive_summary": summary})}


def _summary_input(state: AgentState, supported: list[Any]) -> dict[str, Any]:
    return {
        "question": state.query.raw_text,
        "supported_claims": [
            {
                "claim_text": assessment.claim_text,
                "evidence_ids": assessment.evidence_ids,
                "propositions": [proposition.model_dump(mode="json") for proposition in assessment.propositions],
            }
            for assessment in supported
        ],
    }


def _allowed_evidence_ids(supported: list[Any]) -> set[str]:
    return {
        evidence_id
        for assessment in supported
        for evidence_id in assessment.evidence_ids
    }


def _response_text(response: Any) -> str:
    return "".join(
        _attr(block, "text") or ""
        for block in getattr(response, "content", [])
        if _attr(block, "type") == "text"
    )


def _parse_summary(
    raw: str,
    allowed_ids: set[str],
    model: str,
    prompt_version: str,
) -> ExecutiveSummary:
    payload = json.loads(_strip_json_fence(raw))
    if set(payload) != {"answer", "citations"}:
        raise ValueError("summary response must contain only answer and citations")
    answer = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("summary answer must be a non-empty string")
    if not isinstance(citations, list) or not all(isinstance(item, str) for item in citations):
        raise ValueError("summary citations must be a string list")
    inline_ids = _inline_citation_ids(answer)
    if not inline_ids or set(inline_ids) != set(citations):
        raise ValueError("summary inline citations must exactly match citations")
    if set(inline_ids) - allowed_ids:
        raise ValueError("summary cited evidence outside supported claims")
    return ExecutiveSummary(
        answer=answer.strip(),
        citations=citations,
        model=model,
        prompt_version=prompt_version,
    )


def _inline_citation_ids(answer: str) -> list[str]:
    return [
        evidence_id
        for group in _INLINE_CITATION.findall(answer)
        for evidence_id in _EVIDENCE_ID.findall(group)
    ]

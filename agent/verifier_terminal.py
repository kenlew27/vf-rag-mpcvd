"""Deterministic safe rendering after the single synthesis retry fails."""

from __future__ import annotations

import re
from typing import Any

from agent.schemas import ConfidenceLabel, ScopedAbstention, SynthesisOutput


_INLINE_CITATION = re.compile(
    r"\s*\[\s*EV-[STCDLM]-\d{3,}(?:\s*,\s*EV-[STCDLM]-\d{3,})*\s*\]"
)


def build_terminal_synthesis(
    assessments: list[Any],
    failed_synthesis: SynthesisOutput,
) -> SynthesisOutput:
    """Render only claim-level assessments that remain supported."""
    supported = [
        assessment
        for assessment in assessments
        if _enum_value(assessment.status) == "supported" and assessment.evidence_ids
    ]
    citations = list(dict.fromkeys(
        evidence_id for assessment in supported for evidence_id in assessment.evidence_ids
    ))
    if not supported:
        return SynthesisOutput(
            answer="The supplied evidence does not support a verified factual answer.",
            citations=[],
            confidence=ConfidenceLabel.NOT_ASSESSABLE,
            confidence_basis=[
                "No claim remained supported after the single revision."
            ],
            contradictions_noted=[],
            abstention=ScopedAbstention(
                cannot_conclude=[
                    "A verified factual conclusion from the supplied evidence."
                ],
            ),
        )
    return SynthesisOutput(
        answer=_render_answer(supported),
        citations=citations,
        confidence=ConfidenceLabel.LOW,
        confidence_basis=[
            "Only claims that remained supported after the single revision are shown."
        ],
        contradictions_noted=[],
        abstention=None,
    )


def _render_answer(supported: list[Any]) -> str:
    return "\n".join(
        f"- {_without_terminal_punctuation(_clean_text(assessment.claim_text))} "
        f"[{', '.join(assessment.evidence_ids)}]."
        for assessment in supported
    )


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", _INLINE_CITATION.sub("", text)).strip()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _without_terminal_punctuation(text: str) -> str:
    return text.rstrip().rstrip(".!?")

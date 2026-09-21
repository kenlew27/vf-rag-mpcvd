"""Deterministic synthesis feedback derived from claim-level verification."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RevisionFeedbackItem(BaseModel):
    """One actionable revision instruction for a failed claim."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    claim_text: str
    status: str
    evidence_ids: list[str]
    required_revision: str


_REQUIRED_REVISIONS = {
    "contradicted": (
        "Remove this claim or replace it only with a statement directly supported "
        "by the listed evidence."
    ),
    "unsupported": (
        "Remove this claim, qualify it as unknown, or narrow it to what the listed "
        "evidence establishes."
    ),
}


from agent.schemas import VerificationResult

def build_revision_feedback(verification: VerificationResult) -> list[RevisionFeedbackItem]:
    """Return one deterministic instruction for every non-supported claim."""
    feedback: list[RevisionFeedbackItem] = []
    for assessment in verification.assessments:
        status = _enum_value(assessment.status)
        if status == "supported":
            continue
        feedback.append(
            RevisionFeedbackItem(
                claim_id=assessment.claim_id,
                claim_text=assessment.claim_text,
                status=status,
                evidence_ids=list(assessment.evidence_ids),
                required_revision=_REQUIRED_REVISIONS[status],
            )
        )
    return feedback


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)

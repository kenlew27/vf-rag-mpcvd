"""Evidence synthesis pipeline orchestration."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from agent.synthesis.config import EvidencePlannerConfig, load_evidence_planner_config
from agent.synthesis.models import (
    EvidencePlan,
    EvidencePlanningRequest,
    NormalizedEvidenceBundle,
    SynthesisHypothesis,
    SynthesisPipelineResult,
    SynthesisRequest,
)
from agent.synthesis.normalization import normalize_evidence_packets
from agent.synthesis.planner import AnthropicEvidencePlanner, EvidencePlanner
from agent.synthesis.synthesizer import AnthropicEvidenceSynthesizer, EvidenceSynthesizer
from tools.timing import agent_timing, emit_agent_timing

logger = logging.getLogger(__name__)


class EvidenceSynthesisPipelineError(RuntimeError):
    """Raised when strict planner mode blocks synthesis fallback."""


def run_synthesis_pipeline(
    *,
    question: str,
    document_packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    database_packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    config: EvidencePlannerConfig | None = None,
    planner: EvidencePlanner | None = None,
    synthesizer: EvidenceSynthesizer | None = None,
    timing_context: Mapping[str, object] | None = None,
) -> SynthesisPipelineResult:
    """Normalize evidence, optionally plan once, then synthesize a hypothesis."""

    with agent_timing("synthesis.pipeline_total", timing_context=timing_context):
        runtime_config = config or load_evidence_planner_config()
        with agent_timing("synthesis.evidence_normalization", timing_context=timing_context):
            evidence_bundle = normalize_evidence_packets(
                document_packets,
                database_packets,
                question=question,
            )
        planner_metadata: dict[str, Any] = {
            "enabled": runtime_config.enabled,
            "attempted": False,
            "succeeded": False,
        }

        evidence_plan: EvidencePlan | None = None
        if runtime_config.enabled:
            planner_metadata["attempted"] = True
            try:
                with agent_timing(
                    "synthesis.evidence_planner",
                    timing_context=timing_context,
                    enabled=True,
                ):
                    evidence_plan = (planner or AnthropicEvidencePlanner()).plan(
                        EvidencePlanningRequest(question=question, evidence_bundle=evidence_bundle)
                    )
                planner_metadata["succeeded"] = True
            except Exception as exc:
                planner_metadata["error"] = str(exc)
                if runtime_config.strict_planner_failure:
                    raise EvidenceSynthesisPipelineError("Evidence planner failed in strict mode") from exc
                logger.warning("Evidence planner failed; synthesizing without a plan", exc_info=exc)
        else:
            emit_agent_timing(
                "synthesis.evidence_planner",
                elapsed_ms=0.0,
                timing_context=timing_context,
                status="skipped",
                enabled=False,
            )

        try:
            with agent_timing("synthesis.synthesizer", timing_context=timing_context):
                hypothesis = (synthesizer or AnthropicEvidenceSynthesizer()).synthesize(
                    SynthesisRequest(
                        question=question,
                        evidence_bundle=evidence_bundle,
                        evidence_plan=evidence_plan,
                    )
                )
                if not isinstance(hypothesis, SynthesisHypothesis):
                    hypothesis = SynthesisHypothesis.model_validate(hypothesis)
        except Exception as exc:
            raise EvidenceSynthesisPipelineError(f"Evidence synthesis failed: {exc}") from exc

        return SynthesisPipelineResult(
            evidence_bundle=evidence_bundle,
            evidence_plan=evidence_plan,
            synthesis_hypothesis=hypothesis,
            planner_metadata=planner_metadata,
        )

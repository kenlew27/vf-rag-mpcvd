"""Synthesizer protocol and Anthropic adapter for evidence synthesis."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent.synthesis import models as synthesis_models
from agent.synthesis.config import load_evidence_planner_config
from agent.synthesis.prompts import load_synthesizer_system_prompt
from agent.synthesis import _shared
from agent._utils import build_anthropic_client as _build_anthropic_client


class EvidenceSynthesizer(Protocol):
    """Synthesizes a claim-graph hypothesis from evidence packets."""

    def synthesize(self, request: Any) -> Any:
        """Return a SynthesisHypothesis for the request."""


class AnthropicEvidenceSynthesizer:
    """Anthropic-backed implementation of the evidence synthesizer protocol."""

    def __init__(
        self,
        *,
        client: Any = None,
        model: str | None = None,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
    ) -> None:
        config = load_evidence_planner_config()
        self.client = client
        self.model = model or config.synthesizer_model
        self.max_tokens = max_tokens or config.synthesizer_max_tokens
        self.system_prompt = (
            load_synthesizer_system_prompt() if system_prompt is None else system_prompt
        )

    def synthesize(self, request: Any) -> Any:
        if not self.model:
            raise ValueError("EVIDENCE_SYNTHESIZER_MODEL or ANTHROPIC_MODEL is required")

        client = self.client or _build_anthropic_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": _dump_contract(request)}],
            output_config=_shared.json_schema_output_config(synthesis_models.SynthesisHypothesis),
        )
        return synthesis_models.SynthesisHypothesis.model_validate(_shared.read_json_response(response, "synthesizer"))


def _dump_contract(value: Any) -> str:
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump())
    return json.dumps(value)


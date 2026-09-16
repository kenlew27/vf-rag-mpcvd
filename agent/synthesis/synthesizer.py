"""Synthesizer protocol and Anthropic adapter for evidence synthesis."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent.synthesis import models as synthesis_models
from agent.synthesis.config import load_evidence_planner_config
from agent.synthesis.prompts import load_synthesizer_system_prompt


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
            output_config=_json_schema_output_config(synthesis_models.SynthesisHypothesis),
        )
        return synthesis_models.SynthesisHypothesis.model_validate(_read_json_response(response))


def _build_anthropic_client() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
    from anthropic import Anthropic

    return Anthropic()


def _dump_contract(value: Any) -> str:
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump())
    return json.dumps(value)


def _json_schema_output_config(contract: Any) -> dict[str, Any] | None:
    if not hasattr(contract, "model_json_schema"):
        return None
    schema = _strict_object_schema(contract.model_json_schema())
    return {
        "format": {
            "type": "json_schema",
            "schema": schema,
        }
    }


def _strict_object_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        normalized = {key: _strict_object_schema(value) for key, value in schema.items()}
        if normalized.get("type") == "object":
            normalized["additionalProperties"] = False
        return normalized
    if isinstance(schema, list):
        return [_strict_object_schema(item) for item in schema]
    return schema


def _read_json_response(response: Any) -> dict[str, Any]:
    text = "".join(_read(block, "text", "") for block in getattr(response, "content", [])).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"Anthropic synthesizer returned invalid JSON at character {exc.pos}"
        if _read(response, "stop_reason") == "max_tokens":
            message += "; response reached max_tokens before completing JSON"
        raise ValueError(message) from exc


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

"""Planner protocol and Anthropic adapter for evidence planning."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent.synthesis import models as synthesis_models
from agent.synthesis.config import load_evidence_planner_config
from agent.synthesis.prompts import load_planner_system_prompt


class EvidencePlanner(Protocol):
    """Plans how retrieved evidence should be used for synthesis."""

    def plan(self, request: Any) -> Any:
        """Return an EvidencePlan for the request."""


class AnthropicEvidencePlanner:
    """Anthropic-backed implementation of the evidence planner protocol."""

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
        self.model = model or config.planner_model
        self.max_tokens = max_tokens or config.planner_max_tokens
        self.system_prompt = (
            load_planner_system_prompt() if system_prompt is None else system_prompt
        )

    def plan(self, request: Any) -> Any:
        if not self.model:
            raise ValueError("EVIDENCE_PLANNER_MODEL or ANTHROPIC_MODEL is required")

        client = self.client or _build_anthropic_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": _dump_planner_request(request)}],
            output_config=_json_schema_output_config(synthesis_models.EvidencePlan),
        )
        return synthesis_models.EvidencePlan.model_validate(_read_json_response(response))


def _build_anthropic_client() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
    from anthropic import Anthropic

    return Anthropic()


def _dump_planner_request(request: Any) -> str:
    bundle = _read(request, "evidence_bundle")
    evidence = [_compact_evidence(item) for item in _read(bundle, "evidence", [])]
    return json.dumps(
        {
            "question": _read(request, "question"),
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "evidence": evidence,
            "database_completeness": _dump_plain(_read(bundle, "database_completeness")),
            "missing_evidence": list(_read(bundle, "missing_evidence", []) or []),
        }
    )


def _compact_evidence(item: Any) -> dict[str, Any]:
    kind = _read(item, "evidence_kind")
    payload = _read(item, "payload", {}) or {}
    row = _read(item, "row", {}) or {}
    return {
        "evidence_id": _read(item, "evidence_id"),
        "evidence_kind": kind,
        "source_agent": _read(item, "source_agent"),
        "scope": _evidence_scope(item, payload),
        "score": _evidence_score(item),
        "title": _evidence_title(payload),
        "snippet": _evidence_snippet(kind, payload, row),
    }


def _evidence_scope(item: Any, payload: dict[str, Any]) -> Any:
    return (
        _read(item, "document_scope")
        or payload.get("database_scope")
        or payload.get("scope")
        or payload.get("table")
    )


def _evidence_score(item: Any) -> Any:
    relative_relevance = _read(item, "relative_relevance")
    if relative_relevance is not None:
        return relative_relevance
    return _read(item, "retrieval_score_raw")


def _evidence_title(payload: dict[str, Any]) -> Any:
    return (
        payload.get("title")
        or payload.get("document_title")
        or payload.get("source_title")
        or payload.get("name")
    )


def _evidence_snippet(kind: Any, payload: dict[str, Any], row: dict[str, Any]) -> str:
    if kind == "database":
        return _truncate(_compact_row(row), 240)
    return _truncate(str(payload.get("text") or ""), 240)


def _compact_row(row: dict[str, Any]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in row.items())


def _truncate(value: str, limit: int) -> str:
    return value[:limit]


def _dump_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


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
        message = f"Anthropic planner returned invalid JSON at character {exc.pos}"
        if _read(response, "stop_reason") == "max_tokens":
            message += "; response reached max_tokens before completing JSON"
        raise ValueError(message) from exc


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

"""Planner protocol and Anthropic adapter for evidence planning."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent.synthesis import models as synthesis_models
from agent.synthesis.config import load_evidence_planner_config
from agent.synthesis.prompts import load_planner_system_prompt
from agent.synthesis import _shared
from agent._utils import build_anthropic_client as _build_anthropic_client


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
            output_config=_shared.json_schema_output_config(synthesis_models.EvidencePlan),
        )
        return synthesis_models.EvidencePlan.model_validate(_shared.read_json_response(response, "planner"))


def _dump_planner_request(request: Any) -> str:
    bundle = _shared.read(request, "evidence_bundle")
    evidence = [_compact_evidence(item) for item in _shared.read(bundle, "evidence", [])]
    return json.dumps(
        {
            "question": _shared.read(request, "question"),
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "evidence": evidence,
            "database_completeness": _dump_plain(_shared.read(bundle, "database_completeness")),
            "missing_evidence": list(_shared.read(bundle, "missing_evidence", []) or []),
        }
    )


def _compact_evidence(item: Any) -> dict[str, Any]:
    kind = _shared.read(item, "evidence_kind")
    payload = _shared.read(item, "payload", {}) or {}
    row = _shared.read(item, "row", {}) or {}
    return {
        "evidence_id": _shared.read(item, "evidence_id"),
        "evidence_kind": kind,
        "source_agent": _shared.read(item, "source_agent"),
        "scope": _evidence_scope(item, payload),
        "score": _evidence_score(item),
        "title": _evidence_title(payload),
        "snippet": _evidence_snippet(kind, payload, row),
    }


def _evidence_scope(item: Any, payload: dict[str, Any]) -> Any:
    return (
        _shared.read(item, "document_scope")
        or payload.get("database_scope")
        or payload.get("scope")
        or payload.get("table")
    )


def _evidence_score(item: Any) -> Any:
    relative_relevance = _shared.read(item, "relative_relevance")
    if relative_relevance is not None:
        return relative_relevance
    return _shared.read(item, "retrieval_score_raw")


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


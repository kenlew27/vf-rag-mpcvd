"""Anthropic-backed document query planning from bounded database evidence."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from agent.schemas import (
    DocumentQueryDataGap,
    DocumentQueryPlan,
    DocumentQueryPlanningRequest,
    DocumentSearchQuery,
)
from agent.synthesis.config import load_evidence_planner_config


MAX_DOCUMENT_QUERIES = 3
_PROMPT_PATH = Path(__file__).parent / "prompts" / "document_query_planner.md"


class AnthropicDocumentQueryPlanner:
    """Formulate document searches from a validated database planner table."""

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
            _PROMPT_PATH.read_text(encoding="utf-8")
            if system_prompt is None
            else system_prompt
        )

    def plan(self, request: DocumentQueryPlanningRequest) -> DocumentQueryPlan:
        """Return a validated, deduplicated document query plan."""
        validated_request = DocumentQueryPlanningRequest.model_validate(request)
        if not validated_request.planner_table.rows:
            return DocumentQueryPlan(
                queries=[],
                data_gaps=[
                    DocumentQueryDataGap(
                        description=(
                            "No database rows were returned to ground document searches."
                        ),
                        linked_evidence_ids=[],
                    )
                ],
            )

        if not self.model:
            raise ValueError("EVIDENCE_PLANNER_MODEL or ANTHROPIC_MODEL is required")

        client = self.client or _build_anthropic_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": validated_request.model_dump_json(),
                }
            ],
            output_config=_json_schema_output_config(DocumentQueryPlan),
        )
        plan = DocumentQueryPlan.model_validate(_read_json_response(response))
        _validate_linked_evidence_ids(plan, validated_request)
        return plan.model_copy(update={"queries": _deduplicate_queries(plan.queries)})


def _build_anthropic_client() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
    from anthropic import Anthropic

    return Anthropic()


def _validate_linked_evidence_ids(
    plan: DocumentQueryPlan,
    request: DocumentQueryPlanningRequest,
) -> None:
    known_ids = {row.evidence_id for row in request.planner_table.rows}
    referenced_ids = {
        evidence_id
        for item in [*plan.queries, *plan.data_gaps]
        for evidence_id in item.linked_evidence_ids
    }
    unknown_ids = sorted(referenced_ids - known_ids)
    if unknown_ids:
        raise ValueError(
            "Document query planner referenced unknown evidence IDs: "
            + ", ".join(unknown_ids)
        )


def _deduplicate_queries(queries: list[DocumentSearchQuery]) -> list[DocumentSearchQuery]:
    prioritized = sorted(
        enumerate(queries),
        key=lambda item: (item[1].priority, item[0]),
    )
    seen: set[tuple[str, ...]] = set()
    deduplicated: list[DocumentSearchQuery] = []
    for _, query in prioritized:
        fingerprint = _query_fingerprint(query.query)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduplicated.append(query)
    return deduplicated[:MAX_DOCUMENT_QUERIES]


def _query_fingerprint(query: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    return tuple(sorted(set(re.findall(r"[^\W_]+", normalized))))


def _json_schema_output_config(contract: Any) -> dict[str, Any]:
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
    text = "".join(
        _read(block, "text", "") for block in getattr(response, "content", [])
    ).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        message = (
            "Anthropic document query planner returned invalid JSON "
            f"at character {exc.pos}"
        )
        if _read(response, "stop_reason") == "max_tokens":
            message += "; response reached max_tokens before completing JSON"
        raise ValueError(message) from exc


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

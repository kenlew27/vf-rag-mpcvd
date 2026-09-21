"""Shared utilities for synthesis components."""

import json
from typing import Any

def json_schema_output_config(contract: Any) -> dict[str, Any] | None:
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


def read_json_response(response: Any, component: str = "synthesizer") -> dict[str, Any]:
    text = "".join(read(block, "text", "") for block in getattr(response, "content", [])).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"Anthropic {component} returned invalid JSON at character {exc.pos}"
        if read(response, "stop_reason") == "max_tokens":
            message += "; response reached max_tokens before completing JSON"
        raise ValueError(message) from exc


def read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

"""Single semantic decision boundary for database questions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tools.bigquery.diamond_search import (
    ColumnInfo,
    DatabaseQueryError,
    DatabaseQuerySpec,
    UnsupportedDatabaseQuery,
    field_filter_capability,
    parse_query_spec,
)


_PROMPT_PATH = Path(__file__).parent / "prompts" / "retrieve_bigquery.md"
_TERM_MAP_PATH = Path(__file__).parent / "guidance" / "database" / "database_terms.json"


class DatabasePlanningError(RuntimeError):
    """The one allowed semantic planning call failed."""


@dataclass(frozen=True)
class PlannerCallResult:
    payload: dict[str, Any]
    model: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class PlannerRepairContext:
    """Validation feedback supplied to one replacement planning call."""

    rejected_payload: Mapping[str, Any]
    validator_error: str


class _StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolvedOutput(_StrictPlanModel):
    requested: str = Field(min_length=1)
    columns: list[str]

    @model_validator(mode="after")
    def _dedupe(self) -> "ResolvedOutput":
        if not self.requested.strip():
            raise ValueError("resolved output requested must not be blank")
        if any(not column.strip() for column in self.columns):
            raise ValueError("resolved output columns must not contain blanks")
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("resolved output columns must not contain duplicates")
        self.requested = self.requested.strip()
        self.columns = [column.strip() for column in self.columns]
        return self


class OutputResolution(_StrictPlanModel):
    requested: list[str] = Field(min_length=1)
    resolved: list[ResolvedOutput]
    unavailable: list[str]
    partial: bool

    @model_validator(mode="after")
    def _consistent(self) -> "OutputResolution":
        if any(not value.strip() for value in self.requested):
            raise ValueError("requested outputs must not contain blanks")
        if any(not value.strip() for value in self.unavailable):
            raise ValueError("unavailable outputs must not contain blanks")
        requested = [value.strip() for value in self.requested]
        unavailable = [value.strip() for value in self.unavailable]
        if len(requested) != len(set(requested)) or len(unavailable) != len(set(unavailable)):
            raise ValueError("output resolution entries must not contain duplicates")
        resolved_names = [item.requested.strip() for item in self.resolved]
        if len(resolved_names) != len(set(resolved_names)):
            raise ValueError("resolved output requests must not contain duplicates")
        if set(resolved_names) & set(unavailable):
            raise ValueError("an output cannot be both resolved and unavailable")
        if set(resolved_names) | set(unavailable) != set(requested):
            raise ValueError("every requested output must be resolved or unavailable")
        if self.partial != bool(self.resolved and unavailable):
            raise ValueError("partial must match resolved and unavailable outputs")
        self.requested = requested
        self.unavailable = unavailable
        return self


class _DatabasePlanPayload(_StrictPlanModel):
    query_spec: dict[str, Any]
    output_resolution: OutputResolution


@dataclass(frozen=True)
class DatabasePlanEnvelope:
    query_spec: DatabaseQuerySpec
    output_resolution: OutputResolution


def database_plan_output_schema(query_spec_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The planner envelope keeps field coverage outside the SQL contract."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query_spec": dict(query_spec_schema),
            "output_resolution": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "requested": {"type": "array", "items": {"type": "string"}},
                    "resolved": {
                        "type": "array",
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "requested": {"type": "string"},
                                "columns": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["requested", "columns"],
                        },
                    },
                    "unavailable": {"type": "array", "items": {"type": "string"}},
                    "partial": {"type": "boolean"},
                },
                "required": ["requested", "resolved", "unavailable", "partial"],
            },
        },
        "required": ["query_spec", "output_resolution"],
    }


def parse_database_plan(payload: Mapping[str, Any], columns: Sequence[ColumnInfo]) -> DatabasePlanEnvelope:
    """Validate coverage metadata and the unchanged executable QuerySpec.

    Bare QuerySpecs are accepted only for compatibility with stored fixtures and
    test clients; live planner calls are constrained to the envelope schema.
    """
    if "query_spec" not in payload:
        spec = parse_query_spec(payload, columns)
        selected = list(dict.fromkeys([
            *spec.select,
            *spec.group_by,
            *(field for calculation in spec.calculations for field in calculation.fields),
        ]))
        if not selected and any(item.function == "count" for item in spec.calculations):
            selected = ["record count"]
            resolved = [ResolvedOutput(requested="record count", columns=[])]
        else:
            resolved = [ResolvedOutput(requested=value, columns=[value]) for value in selected]
        return DatabasePlanEnvelope(
            query_spec=spec,
            output_resolution=OutputResolution(
                requested=selected,
                resolved=resolved,
                unavailable=[],
                partial=False,
            ),
        )
    try:
        payload_model = _DatabasePlanPayload.model_validate(payload)
    except ValidationError as exc:
        raise DatabaseQueryError(f"The database output resolution was invalid: {exc}") from exc
    spec = parse_query_spec(payload_model.query_spec, columns)
    resolution = payload_model.output_resolution
    available = {column.name for column in columns}
    unknown = sorted({column for item in resolution.resolved for column in item.columns} - available)
    if unknown:
        raise DatabaseQueryError("Unknown runtime database column in output resolution: " + ", ".join(unknown))
    if not resolution.resolved:
        raise UnsupportedDatabaseQuery("None of the requested database output fields are available.")
    executable_columns = _executable_output_columns(spec)
    claimed_columns = {column for item in resolution.resolved for column in item.columns}
    unprojected = sorted(claimed_columns - executable_columns)
    if unprojected:
        raise DatabaseQueryError("Resolved output columns are not represented by the executable query: " + ", ".join(unprojected))
    if any(not item.columns for item in resolution.resolved):
        if spec.operation != "aggregate" or not any(item.function == "count" for item in spec.calculations):
            raise DatabaseQueryError("Only count aggregates may resolve an output without physical columns.")
    return DatabasePlanEnvelope(query_spec=spec, output_resolution=resolution)


def _executable_output_columns(spec: DatabaseQuerySpec) -> set[str]:
    if spec.operation == "rows":
        return set(spec.select)
    if spec.operation == "cohort_rows":
        return set(spec.select) | {
            item.field for cohort in spec.cohorts for item in cohort.filters if item.field is not None
        }
    if spec.operation == "aggregate":
        return set(spec.group_by) | {field for calculation in spec.calculations for field in calculation.fields}
    return set()


def load_planner_prompt(path: Path = _PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_schema_catalog(columns: Sequence[Any]) -> str:
    """Return deterministic names/types/descriptions without stored values."""
    records = []
    for column in columns:
        name = _read(column, "name")
        field_type = _read(column, "field_type", _read(column, "bq_type", "UNKNOWN"))
        description = _read(column, "description")
        runtime_column = ColumnInfo(
            name=str(name), field_type=str(field_type), description=str(description).strip() if description else None
        )
        capability = field_filter_capability(runtime_column)
        records.append({
            "name": runtime_column.name,
            "type": runtime_column.field_type,
            "description": runtime_column.description,
            "value_kind": capability.value_kind,
            "allowed_operators": list(capability.allowed_operators),
            "supports_cohort_boundary": capability.supports_cohort_boundary,
        })
    records.sort(key=lambda item: item["name"])
    return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def plan_database_query(
    *,
    client: Any,
    model: str,
    database_question: str,
    output_schema: Mapping[str, Any],
    schema_catalog: str,
    identifier_candidates: Sequence[Mapping[str, Any]] = (),
    repair_context: PlannerRepairContext | None = None,
    max_tokens: int = 2048,
) -> PlannerCallResult:
    """Make exactly one constrained call using only the supervisor handoff."""
    question = " ".join(database_question.split())
    if not question:
        raise DatabasePlanningError("The database question is empty.")
    system = (
        load_planner_prompt()
        + "\n\nRuntime schema (authoritative; exact names only):\n"
        + schema_catalog
    )
    if identifier_candidates:
        system += (
            "\n\nResolved identifier candidates (authoritative, type-correct predicate groups):\n"
            + json.dumps(identifier_candidates, ensure_ascii=False, separators=(",", ":"), default=str)
        )
    content = question
    if repair_context is not None:
        content = (
            "Database question:\n"
            f"{question}\n\n"
            "The previous database plan failed validation. Return a replacement database plan envelope only. "
            "Do not repeat the invalid shape; use unsupported when the request cannot be represented.\n"
            "Rejected QuerySpec / database plan:\n"
            f"{json.dumps(dict(repair_context.rejected_payload), ensure_ascii=False, default=str)}\n\n"
            "Validator error:\n"
            f"{repair_context.validator_error}"
        )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": dict(output_schema),
                }
            },
        )
    except Exception as exc:
        raise DatabasePlanningError("The database planner provider call failed.") from exc

    text = "".join(
        str(_read(block, "text", ""))
        for block in (_read(response, "content", []) or [])
    ).strip()
    if not text:
        raise DatabasePlanningError("The database planner returned no structured output.")
    try:
        payload = json.loads(text, parse_float=Decimal)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DatabasePlanningError("The database planner returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise DatabasePlanningError("The database planner output must be an object.")

    usage = _read(response, "usage")
    return PlannerCallResult(
        payload=payload,
        model=model,
        stop_reason=_optional_str(_read(response, "stop_reason")),
        input_tokens=_optional_int(_read(usage, "input_tokens")),
        output_tokens=_optional_int(_read(usage, "output_tokens")),
    )


@lru_cache(maxsize=1)
def load_database_term_map() -> Mapping[str, tuple[str, ...]]:
    payload = json.loads(_TERM_MAP_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("database term map must be an object")
    normalized: dict[str, tuple[str, ...]] = {}
    for term, columns in payload.items():
        if not isinstance(term, str) or not isinstance(columns, list):
            raise ValueError("database term map entries must map strings to lists")
        normalized[term] = tuple(str(column) for column in columns)
    return MappingProxyType(normalized)


def context_columns_for_original_question(
    original_question: str,
    runtime_columns: Sequence[Any],
    term_map: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Select optional row context by exact terms; never return query logic."""
    available = {str(_read(column, "name", column)) for column in runtime_columns}
    mappings = term_map or load_database_term_map()
    normalized_question = original_question.casefold()
    candidates: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    position = 0
    combined: dict[str, Sequence[str]] = dict(mappings)
    for column in sorted(available):
        combined.setdefault(column, (column,))
    for term, columns in sorted(combined.items(), key=lambda item: (-len(item[0]), item[0].casefold())):
        valid_columns = [column for column in columns if column in available]
        if not valid_columns:
            continue
        for match in re.finditer(
            rf"(?<![\w]){re.escape(term.casefold())}(?![\w])",
            normalized_question,
        ):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.append(span)
            for column in valid_columns:
                candidates.append((match.start(), position, column))
                position += 1
    candidates.sort()
    return tuple(dict.fromkeys(column for _, _, column in candidates))


def planning_log_metadata(result: PlannerCallResult) -> dict[str, Any]:
    """Return provider metadata without questions, values, or rejected payloads."""
    return {
        "model": result.model,
        "stop_reason": result.stop_reason,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

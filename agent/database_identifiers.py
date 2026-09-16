"""Declarative identifier recognition for database planning."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Mapping, Sequence

from tools.bigquery.diamond_search import ColumnInfo, field_filter_capability


_FORMATS_PATH = Path(__file__).parent / "guidance" / "database" / "database_identifier_formats.json"


class IdentifierRegistryError(ValueError):
    """Identifier registry data is invalid."""


@dataclass(frozen=True)
class IdentifierPredicate:
    field: str
    capture: str | None = None
    template: str | None = None


@dataclass(frozen=True)
class IdentifierFormat:
    name: str
    pattern: re.Pattern[str]
    predicates: tuple[IdentifierPredicate, ...]


def load_identifier_formats(path: Path = _FORMATS_PATH) -> tuple[IdentifierFormat, ...]:
    """Load strict, data-only identifier transformation rules."""
    return _load_identifier_formats(path.resolve())


@lru_cache(maxsize=None)
def _load_identifier_formats(path: Path) -> tuple[IdentifierFormat, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentifierRegistryError(f"Could not load identifier registry: {path.name}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("formats"), list):
        raise IdentifierRegistryError("identifier registry must contain a formats list")
    formats: list[IdentifierFormat] = []
    names: set[str] = set()
    for entry in payload["formats"]:
        if not isinstance(entry, Mapping):
            raise IdentifierRegistryError("identifier format entries must be objects")
        name = entry.get("name")
        pattern_text = entry.get("pattern")
        predicates_payload = entry.get("predicates")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise IdentifierRegistryError("identifier format names must be unique and non-empty")
        if not isinstance(pattern_text, str) or not pattern_text:
            raise IdentifierRegistryError(f"identifier format {name!r} needs a pattern")
        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            raise IdentifierRegistryError(f"identifier format {name!r} has an invalid pattern") from exc
        if not isinstance(predicates_payload, list) or not predicates_payload:
            raise IdentifierRegistryError(f"identifier format {name!r} needs predicates")
        predicates: list[IdentifierPredicate] = []
        for predicate in predicates_payload:
            if not isinstance(predicate, Mapping) or not isinstance(predicate.get("field"), str) or not predicate["field"].strip():
                raise IdentifierRegistryError(f"identifier format {name!r} has an invalid target field")
            capture = predicate.get("capture")
            template = predicate.get("template")
            if (capture is None) == (template is None) or (capture is not None and not isinstance(capture, str)) or (template is not None and not isinstance(template, str)):
                raise IdentifierRegistryError(f"identifier format {name!r} predicates need one capture or template")
            references = {capture} if capture is not None else _template_fields(template)
            if not references or not references <= set(pattern.groupindex):
                raise IdentifierRegistryError(f"identifier format {name!r} references an unknown capture")
            predicates.append(IdentifierPredicate(field=predicate["field"], capture=capture, template=template))
        names.add(name)
        formats.append(IdentifierFormat(name=name, pattern=pattern, predicates=tuple(predicates)))
    return tuple(formats)


def resolve_identifier_candidates(
    question: str,
    columns: Sequence[ColumnInfo],
    formats: Sequence[IdentifierFormat] | None = None,
) -> list[dict[str, Any]]:
    """Return type-correct predicate groups without flattening correlations."""
    by_name = {column.name: column for column in columns}
    candidates: list[dict[str, Any]] = []
    for identifier_format in formats if formats is not None else load_identifier_formats():
        if any(predicate.field not in by_name for predicate in identifier_format.predicates):
            continue
        for match in identifier_format.pattern.finditer(question):
            predicates: list[dict[str, Any]] = []
            for predicate in identifier_format.predicates:
                raw = match.group(predicate.capture) if predicate.capture else predicate.template.format(**match.groupdict())
                value = _coerce_identifier_value(raw, by_name[predicate.field])
                if value is _UNAVAILABLE:
                    predicates = []
                    break
                predicates.append({"field": predicate.field, "operator": "eq", "value": value})
            if predicates:
                candidates.append({
                    "format": identifier_format.name,
                    "literal": match.group(0),
                    "predicates": predicates,
                })
    return candidates


def recognized_identifier_literals(text: str) -> tuple[str, ...]:
    """Return registry-recognized literals for general handoff preservation."""
    values: list[str] = []
    for identifier_format in load_identifier_formats():
        values.extend(match.group(0) for match in identifier_format.pattern.finditer(text))
    return tuple(dict.fromkeys(values))


_UNAVAILABLE = object()


def _coerce_identifier_value(raw: str, column: ColumnInfo) -> Any:
    capability = field_filter_capability(column)
    if capability.value_kind in {"string", "exact"}:
        return raw
    if capability.value_kind == "boolean":
        values = {"true": True, "false": False}
        return values.get(raw.casefold(), _UNAVAILABLE)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return _UNAVAILABLE
    if not value.is_finite():
        return _UNAVAILABLE
    return int(value) if value == value.to_integral_value() else float(value)


def _template_fields(template: str) -> set[str]:
    try:
        return {field for _, field, _, _ in Formatter().parse(template) if field}
    except ValueError as exc:
        raise IdentifierRegistryError("identifier template is invalid") from exc

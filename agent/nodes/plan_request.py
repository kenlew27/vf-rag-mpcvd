"""Request-planning node."""

import json
import os
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.request_plan import (
    KnowledgeSource,
    RequestPlan,
    RequestStatus,
    database_reference_question,
    database_only_question,
    document_scopes_for_sources,
    request_plan_output_schema,
)
from agent.database_identifiers import recognized_identifier_literals
from agent.state import AgentState

_PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "plan_request.md"
_DATABASE_TERMS_PATH = Path(__file__).parents[1] / "guidance" / "database" / "database_terms.json"
_REQUEST_PLANNER_TERMS_PATH = (
    Path(__file__).parents[1] / "guidance" / "database" / "request_planner_terms.json"
)

_POST_RETRIEVAL_CLAUSE_RE = re.compile(
    r"(?:^|[,;]\s*|\band\s+)(?:explicitly\s+|please\s+)?"
    r"(?:note|noting|flag|highlight|organize|interpret|explain|assess|recommend|suggest|"
    r"separate|distinguish|preserv(?:e|ing)\s+null|"
    r"identify\s+(?:mechanisms?|causes?)|draw\s+conclusions?|discuss)\b[^.;!?]*"
    r"(?=[.;!?]|$)",
    re.IGNORECASE,
)
_GENERIC_HANDOFF_RE = re.compile(
    r"\b(?:relevant\s+(?:database\s+)?(?:evidence|records|data)|"
    r"all\s+(?:details|attributes))\b",
    re.IGNORECASE,
)
_BROAD_FIELD_BUNDLE_RE = re.compile(
    r"\b(?:process conditions?|telemetry|characterization results?|"
    r"final[- ]quality(?:\s+or\s+characterization)?\s+(?:fields?|results?)|"
    r"final characterization|quality metrics?|outcome (?:data|fields?|metrics?))\b",
    re.IGNORECASE,
)
_ENTITY_REFERENCE_RE = re.compile(
    r"\b(?P<noun>sample(?:\s+ids?)?s?|reactors?|holders?|projects?|recipes?|"
    r"process(?:es)?(?:\s+with)?(?:\s+process)?\s+identifiers?)\s+"
    r"(?P<identifiers>(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]*"
    r"(?:\s*(?:,|and|or)\s*(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]*)*)",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]*")
_STEP_LIST_RE = re.compile(
    r"\bsteps?\s+\d+(?:\s*(?:,|and|or)\s*\d+)+",
    re.IGNORECASE,
)
_STEP_RE = re.compile(r"\bsteps?[\s_-]*(\d+)(?!\d)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![\w.-])(?:\d+(?:\.\d+)?|\.\d+)(?![\w-]|\.\d)")


def database_vocabulary_context() -> str:
    """Return the compact alias and field-bundle catalog safe for request planning.

    The live database schema remains the downstream planner's authority.
    """

    return json.dumps(
        {"aliases": [
            {"phrase": phrase, "columns": list(columns)}
            for phrase, columns in _database_term_map().items()
        ]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@lru_cache(maxsize=1)
def _database_term_map() -> dict[str, tuple[str, ...]]:
    terms: dict[str, tuple[str, ...]] = {}
    for path in (_DATABASE_TERMS_PATH, _REQUEST_PLANNER_TERMS_PATH):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path.name} must contain an object")
        for phrase, columns in payload.items():
            if not isinstance(columns, list):
                raise ValueError(f"{path.name} entries must map phrases to column lists")
            terms[str(phrase)] = tuple(str(column) for column in columns)
    return dict(sorted(terms.items(), key=lambda item: item[0].casefold()))


def plan_request(state: AgentState, client: Any = None, model: str | None = None) -> AgentState:
    """Produce the validated task, source, and status plan for a request."""

    model = model or os.getenv("ANTHROPIC_MODEL")
    if not model:
        raise ValueError("ANTHROPIC_MODEL is required or pass model=...")

    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
        from anthropic import Anthropic

        client = Anthropic()

    request_plan = _parse_result(_planner_response(client, model, state.query.raw_text))
    request_plan, issues = _validated_database_handoff(state.query.raw_text, request_plan)
    if issues:
        corrected = _parse_result(_planner_response(
            client,
            model,
            state.query.raw_text,
            issues=issues,
            prior_question=request_plan.structured_data_question,
        ))
        # The retry corrects only the database handoff; routing remains the first
        # response's validated classification decision.
        request_plan = request_plan.model_copy(update={
            "structured_data_question": corrected.structured_data_question,
        })
        request_plan, issues = _validated_database_handoff(state.query.raw_text, request_plan)
    if issues:
        request_plan = RequestPlan(
            status=RequestStatus.NEEDS_CLARIFICATION,
            reasons=[_clarification_reason(issues)],
        )
    return state.model_copy(
        update={
            "request_plan": request_plan,
            "document_scopes": document_scopes_for_sources(request_plan.knowledge_sources),
        }
    )


def _planner_response(
    client: Any,
    model: str,
    question: str,
    *,
    issues: list[str] | None = None,
    prior_question: str | None = None,
) -> Any:
    system = (
        _PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\nCompact database vocabulary (authoritative aliases and field bundles; not a full schema):\n"
        + database_vocabulary_context()
    )
    content = question
    if issues:
        content = (
            "Original request:\n"
            f"{question}\n\n"
            "Your previous structured_data_question was rejected: "
            f"{'; '.join(issues)}\n"
            f"Previous structured_data_question: {prior_question or 'null'}\n\n"
            "Return the complete JSON plan again. Keep task, source, and status decisions; "
            "replace only the structured_data_question with one concise, executable database-only sentence."
        )
    return client.messages.create(
        model=model,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": request_plan_output_schema(),
            }
        },
    )


def _parse_result(response: Any) -> RequestPlan:
    text = "".join(_read(block, "text", "") for block in getattr(response, "content", []))
    return RequestPlan.model_validate(json.loads(text))


def _validated_database_handoff(question: str, plan: RequestPlan) -> tuple[RequestPlan, list[str]]:
    if KnowledgeSource.STRUCTURED_DATA not in plan.knowledge_sources:
        return plan, []
    if len(plan.knowledge_sources) > 1 and plan.structured_data_question is None:
        return plan, ["structured_data_question is required for a compound database request"]
    if plan.structured_data_question is None:
        return plan, []
    handoff = _strip_post_retrieval_instructions(
        database_only_question(question, plan.structured_data_question)
    )
    normalized = plan.model_copy(update={"structured_data_question": handoff})
    if not handoff:
        return normalized, ["database handoff contains no executable database clause"]
    return normalized, _database_handoff_issues(database_reference_question(question), handoff)


def _clarification_reason(issues: list[str]) -> str:
    """Keep the final controlled clarification within the RequestPlan contract."""
    prefix = "Database handoff could not be made executable: "
    detail = "; ".join(issues)
    return (prefix + detail)[:300].rstrip(" ;,")


def _strip_post_retrieval_instructions(handoff: str) -> str:
    cleaned = _POST_RETRIEVAL_CLAUSE_RE.sub("", handoff)
    return " ".join(cleaned.strip(" ,;.").split()) + ("." if cleaned.strip(" ,;.") else "")


def _database_handoff_issues(question: str, handoff: str | None) -> list[str]:
    """Check preservation and basic executability without pretending to own schema validation."""
    if handoff is None:
        return ["structured_data_question is required for this database detail"] if _has_database_detail(question) else []

    issues: list[str] = []
    if _GENERIC_HANDOFF_RE.search(handoff) and _has_database_detail(question):
        issues.append("database handoff discarded requested detail")
    has_broad_bundle = _BROAD_FIELD_BUNDLE_RE.search(handoff) is not None
    if has_broad_bundle:
        issues.append("database field bundle must be expanded to its listed physical columns")
    if _POST_RETRIEVAL_CLAUSE_RE.search(handoff):
        issues.append("database handoff contains post-retrieval instructions")
    if not question.strip():
        return list(dict.fromkeys(issues))

    for entity, identifiers in _explicit_entities(question):
        if entity != "sample" and not _entity_noun_present(entity, handoff):
            issues.append(f"explicit entity noun {entity!r} was not preserved")
        for identifier in identifiers:
            if identifier.casefold() not in handoff.casefold():
                issues.append(f"identifier {identifier!r} was not preserved")
    for identifier in recognized_identifier_literals(question):
        if identifier.casefold() not in handoff.casefold():
            issues.append(f"identifier {identifier!r} was not preserved")
    reference_identifiers = {
        identifier.casefold()
        for _, identifiers in _explicit_entities(question)
        for identifier in identifiers
    } | {identifier.casefold() for identifier in recognized_identifier_literals(question)}
    handoff_identifiers = {
        identifier.casefold()
        for _, identifiers in _explicit_entities(handoff)
        for identifier in identifiers
    } | {identifier.casefold() for identifier in recognized_identifier_literals(handoff)}
    for identifier in sorted(handoff_identifiers - reference_identifiers):
        issues.append(f"identifier {identifier!r} was introduced without database reference")

    if not has_broad_bundle:
        requested_columns = _matched_columns(_metric_reference_text(question))
        returned_columns = _matched_columns(handoff)
        for column in dict.fromkeys(requested_columns):
            if column not in returned_columns:
                issues.append(f"requested metric {column!r} was not preserved")

    requested_numbers = _numeric_values(question)
    returned_numbers = _numeric_values(handoff)
    for number in requested_numbers:
        if number not in returned_numbers:
            issues.append(f"numeric value {number} was not preserved")
    for number in returned_numbers:
        if number not in requested_numbers:
            issues.append(f"numeric value {number} was introduced without database reference")
    for step in _step_numbers(question):
        if step not in _step_numbers(handoff):
            issues.append(f"process step {step} was not preserved")
    requested_operators = _comparison_operators(question)
    returned_operators = _comparison_operators(handoff)
    for operator in requested_operators:
        if operator not in returned_operators:
            issues.append(f"comparison operator {operator!r} was not preserved")
    for operator in returned_operators:
        if operator not in requested_operators:
            issues.append(f"comparison operator {operator!r} was introduced without database reference")

    requested_aggregates = _aggregate_operations(question)
    returned_aggregates = _aggregate_operations(handoff)
    for aggregate in requested_aggregates:
        if aggregate not in returned_aggregates:
            issues.append(f"aggregate {aggregate!r} was not preserved")
    if re.search(r"\b(?:compute|calculate|summari[sz]e|count)\b", handoff, re.IGNORECASE):
        for aggregate in returned_aggregates:
            if aggregate not in requested_aggregates:
                issues.append(f"aggregate {aggregate!r} was introduced without database reference")
    if _requires_grouping(question) and not _requires_grouping(handoff):
        issues.append("grouping was not preserved")
    if _requires_ordering(question) and not _requires_ordering(handoff):
        issues.append("ordering or layout was not preserved")
    if _requires_grouping(handoff) and not _requires_grouping(question):
        issues.append("grouping was introduced without database reference")
    if _requires_ordering(handoff) and not _requires_ordering(question):
        issues.append("ordering or layout was introduced without database reference")
    if _requires_grouping(handoff) and not _aggregate_operations(handoff):
        issues.append("grouping requires an aggregate database operation")
    return list(dict.fromkeys(issues))


def _has_database_detail(question: str) -> bool:
    return bool(
        _explicit_entities(question)
        or recognized_identifier_literals(question)
        or _matched_columns(question)
        or _numeric_values(question)
        or _aggregate_operations(question)
        or _requires_grouping(question)
        or _requires_ordering(question)
    )


def _explicit_entities(question: str) -> list[tuple[str, tuple[str, ...]]]:
    entities: list[tuple[str, tuple[str, ...]]] = []
    for match in _ENTITY_REFERENCE_RE.finditer(question):
        noun = match.group("noun").casefold()
        entity = next(
            name for name in ("sample", "reactor", "holder", "project", "recipe", "process")
            if noun.startswith(name)
        )
        entities.append((entity, tuple(_IDENTIFIER_RE.findall(match.group("identifiers")))))
    return entities


def _entity_noun_present(entity: str, handoff: str) -> bool:
    patterns = {
        "reactor": r"\breactors?\b",
        "holder": r"\bholders?\b|\bholder[_ ]?id\b",
        "project": r"\bprojects?\b",
        "recipe": r"\brecipes?\b",
        "process": r"\bprocess(?:es)?\b",
    }
    return re.search(patterns[entity], handoff, re.IGNORECASE) is not None


def _metric_reference_text(question: str) -> str:
    """Exclude optional cohort context that the database plan does not project explicitly."""
    if _step_numbers(question):
        return ""
    normalized = re.sub(
        r"\b(?:using|including|with)\s+[^.;!?]*?\s+context\b",
        "",
        question,
        flags=re.IGNORECASE,
    )
    if len(_comparison_operators(question)) >= 2 and re.search(
        r"\b(?:compare|contrast|cohorts?|groups?)\b",
        question,
        re.IGNORECASE,
    ):
        normalized = re.sub(r",?\s*including\b[^.;!?]*", "", normalized, flags=re.IGNORECASE)
    return normalized


def _matched_columns(text: str) -> list[str]:
    terms: dict[str, list[str]] = {}
    for phrase, columns in _database_term_map().items():
        for column in columns:
            terms.setdefault(phrase, []).append(column)
            terms.setdefault(column, []).append(column)
    occupied: list[tuple[int, int]] = []
    matches: list[tuple[int, int, str]] = []
    for term, columns in sorted(terms.items(), key=lambda item: (-len(item[0]), item[0].casefold())):
        for match in re.finditer(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.IGNORECASE):
            start, end = match.span()
            if any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied):
                continue
            occupied.append((start, end))
            matches.extend(
                (start, position, column)
                for position, column in enumerate(dict.fromkeys(columns))
            )
    return [column for _, _, column in sorted(matches)]


def _repeated_output_columns(handoff: str) -> list[str]:
    sections = re.split(
        r"\b(?:return(?:ing)?|report(?:ing)?|using|compute|calculate)\b",
        handoff,
        flags=re.IGNORECASE,
    )
    if len(sections) == 1:
        return []
    columns = _matched_columns(sections[-1])
    return [column for column in dict.fromkeys(columns) if columns.count(column) > 1]


def _numeric_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    without_steps = _STEP_RE.sub("", _STEP_LIST_RE.sub("", text))
    for literal in _NUMBER_RE.findall(without_steps):
        try:
            values.append(Decimal(literal).normalize())
        except InvalidOperation:
            continue
    return values


def _step_numbers(text: str) -> list[int]:
    values = [int(value) for value in _STEP_RE.findall(text)]
    for step_list in _STEP_LIST_RE.findall(text):
        values.extend(int(value) for value in re.findall(r"\d+", step_list))
    return list(dict.fromkeys(values))


def _comparison_operators(text: str) -> list[str]:
    normalized = text.casefold()
    replacements = (
        (r"\b(?:greater than or equal to|at least|at or above|no less than)\b", ">="),
        (r"\b(?:less than or equal to|at most|at or below|no more than)\b", "<="),
        (r"\b(?:greater than|above|more than)\b", ">"),
        (r"\b(?:less than|below|fewer than)\b", "<"),
    )
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, f" {replacement} ", normalized)
    return re.findall(r">=|<=|(?<![<>=])>(?![=])|(?<![<>=])<(?![=])", normalized)


def _aggregate_operations(text: str) -> list[str]:
    patterns = {
        "avg": r"\baverage\b|\bmean\s+(?!chamber\b|pressure\b|microwave\b|power\b|reflected\b)",
        "count": r"\b(?:count|number of|row count|record count)\b",
        "sum": r"\b(?:sum|total)\b",
        "min": r"\b(?:minimum|min)\b",
        "max": r"\b(?:maximum|max)\b",
        "median": r"\bmedian\b",
        "stddev": r"\b(?:standard deviation|stddev)\b",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def _requires_grouping(text: str) -> bool:
    if re.search(
        r"\bgroup(?:ed)?\s+by\b|\bsummarize\b[^.;!?]{0,80}\bby\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if not _aggregate_operations(text):
        return False
    return re.search(
        r"\b(?:for|under)\s+each\b|\bper\s+(?:project|reactor|recipe|holder|gas|status|flag|category)\b",
        text,
        re.IGNORECASE,
    ) is not None


def _requires_ordering(text: str) -> bool:
    return re.search(
        r"\b(?:side[- ]by[- ]side|ordered?|order\s+by|sort(?:ed)?|ascending|descending|top\s+\d+|highest|lowest)\b",
        text,
        re.IGNORECASE,
    ) is not None


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

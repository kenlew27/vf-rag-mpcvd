"""Run a JSON evaluation batch through the database or full-workflow API.

Usage:
    python -m agent.database_batch harness/database_holdout.json database_results.csv \
        --base-url http://127.0.0.1:8001 --timeout 300 \
        --debug-trace --details-output database_results.jsonl

    python -m agent.database_batch harness/database_holdout.json workflow_results.csv \
        --base-url http://127.0.0.1:8001 --full-workflow \
        --debug-trace --details-output workflow_results.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import httpx


_EVAL_ID_COL = "Eval ID (Unique identifier for the evaluation item)"
_QUERY_COL = "Query (Natural-language database request)"
_SUPERVISOR_QUERY_COL = "Supervisor Query (Sanitized database-only request)"
_CAPABILITY_COL = "Capability"
_REFERENCE_SQL_COL = "SQL to run after replacing placeholders"
_GOLD_COL = "Gold SQL / Result (Expert-approved query logic or expected result set)"

_CSV_FIELDS = [
    "eval_id",
    "question",
    "supervisor_query",
    "capability",
    "reference_sql",
    "gold_sql_or_result",
    "sql",
    "query_type",
    "rows_returned",
    "database_rows",
    "debug_trace",
    "checks",
    "failure_pattern",
    "error",
    "diagnostic_id",
    "traceback",
    "stage_timings",
    "attempts",
]

Requester = Callable[[str, dict[str, Any], float], dict[str, Any]]
_RESULT_SIGNIFICANT_FIGURES = 4


def run_batch(
    input_path: Path,
    output_path: Path,
    *,
    base_url: str = "http://127.0.0.1:8001",
    timeout: float = 300,
    include_debug_trace: bool = True,
    details_output_path: Path | None = None,
    full_workflow: bool = False,
    requester: Requester | None = None,
) -> None:
    """Evaluate paired questions directly or through planning, retrieval, and synthesis."""
    questions = _load_questions(input_path)
    requester = requester or _request_database_agent
    endpoint_path = "agent/synthesize" if full_workflow else "agent/data-query"
    endpoint = f"{base_url.rstrip('/')}/{endpoint_path}"

    del include_debug_trace  # Debug traces are mandatory for harness runs.
    details_output_path = details_output_path or output_path.with_suffix(output_path.suffix + ".jsonl")
    details_file = details_output_path.open("w", encoding="utf-8")
    try:
        with output_path.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for item in questions:
                row, details = _run_question(
                    eval_id=item["eval_id"],
                    question=item["question"],
                    supervisor_query=item["supervisor_query"],
                    capability=item["capability"],
                    reference_sql=item["reference_sql"],
                    gold_sql_or_result=item["gold_sql_or_result"],
                    expected=item["expected"],
                    endpoint=endpoint,
                    timeout=timeout,
                    include_debug_trace=True,
                    full_workflow=full_workflow,
                    requester=requester,
                )
                writer.writerow(row)
                details_file.write(
                    json.dumps(_redact_value(details), ensure_ascii=False, default=str) + "\n"
                )
    finally:
        details_file.close()


def _load_questions(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open(encoding="utf-8") as input_file:
        payload = json.load(input_file)

    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array or an object with an items array")

    questions: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError("each input item must be an object")

        is_holdout_row = _QUERY_COL in item or _SUPERVISOR_QUERY_COL in item
        question = item.get(_QUERY_COL) if is_holdout_row else item.get("question")
        supervisor_query = (
            item.get(_SUPERVISOR_QUERY_COL)
            if is_holdout_row
            else item.get("supervisor_query", question)
        )
        if not isinstance(question, str) or not question.strip():
            raise ValueError("each input item must have a non-empty string original query")
        if not isinstance(supervisor_query, str) or not supervisor_query.strip():
            raise ValueError("each input item must have a non-empty string supervisor query")

        eval_id = item.get(_EVAL_ID_COL, item.get("eval_id", f"DB-{index:03d}"))
        if not isinstance(eval_id, str) or not eval_id.strip():
            raise ValueError("each input item must have a non-empty string evaluation ID")
        expected = item.get("expected", {})
        if not isinstance(expected, dict):
            raise ValueError("expected must be an object when provided")
        reference_sql = expected.get("reference_sql", item.get(_REFERENCE_SQL_COL, ""))
        result_rows = expected.get("result_rows")
        gold_sql_or_result = (
            json.dumps(result_rows, ensure_ascii=False, default=str)
            if result_rows is not None
            else str(item.get(_GOLD_COL, ""))
        )
        questions.append(
            {
                "eval_id": eval_id.strip(),
                "question": question.strip(),
                "supervisor_query": supervisor_query.strip(),
                "capability": str(item.get("capability", item.get(_CAPABILITY_COL, ""))),
                "reference_sql": str(reference_sql),
                "gold_sql_or_result": gold_sql_or_result,
                "expected": expected,
            }
        )
    return questions


def _run_question(
    *,
    eval_id: str,
    question: str,
    supervisor_query: str,
    capability: str,
    reference_sql: str,
    gold_sql_or_result: str,
    expected: dict[str, Any],
    endpoint: str,
    timeout: float,
    include_debug_trace: bool,
    full_workflow: bool,
    requester: Requester,
) -> tuple[dict[str, str | int], dict[str, Any]]:
    started = time.monotonic()
    diagnostic_id = str(uuid.uuid4())
    attempts = [{"attempt": 1}]
    result: dict[str, str | int] = {
        "eval_id": eval_id,
        "question": question,
        "supervisor_query": supervisor_query,
        "capability": capability,
        "reference_sql": reference_sql,
        "gold_sql_or_result": gold_sql_or_result,
        "sql": "",
        "query_type": "",
        "rows_returned": "",
        "database_rows": "[]",
        "debug_trace": "{}",
        "checks": "{}",
        "failure_pattern": "",
        "error": "",
        "diagnostic_id": diagnostic_id,
        "traceback": "",
        "stage_timings": "",
        "attempts": json.dumps(attempts),
    }
    actual: dict[str, Any] = {}
    debug_trace: dict[str, Any] = {}
    checks: dict[str, bool | None] = {}
    request_plan: dict[str, Any] = {}
    document_evidence: dict[str, Any] = {}
    final_answer: dict[str, Any] = {}
    raw_response: dict[str, Any] = {}
    stage_started = time.monotonic()
    stage_timings: list[dict[str, Any]] = []
    exception_record: dict[str, Any] | None = None
    try:
        payload: dict[str, Any] = {"question": question}
        if not full_workflow:
            payload["supervisor_query"] = supervisor_query
        payload["include_debug_trace"] = True
        raw_response = requester(
            endpoint,
            payload,
            timeout,
        )
        if not isinstance(raw_response, dict):
            raise ValueError("endpoint response must be a JSON object")
        stage_timings.append({
            "stage": "request",
            "status": "succeeded",
            "duration_ms": int((time.monotonic() - stage_started) * 1000),
        })
        if full_workflow:
            request_plan = _object(raw_response.get("request_plan"))
            document_evidence = _object(raw_response.get("document_evidence"))
            final_answer = _object(raw_response.get("final_answer"))
            response = _object(raw_response.get("database_evidence"))
        else:
            response = raw_response
        actual_supervisor_query = (
            str(request_plan.get("structured_data_question") or "")
            if full_workflow
            else supervisor_query
        )
        result["sql"] = str(response.get("sql") or "")
        result["query_type"] = str(response.get("query_type") or "")
        result["rows_returned"] = response.get("rows_returned") or 0
        result["database_rows"] = json.dumps(response.get("evidence_rows") or [])
        debug_value = response.get("debug_trace") or {}
        if not isinstance(debug_value, dict):
            raise ValueError("debug_trace must be a JSON object when provided")
        debug_trace = debug_value
        checks = _evaluate_response(
            expected=expected,
            supervisor_query=supervisor_query,
            actual_supervisor_query=actual_supervisor_query,
            response=response,
            debug_trace=debug_trace,
            request_plan=request_plan,
            document_evidence=document_evidence,
            final_answer=final_answer,
            full_workflow=full_workflow,
        )
        result["debug_trace"] = json.dumps(debug_trace, ensure_ascii=False, default=str)
        result["checks"] = json.dumps(checks, ensure_ascii=False)
        result["failure_pattern"] = _failure_pattern(response, checks, debug_trace)
        actual = {
            "sql": response.get("sql"),
            "query_type": response.get("query_type"),
            "rows_returned": response.get("rows_returned") or 0,
            "result_rows": _combined_result_rows(response),
            "request_plan": request_plan,
            "structured_data_question": actual_supervisor_query,
            "document_evidence": document_evidence,
            "final_answer": final_answer,
        }
    except Exception as exc:
        exception_traceback = _redact("".join(traceback.format_exception(exc)))
        print(exception_traceback, file=sys.stderr)
        result["error"] = _redact(f"{type(exc).__name__}: {exc}")
        result["traceback"] = exception_traceback
        result["failure_pattern"] = "transport_or_harness_error"
        stage_timings.append({
            "stage": "request",
            "status": "failed",
            "duration_ms": int((time.monotonic() - stage_started) * 1000),
            "error": result["error"],
        })
        exception_record = {
            "type": type(exc).__name__,
            "message": _redact(str(exc)),
            "traceback": exception_traceback,
        }
    result["stage_timings"] = json.dumps(stage_timings, ensure_ascii=False)
    details = {
        "diagnostic_id": diagnostic_id,
        "eval_id": eval_id,
        "request": {
            "evaluation_mode": "full_workflow" if full_workflow else "database_agent",
            "question": question,
            "supervisor_query": supervisor_query,
            "supervisor_query_reference": supervisor_query,
        },
        "capability": capability,
        "expected": expected,
        "actual": actual,
        "raw_response": raw_response,
        "debug_trace": debug_trace,
        "stage_timings": stage_timings,
        "attempts": attempts,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "checks": checks,
        "failure_pattern": result["failure_pattern"] or None,
        "error": result["error"] or None,
        "exception": exception_record,
    }
    return result, details


def _redact(text: str) -> str:
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|authorization|bearer|token|password)\b\s*[:=]\s*)[^\s,]+",
        r"\1[REDACTED]",
        text,
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _evaluate_response(
    *,
    expected: dict[str, Any],
    supervisor_query: str,
    actual_supervisor_query: str,
    response: dict[str, Any],
    debug_trace: dict[str, Any],
    request_plan: dict[str, Any],
    document_evidence: dict[str, Any],
    final_answer: dict[str, Any],
    full_workflow: bool,
) -> dict[str, bool | None]:
    if not expected:
        return {}
    checks: dict[str, bool | None] = {}
    if full_workflow:
        expected_plan = expected.get("request_plan") or {}
        checks["request_status"] = request_plan.get("status") == expected_plan.get(
            "status", "ready"
        )
        checks["request_tasks"] = _contains_required(
            request_plan.get("tasks"), expected_plan.get("required_tasks", [])
        )
        checks["knowledge_sources"] = _contains_required(
            request_plan.get("knowledge_sources"),
            expected_plan.get("required_knowledge_sources", []),
        )
        checks["supervisor_query_present"] = bool(actual_supervisor_query.strip())
        checks["supervisor_query_sanitized"] = _is_database_only_question(
            actual_supervisor_query
        )
        checks["supervisor_query_used"] = bool(actual_supervisor_query.strip()) and (
            debug_trace.get("database_question") == actual_supervisor_query
        )
        checks["document_evidence"] = _has_document_evidence(document_evidence)
        checks["final_answer"] = bool(final_answer)
    else:
        checks["supervisor_query_used"] = (
            debug_trace.get("database_question") == supervisor_query
        )
    expected_spec = expected.get("query_spec")
    if isinstance(expected_spec, dict):
        actual_spec = _actual_query_spec(debug_trace)
        for field in (
            "operation",
            "select",
            "filters",
            "group_by",
            "calculations",
            "order_by",
            "cohorts",
            "limit",
            "unsupported_reason",
        ):
            checks[field] = (
                _spec_field_equal(
                    field,
                    actual_spec.get(field),
                    expected_spec.get(field),
                )
                if actual_spec is not None
                else False
            )
    if "query_type" in expected:
        checks["query_type"] = response.get("query_type") == expected.get("query_type")
    if "context_columns" in expected:
        checks["context_columns"] = (
            response.get("context_columns", []) == expected.get("context_columns")
        )
    checks["dry_run_passed"] = response.get("dry_run_passed") is True
    expected_rows = expected.get("result_rows")
    checks["result_rows"] = (
        None
        if expected_rows is None
        else _result_rows_equal(_combined_result_rows(response), expected_rows)
    )
    return checks


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contains_required(actual: Any, required: Any) -> bool:
    if not isinstance(actual, list) or not isinstance(required, list):
        return False
    return set(required).issubset(set(actual))


def _is_database_only_question(value: str) -> bool:
    return bool(value.strip()) and re.search(
        r"\.pdf\b|\b(?:paper|papers|document|documents|literature)\b",
        value,
        flags=re.IGNORECASE,
    ) is None


def _has_document_evidence(value: dict[str, Any]) -> bool:
    return any(
        isinstance(packet, dict) and bool(packet.get("contexts"))
        for packet in value.values()
    )


def _spec_field_equal(field: str, actual: Any, expected: Any) -> bool:
    if field in {"select", "filters", "group_by", "calculations", "cohorts"}:
        if not isinstance(actual, list) or not isinstance(expected, list):
            return actual == expected
        return sorted(_canonical_json(item) for item in actual) == sorted(
            _canonical_json(item) for item in expected
        )
    return actual == expected


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _actual_query_spec(debug_trace: dict[str, Any]) -> dict[str, Any] | None:
    for stage_name, key in (
        ("value_resolution", "resolved_spec"),
        ("validation", "validated_spec"),
        ("planning", "planner_spec"),
    ):
        stage = _trace_stage(debug_trace, stage_name)
        value = stage.get(key) if stage else None
        if isinstance(value, dict):
            return value
    return None


def _trace_stage(debug_trace: dict[str, Any], name: str) -> dict[str, Any] | None:
    stages = debug_trace.get("stages", [])
    if not isinstance(stages, list):
        return None
    return next(
        (
            stage
            for stage in stages
            if isinstance(stage, dict) and stage.get("stage") == name
        ),
        None,
    )


def _combined_result_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_rows = response.get("evidence_rows") or []
    context_rows = response.get("context_rows") or []
    if not isinstance(evidence_rows, list) or not isinstance(context_rows, list):
        return []
    if not context_rows:
        return evidence_rows
    return [
        {**evidence, **context}
        for evidence, context in zip(evidence_rows, context_rows, strict=False)
        if isinstance(evidence, dict) and isinstance(context, dict)
    ]


def _result_rows_equal(actual: Any, expected: Any) -> bool:
    return _normalize_result_value(actual) == _normalize_result_value(expected)


def _normalize_result_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_result_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_result_value(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value == 0:
            return decimal_value
        exponent = decimal_value.adjusted() - _RESULT_SIGNIFICANT_FIGURES + 1
        quantum = Decimal("1").scaleb(exponent)
        return decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
    return value


def _failure_pattern(
    response: dict[str, Any],
    checks: dict[str, bool | None],
    debug_trace: dict[str, Any],
) -> str:
    stages = debug_trace.get("stages", [])
    if isinstance(stages, list):
        failed = next(
            (
                stage.get("stage")
                for stage in stages
                if isinstance(stage, dict) and stage.get("status") == "failed"
            ),
            None,
        )
        if failed:
            return f"stage:{failed}"
    if response.get("execution_failed"):
        return f"stage:{response.get('failure_stage') or 'unknown'}"
    priorities = (
        "request_status",
        "request_tasks",
        "knowledge_sources",
        "supervisor_query_present",
        "supervisor_query_sanitized",
        "supervisor_query_used",
        "operation",
        "filters",
        "select",
        "group_by",
        "calculations",
        "order_by",
        "cohorts",
        "limit",
        "query_type",
        "context_columns",
        "dry_run_passed",
        "result_rows",
        "document_evidence",
        "final_answer",
    )
    mismatch = next((name for name in priorities if checks.get(name) is False), None)
    return f"mismatch:{mismatch}" if mismatch else ""


def _request_database_agent(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    response = httpx.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("endpoint response must be a JSON object")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run paired database holdouts directly or through the full workflow"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="path to a holdout items object, six-column array, or legacy question array",
    )
    parser.add_argument("output", type=Path, help="path to write the CSV results")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="agent API base URL")
    parser.add_argument("--timeout", type=float, default=300, help="per-request timeout in seconds")
    parser.add_argument(
        "--debug-trace",
        action="store_true",
        help="request the opt-in structured database planning and execution trace",
    )
    parser.add_argument(
        "--details-output",
        type=Path,
        help="optional path for one detailed JSON object per evaluation item",
    )
    parser.add_argument(
        "--full-workflow",
        action="store_true",
        help="send only the user question to /agent/synthesize and score the generated database plan",
    )
    args = parser.parse_args()
    run_batch(
        args.input,
        args.output,
        base_url=args.base_url,
        timeout=args.timeout,
        include_debug_trace=True,
        details_output_path=args.details_output,
        full_workflow=args.full_workflow,
    )


if __name__ == "__main__":
    main()

"""Run the database agent's single-plan deterministic query pipeline."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from google.cloud import bigquery

from agent.database_query_planner import (
    DatabasePlanningError,
    PlannerRepairContext,
    build_schema_catalog,
    context_columns_for_original_question,
    database_plan_output_schema,
    parse_database_plan,
    plan_database_query,
    planning_log_metadata,
)
from agent.database_identifiers import resolve_identifier_candidates
from agent.state import AgentState
from tools.bigquery.diamond_search import (
    DatabaseQueryError,
    QueryClarificationRequired,
    QueryExecutionError,
    UnsupportedDatabaseQuery,
    build_database_packet,
    compile_query,
    dry_run_query,
    execute_query,
    load_runtime_schema,
    query_spec_output_schema,
    resolve_query_values,
)
from tools.timing import agent_timing


_LOG_PATH = os.environ.get("RETRIEVE_BIGQUERY_LOG_PATH", "retrieve_bigquery_steps.jsonl")
_logger = logging.getLogger(__name__)


def retrieve_bigquery(
    state: AgentState,
    client: Any = None,
    model: str | None = None,
    bq_client: Any = None,
    *,
    timing_context: Mapping[str, object] | None = None,
) -> AgentState:
    """Plan once from the supervisor handoff, then validate and execute."""
    model = model or os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise ValueError("ANTHROPIC_MODEL is required or pass model=...")
    client = client or _build_anthropic_client()
    bq_client = bq_client or bigquery.Client()

    original_question = state.query.raw_text
    database_question = (
        state.request_plan.structured_data_question
        if state.request_plan is not None and state.request_plan.structured_data_question
        else original_question
    )
    started = time.monotonic()
    stage = "schema"
    planner_results: list[Any] = []
    planner_attempts = 0
    operation: str | None = None
    trace_stages: list[dict[str, Any]] = []
    stage_started = time.monotonic()

    try:
        with agent_timing("database.schema", timing_context=timing_context):
            columns = load_runtime_schema(bq_client)
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            column_count=len(columns),
        )

        identifier_candidates = resolve_identifier_candidates(database_question, columns)

        stage = "planning"
        stage_started = time.monotonic()
        with agent_timing("database.plan", timing_context=timing_context):
            planner_attempts += 1
            planned = plan_database_query(
                client=client,
                model=model,
                database_question=database_question,
                output_schema=database_plan_output_schema(query_spec_output_schema(columns)),
                schema_catalog=build_schema_catalog(columns),
                identifier_candidates=identifier_candidates,
            )
            planner_results.append(planned)
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            planner_spec=planned.payload,
            attempt=1,
            planner_metadata=planning_log_metadata(planned),
        )

        stage = "validation"
        stage_started = time.monotonic()
        try:
            with agent_timing("database.validate", timing_context=timing_context):
                database_plan = parse_database_plan(planned.payload, columns)
                spec = database_plan.query_spec
                operation = spec.operation
            _record_trace_stage(
                trace_stages,
                stage=stage,
                started=stage_started,
                attempt=1,
                validated_spec=spec,
            )
        except UnsupportedDatabaseQuery:
            raise
        except DatabaseQueryError as validation_error:
            _record_trace_failure(
                trace_stages,
                stage=stage,
                started=stage_started,
                error=validation_error,
                attempt=1,
            )
            stage = "planning"
            stage_started = time.monotonic()
            with agent_timing("database.plan_repair", timing_context=timing_context):
                planner_attempts += 1
                planned = plan_database_query(
                    client=client,
                    model=model,
                    database_question=database_question,
                    output_schema=database_plan_output_schema(query_spec_output_schema(columns)),
                    schema_catalog=build_schema_catalog(columns),
                    identifier_candidates=identifier_candidates,
                    repair_context=PlannerRepairContext(
                        rejected_payload=planned.payload,
                        validator_error=str(validation_error),
                    ),
                )
                planner_results.append(planned)
            _record_trace_stage(
                trace_stages,
                stage=stage,
                started=stage_started,
                attempt=2,
                planner_spec=planned.payload,
                planner_metadata=planning_log_metadata(planned),
            )
            stage = "validation"
            stage_started = time.monotonic()
            try:
                with agent_timing("database.validate_repair", timing_context=timing_context):
                    database_plan = parse_database_plan(planned.payload, columns)
                    spec = database_plan.query_spec
                    operation = spec.operation
                _record_trace_stage(
                    trace_stages,
                    stage=stage,
                    started=stage_started,
                    attempt=2,
                    validated_spec=spec,
                )
            except UnsupportedDatabaseQuery:
                raise
            except DatabaseQueryError:
                raise

        stage = "value_resolution"
        stage_started = time.monotonic()
        with agent_timing("database.value_resolution", timing_context=timing_context):
            spec = resolve_query_values(spec, columns, bq_client)
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            resolved_spec=spec,
        )

        stage = "compilation"
        stage_started = time.monotonic()
        context_columns = (
            context_columns_for_original_question(original_question, columns)
            if spec.operation in {"rows", "cohort_rows"}
            else ()
        )
        with agent_timing("database.compile", timing_context=timing_context):
            compiled = compile_query(spec, columns, context_columns=context_columns)
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            query_type=compiled.query_type,
            required_columns=compiled.required_columns,
            context_columns=compiled.context_columns,
            sql=compiled.sql,
            query_parameters=_query_parameters_trace(compiled.query_parameters),
        )

        stage = "dry_run"
        stage_started = time.monotonic()
        with agent_timing("database.dry_run", timing_context=timing_context):
            dry_run_passed = dry_run_query(compiled, bq_client)
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            dry_run_passed=dry_run_passed,
        )

        stage = "execution"
        stage_started = time.monotonic()
        with agent_timing("database.execute", timing_context=timing_context) as span:
            rows = execute_query(compiled, bq_client)
            span.set(rows=len(rows))
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            rows_returned=len(rows),
        )

        stage = "packet"
        stage_started = time.monotonic()
        with agent_timing("database.packet", timing_context=timing_context):
            packet = build_database_packet(
                original_question=original_question,
                database_question=database_question,
                spec=spec,
                compiled=compiled,
                rows=rows,
                dry_run_passed=dry_run_passed,
                output_resolution=database_plan.output_resolution.model_dump(mode="json"),
            )
        _record_trace_stage(
            trace_stages,
            stage=stage,
            started=stage_started,
            query_type=packet.get("query_type"),
            required_columns=packet.get("required_columns", []),
            context_columns=packet.get("context_columns", []),
        )
    except QueryClarificationRequired as exc:
        _record_trace_failure(trace_stages, stage=stage, started=stage_started, error=exc, **_trace_attempt(stage, planner_attempts))
        rows = []
        packet = _outcome_packet(
            original_question,
            database_question,
            query_type="clarification",
            reason=str(exc),
            clarification_needed=True,
        )
    except UnsupportedDatabaseQuery as exc:
        _record_trace_failure(trace_stages, stage=stage, started=stage_started, error=exc, **_trace_attempt(stage, planner_attempts))
        rows = []
        packet = _outcome_packet(
            original_question,
            database_question,
            query_type="unsupported",
            reason=str(exc),
        )
    except (DatabasePlanningError, DatabaseQueryError) as exc:
        _record_trace_failure(trace_stages, stage=stage, started=stage_started, error=exc, **_trace_attempt(stage, planner_attempts))
        rows = []
        packet = _outcome_packet(
            original_question,
            database_question,
            query_type="execution_failed",
            reason=_failure_reason(stage, exc),
            execution_failed=True,
            failure_stage=stage,
        )
    except Exception as exc:  # keep the graph boundary controlled
        _record_trace_failure(trace_stages, stage=stage, started=stage_started, error=exc, **_trace_attempt(stage, planner_attempts))
        _logger.exception("database agent failed at %s", stage)
        rows = []
        packet = _outcome_packet(
            original_question,
            database_question,
            query_type="execution_failed",
            reason=f"Database {stage} failed unexpectedly ({type(exc).__name__}).",
            execution_failed=True,
            failure_stage=stage,
        )

    if state.include_debug_trace:
        packet["debug_trace"] = {
            "schema_version": "1.0.0",
            "run_id": state.run_id,
            "database_question": database_question,
            "stages": trace_stages,
        }

    _append_log(
        {
            "run_id": state.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": "retrieve_bigquery",
            "stage": stage,
            "operation": operation,
            "query_type": packet.get("query_type"),
            "dry_run_passed": packet.get("dry_run_passed"),
            "rows_returned": packet.get("rows_returned", 0),
            "execution_failed": packet.get("execution_failed", False),
            "clarification_needed": packet.get("clarification_needed", False),
            "node_latency_ms": int((time.monotonic() - started) * 1000),
            **_planner_log_metadata(planner_results, planner_attempts),
        }
    )
    return state.model_copy(
        update={"bigquery_results": rows, "database_evidence": packet}
    )


def _build_anthropic_client() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required when no client is provided")
    from anthropic import Anthropic

    return Anthropic()


def _outcome_packet(
    original_question: str,
    database_question: str,
    *,
    query_type: str,
    reason: str,
    clarification_needed: bool = False,
    execution_failed: bool = False,
    failure_stage: str | None = None,
) -> dict[str, Any]:
    reason = " ".join(reason.split())[:300] or "The database request could not be completed."
    packet: dict[str, Any] = {
        "schema_version": "2.1.0",
        "agent": "database_agent",
        "question": original_question,
        "database_question": database_question,
        "query_type": query_type,
        "tables_or_views_used": [],
        "sql": None,
        "dry_run_passed": False,
        "rows_returned": 0,
        "row_count_before_limit": 0,
        "required_columns": [],
        "context_columns": [],
        "evidence_rows": [],
        "context_rows": [],
        "output_resolution": {"requested": [], "resolved": [], "unavailable": [], "partial": False},
        "limitations": [reason],
        "clarification_needed": clarification_needed,
        "execution_failed": execution_failed,
        "requires_synthesis": True,
        "planner_table": {
            "columns": [
                {"name": "evidence_id", "label": "Evidence ID", "unit": None, "source_column": None},
                {"name": "process_id", "label": "Process ID", "unit": None, "source_column": None},
            ],
            "rows": [],
            "rows_returned": 0,
            "result_limit": 50,
            "truncated": False,
            "schema_caveats": [],
        },
    }
    if failure_stage:
        packet["failure_stage"] = failure_stage
    return packet


def _failure_reason(stage: str, exc: Exception) -> str:
    if isinstance(exc, DatabasePlanningError):
        return "The single database planning call failed; no SQL was generated."
    if isinstance(exc, QueryExecutionError):
        return str(exc)
    return f"The database query specification failed validation at {stage}."


def _record_trace_stage(
    stages: list[dict[str, Any]],
    *,
    stage: str,
    started: float,
    **details: Any,
) -> None:
    stages.append(
        {
            "stage": stage,
            "status": "passed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            **_json_safe(details),
        }
    )


def _record_trace_failure(
    stages: list[dict[str, Any]],
    *,
    stage: str,
    started: float,
    error: Exception,
    **details: Any,
) -> None:
    stages.append(
        {
            "stage": stage,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_type": type(error).__name__,
            "error": " ".join(str(error).split())[:300],
            **_json_safe(details),
        }
    )


def _query_parameters_trace(parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for parameter in parameters:
        to_api_repr = getattr(parameter, "to_api_repr", None)
        if callable(to_api_repr):
            trace.append(_json_safe(to_api_repr()))
            continue
        trace.append(
            _json_safe(
                {
                    "name": getattr(parameter, "name", None),
                    "type": getattr(parameter, "type_", None),
                    "value": getattr(parameter, "value", None),
                }
            )
        )
    return trace


def _json_safe(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _append_log(record: dict[str, Any]) -> None:
    try:
        from storage.agent_log import append_run_log

        append_run_log(record, fallback_path=_LOG_PATH)
    except Exception as exc:
        _logger.warning("database agent log write failed (%s)", type(exc).__name__)


def _planner_log_metadata(results: list[Any], planner_attempts: int) -> dict[str, Any]:
    """Persist aggregate planner diagnostics without rejected QuerySpecs."""
    if not results:
        return {"planner_attempts": planner_attempts, "repair_used": planner_attempts > 1, "input_tokens": None, "output_tokens": None}
    metadata = planning_log_metadata(results[-1])
    metadata["planner_attempts"] = planner_attempts
    metadata["repair_used"] = planner_attempts > 1
    for key in ("input_tokens", "output_tokens"):
        values = [planning_log_metadata(result)[key] for result in results]
        metadata[key] = sum(value for value in values if value is not None) if any(value is not None for value in values) else None
    return metadata


def _trace_attempt(stage: str, planner_attempts: int) -> dict[str, int]:
    return {"attempt": planner_attempts} if stage in {"planning", "validation"} and planner_attempts else {}

"""Supervisor node — routes retrieval lanes from a request plan."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from agent.request_plan import (
    KnowledgeSource,
    RequestPlan,
    RequestStatus,
)
from agent.supervisor_routing import routed_agents
from agent.schemas import (
    DocumentQueryPlan,
    DocumentQueryPlanningRequest,
    DocumentQueryScope,
)
from agent.state import DocumentQueryRewrite, AgentState
from tools.retrieval.document_scope import (
    EXTERNAL_SCOPE,
    INTERNAL_SCOPE,
    DocumentScope,
)

logger = logging.getLogger(__name__)

# File path is configurable via env var; defaults to the working directory.
_LOG_PATH = os.environ.get("SUPERVISOR_LOG_PATH", "supervisor_calls.jsonl")


def _append_call_log(record: dict) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("supervisor | could not write call log: %s", exc)

def supervisor(
    state: AgentState,
    *,
    table_agent: Any = None,
    internal_document_agent: Any = None,
    external_document_agent: Any = None,
    document_query_planner: Any = None,
    document_query_rewriter: Any = None,
) -> AgentState:
    """Page only the retrieval lanes selected by ``state.request_plan``.

    table_agent / document agents are pre-compiled LangGraph runnables injected
    by the graph factory. document_query_planner is injected so tests can supply
    a deterministic fake for the combined database-plus-document path.

    Updates state with whichever evidence packets were produced.
    """
    run_id = state.run_id
    plan = state.request_plan or RequestPlan(
        status=RequestStatus.NEEDS_CLARIFICATION,
        reasons=["A request plan is required before retrieval."],
    )
    updates: dict[str, Any] = {}
    agents_paged: list[str] = []
    agents_empty: list[str] = []
    lane_failures: list[dict[str, str]] = []
    execution_reasons: list[str] = list(plan.reasons)
    planning_warnings: list[str] = []
    clarification_reasons: list[str] = []
    unsupported_reasons: list[str] = []

    agents_to_route = routed_agents(plan)
    needs_db = "table_agent" in agents_to_route
    scopes = tuple(
        agent.removesuffix("_document_agent")
        for agent in agents_to_route
        if agent.endswith("_document_agent")
    )
    needs_document = bool(scopes)
    combined_retrieval = needs_db and needs_document

    logger.info("supervisor | run_id: %s | query: %r", run_id, state.query.raw_text)
    logger.info("supervisor | run_id: %s | request plan: %s", run_id, plan.model_dump(mode="json"))

    # Once a request has an outcome, rerunning the graph must not re-page lanes.
    if plan.status != RequestStatus.READY:
        return _finish(
            state,
            plan,
            updates,
            agents_paged,
            agents_empty,
            scopes,
            _not_needed_planning_meta(),
            execution_reasons,
            lane_failures=lane_failures,
        )

    working_state = state
    if needs_db and table_agent is not None:
        logger.info(
            "supervisor -> table_agent | run_id: %s | query: %r | sources: %s",
            run_id,
            state.query.raw_text,
            [source.value for source in plan.knowledge_sources],
        )
        try:
            db_result = _invoke_agent(table_agent, working_state)
            db_evidence = _result_value(db_result, "database_evidence", {})
            db_rows = _result_value(db_result, "bigquery_results", [])
            updates["database_evidence"] = db_evidence
            updates["bigquery_results"] = db_rows
            working_state = working_state.model_copy(update={"database_evidence": db_evidence, "bigquery_results": db_rows})
            agents_paged.append("table_agent")
            if _database_execution_failed(db_evidence):
                reason = _database_failure_reason(db_evidence)
                lane_failures.append({"lane": "structured", "agent": "table_agent", "reason": reason})
                _extend_unique(execution_reasons, [reason])
            elif _database_unsupported(db_evidence):
                reason = _database_failure_reason(db_evidence)
                agents_empty.append("table_agent")
                _extend_unique(unsupported_reasons, [reason])
            elif db_evidence.get("clarification_needed"):
                reason = (db_evidence.get("limitations") or [
                    "The structured-data request needs clarification."
                ])[0]
                agents_empty.append("table_agent")
                _extend_unique(clarification_reasons, [reason])
            elif not _has_database_evidence(db_evidence, db_rows):
                agents_empty.append("table_agent")
        except Exception as exc:
            agents_paged.append("table_agent")
            reason = f"Structured data retrieval failed ({type(exc).__name__})."
            lane_failures.append({"lane": "structured", "agent": "table_agent", "reason": reason})
            _extend_unique(execution_reasons, [reason])
    elif needs_db:
        reason = "Structured data retrieval is not configured."
        lane_failures.append({"lane": "structured", "agent": "table_agent", "reason": reason})
        _extend_unique(execution_reasons, [reason])

    document_query_plan: DocumentQueryPlan | None = None
    planning_meta = _not_needed_planning_meta()
    queries_by_scope: dict[DocumentScope, list[str]] = {}
    query_ids_by_scope: dict[DocumentScope, list[str]] = {}

    if combined_retrieval:
        document_query_plan, planning_meta, planning_reasons = _plan_document_queries(
            working_state,
            document_query_planner,
        )
        _extend_unique(planning_warnings, planning_reasons)
        updates["document_query_plan"] = document_query_plan

        if planning_meta["status"] == "planned" and document_query_plan is not None:
            queries_by_scope, query_ids_by_scope, excluded_query_ids = _dispatch_planned_queries(
                document_query_plan,
                scopes,
            )
            planning_meta["dispatched_query_ids_by_scope"] = {
                scope: query_ids_by_scope.get(scope, []) for scope in scopes
            }
            planning_meta["source_scope_excluded_query_ids"] = excluded_query_ids
            dispatched_queries = _unique_queries(
                query
                for scope in scopes
                for query in queries_by_scope.get(scope, [])
            )
            updates["retrieval_queries"] = dispatched_queries
            if excluded_query_ids:
                _extend_unique(
                    planning_warnings,
                    [
                        "DB-informed document queries were excluded by the requested "
                        "document source scopes: " + ", ".join(excluded_query_ids)
                    ],
                )
        else:
            planning_meta["fallback"] = "document_only"
            updates["retrieval_queries"] = []
            if document_query_rewriter is not None:
                rewrite = _invoke_document_query_rewriter(
                    document_query_rewriter,
                    state.query.raw_text,
                )
                updates["document_query_rewrite"] = rewrite
                working_state = working_state.model_copy(
                    update={"document_query_rewrite": rewrite}
                )

    working_state = working_state.model_copy(
        update={
                "document_query_plan": document_query_plan if combined_retrieval else state.document_query_plan,
        }
    )

    document_evidence = dict(state.document_evidence)
    resolved_document_targets = list(state.resolved_document_targets)
    if needs_document:
        for scope in scopes:
            if combined_retrieval and planning_meta["status"] == "planned" and not queries_by_scope.get(scope):
                continue
            document_agent = _document_agent_for_scope(
                scope,
                internal_document_agent=internal_document_agent,
                external_document_agent=external_document_agent,
            )
            if document_agent is None:
                logger.info("supervisor | run_id: %s | %s_document_agent not configured, skipping", run_id, scope)
                agent_name = f"{scope}_document_agent"
                reason = _lane_failure_reason(scope, "not configured")
                lane_failures.append({"lane": scope, "agent": agent_name, "reason": reason})
                _extend_unique(execution_reasons, [reason])
                continue

            scope_queries = queries_by_scope.get(scope, []) if combined_retrieval else list(state.retrieval_queries)
            document_state = working_state.model_copy(
                update={
                    "retrieval_queries": scope_queries,
                    "document_evidence": document_evidence,
                    "resolved_document_targets": resolved_document_targets,
                }
            )
            logger.info(
                "supervisor -> %s_document_agent | run_id: %s | query: %r | sources: %s | scope: %s",
                scope,
                run_id,
                state.query.raw_text,
                [source.value for source in plan.knowledge_sources],
                scope,
            )
            agent_name = f"{scope}_document_agent"
            try:
                result = _invoke_agent(document_agent, document_state)
                agents_paged.append(agent_name)
                packet = _document_packet(result, scope)
                if packet:
                    document_evidence[scope] = packet
                if not _has_document_evidence(packet):
                    agents_empty.append(agent_name)
                result_plan = _result_value(result, "request_plan", None)
                if isinstance(result_plan, dict):
                    result_plan = RequestPlan.model_validate(result_plan)
                if result_plan is not None and result_plan.status == RequestStatus.NEEDS_CLARIFICATION:
                    _extend_unique(clarification_reasons, result_plan.reasons)
                elif result_plan is not None and result_plan.status == RequestStatus.INSUFFICIENT_EVIDENCE:
                    # Another selected lane can still provide sufficient evidence.
                    _extend_unique(planning_warnings, result_plan.reasons)
            except Exception as exc:
                agents_paged.append(agent_name)
                reason = _lane_failure_reason(scope, type(exc).__name__)
                lane_failures.append({"lane": scope, "agent": agent_name, "reason": reason})
                _extend_unique(execution_reasons, [reason])
                continue
            for target in _result_value(result, "resolved_document_targets", []) or []:
                if target not in resolved_document_targets:
                    resolved_document_targets.append(target)

    if document_evidence:
        updates["document_evidence"] = document_evidence
    if resolved_document_targets:
        updates["resolved_document_targets"] = resolved_document_targets

    if plan.status == RequestStatus.READY:
        database_evidence = updates.get("database_evidence", state.database_evidence)
        database_rows = updates.get("bigquery_results", state.bigquery_results)
        usable_evidence = _has_usable_evidence(database_evidence, database_rows, document_evidence)
        has_execution_failure = bool(lane_failures)
        if (
            has_execution_failure
            and not usable_evidence
            and KnowledgeSource.GENERAL_KNOWLEDGE not in plan.knowledge_sources
        ):
            plan = plan.model_copy(update={"status": RequestStatus.EXECUTION_FAILED, "reasons": execution_reasons})
        elif clarification_reasons and not usable_evidence:
            plan = plan.model_copy(
                update={"status": RequestStatus.NEEDS_CLARIFICATION, "reasons": clarification_reasons}
            )
        elif unsupported_reasons and not usable_evidence:
            plan = plan.model_copy(
                update={"status": RequestStatus.UNSUPPORTED_REQUEST, "reasons": unsupported_reasons}
            )
        elif _comparison_has_empty_cohort(database_evidence) and not any(
            _has_document_evidence(packet) for packet in document_evidence.values()
        ):
            plan = plan.model_copy(
                update={
                    "status": RequestStatus.INSUFFICIENT_EVIDENCE,
                    "reasons": ["At least one comparison cohort returned no matching runs."],
                }
            )
        elif _completed_zero_row_database_query(database_evidence, database_rows) and not any(
            _has_document_evidence(packet) for packet in document_evidence.values()
        ):
            plan = plan.model_copy(
                update={
                    "status": RequestStatus.INSUFFICIENT_EVIDENCE,
                    "reasons": ["The validated database query returned no matching rows."],
                }
            )
        elif (
            (needs_db or needs_document)
            and not usable_evidence
            and KnowledgeSource.GENERAL_KNOWLEDGE not in plan.knowledge_sources
        ):
            plan = plan.model_copy(
                update={
                    "status": RequestStatus.NEEDS_CLARIFICATION,
                    "reasons": [
                        "No relevant database or literature evidence was found. "
                        "Add a recipe, sample, metric, process condition, paper, or topic to narrow the search."
                    ],
                }
            )
        elif (needs_db or needs_document) and not usable_evidence:
            _extend_unique(
                planning_warnings,
                ["No retrievable evidence was found; continuing with general knowledge."],
            )
        else:
            _extend_unique(planning_warnings, clarification_reasons)
            _extend_unique(planning_warnings, unsupported_reasons)
            if has_execution_failure:
                _extend_unique(planning_warnings, execution_reasons)

    if planning_warnings:
        planning_meta["warnings"] = planning_warnings
    updates["request_plan"] = plan
    updates["document_scopes"] = list(scopes)
    return _finish(
        state,
        plan,
        updates,
        agents_paged,
        agents_empty,
        scopes,
        planning_meta,
        execution_reasons,
        queries_by_scope,
        combined_retrieval,
        lane_failures=lane_failures,
    )


def _finish(
    state: AgentState,
    plan: RequestPlan,
    updates: dict[str, Any],
    agents_paged: list[str],
    agents_empty: list[str],
    scopes: tuple[DocumentScope, ...],
    planning_meta: dict[str, Any],
    reasons: list[str],
    queries_by_scope: dict[DocumentScope, list[str]] | None = None,
    combined_retrieval: bool = False,
    lane_failures: list[dict[str, str]] | None = None,
) -> AgentState:
    """Attach routing metadata and emit a controlled supervisor result."""
    updates["request_plan"] = plan
    updates["document_scopes"] = list(scopes)
    lane_failures = list(lane_failures or [])
    updates["supervisor_meta"] = {
        "run_id": state.run_id,
        "tasks": [task.value for task in plan.tasks],
        "knowledge_sources": [source.value for source in plan.knowledge_sources],
        "request_status": plan.status.value,
        "reasons": list(plan.reasons),
        "agents_routed": routed_agents(plan),
        "agents_paged": agents_paged,
        "agents_empty": agents_empty,
        "lane_failures": lane_failures,
        "document_scopes_resolved": list(scopes),
        "source_mode": state.source_mode,
        "document_query_planning": planning_meta,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _append_call_log({
        "run_id": state.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": state.query.raw_text,
        "request_plan": plan.model_dump(mode="json"),
        "document_scopes_resolved": list(scopes),
        "source_mode": state.source_mode,
        "agents_paged": agents_paged,
        "agents_empty": agents_empty,
        "lane_failures": lane_failures,
        "agents_info": {
            agent: {
                "query": state.query.raw_text,
                "request_plan": plan.model_dump(mode="json"),
                **({"scope": agent.removesuffix("_document_agent")} if "document" in agent else {}),
                **({"retrieval_queries": (queries_by_scope or {}).get(agent.removesuffix("_document_agent"), [])}
                   if "document" in agent and combined_retrieval else {}),
            }
            for agent in agents_paged
        },
    })
    return state.model_copy(update=updates)


def _has_database_evidence(evidence: Any, rows: Any) -> bool:
    if rows:
        return True
    return bool(isinstance(evidence, dict) and evidence.get("evidence_rows"))


def _database_execution_failed(evidence: Any) -> bool:
    """Identify controlled database-planning/execution failure packets."""
    return bool(
        isinstance(evidence, dict)
        and (
            evidence.get("execution_failed") is True
            or evidence.get("query_type") == "execution_failed"
        )
    )


def _database_unsupported(evidence: Any) -> bool:
    return bool(
        isinstance(evidence, dict)
        and evidence.get("query_type") == "unsupported"
    )


def _database_failure_reason(evidence: dict[str, Any]) -> str:
    limitations = evidence.get("limitations")
    if isinstance(limitations, list) and limitations and isinstance(limitations[0], str):
        return limitations[0]
    return "Structured data planning or execution failed."


def _completed_zero_row_database_query(evidence: Any, rows: Any) -> bool:
    """Recognize an executed, dry-run-approved database query with no matches."""
    return bool(
        isinstance(evidence, dict)
        and evidence.get("query_type") not in {"clarification", "execution_failed", "unsupported"}
        and evidence.get("dry_run_passed") is True
        and isinstance(evidence.get("sql"), str)
        and evidence["sql"].strip()
        and evidence.get("rows_returned") == 0
        and not isinstance(evidence.get("rows_returned"), bool)
        and not rows
    )


def _comparison_has_empty_cohort(evidence: Any) -> bool:
    return bool(
        isinstance(evidence, dict)
        and evidence.get("query_type") == "comparison"
        and any(
            isinstance(result, dict) and result.get("insufficient_evidence")
            for result in evidence.get("cohort_results", [])
        )
    )


def _has_document_evidence(packet: Any) -> bool:
    return bool(isinstance(packet, dict) and packet.get("contexts"))


def _has_usable_evidence(database_evidence: Any, rows: Any, document_evidence: dict[str, Any]) -> bool:
    return _has_database_evidence(database_evidence, rows) or any(
        _has_document_evidence(packet) for packet in document_evidence.values()
    )


def _lane_failure_reason(scope: DocumentScope, detail: str) -> str:
    label = "Internal documents" if scope == INTERNAL_SCOPE else "External literature"
    suffix = "is not configured" if detail == "not configured" else f"retrieval failed ({detail})"
    return f"{label} {suffix}."


def _not_needed_planning_meta() -> dict[str, Any]:
    return {
        "status": "not_needed",
        "plan": None,
        "input": None,
        "input_provenance": {
            "question": "state.query.raw_text",
            "planner_table": "database_evidence.planner_table",
            "database_limitations": "database_evidence.limitations",
        },
        "dispatched_query_ids_by_scope": {},
        "source_scope_excluded_query_ids": [],
        "fallback": "none",
        "error_type": None,
    }


def _plan_document_queries(
    state: AgentState,
    planner: Any,
) -> tuple[DocumentQueryPlan | None, dict[str, Any], list[str]]:
    database_evidence = state.database_evidence or {}
    planner_table = database_evidence.get("planner_table")
    database_limitations = database_evidence.get("limitations") or []
    meta = _not_needed_planning_meta()
    meta["input"] = {
        "question": state.query.raw_text,
        "planner_table": planner_table,
        "database_limitations": list(database_limitations),
    }

    try:
        request = DocumentQueryPlanningRequest.model_validate(meta["input"])
    except ValidationError as exc:
        meta.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        return (
            None,
            meta,
            [
                "DB-informed document query planner input was invalid; document retrieval "
                "used the existing document-only fallback."
            ],
        )

    if not request.planner_table.rows:
        meta["status"] = "empty_database"
        return (
            None,
            meta,
            ["No database rows were returned to ground DB-informed document queries."],
        )

    try:
        if planner is None:
            raise RuntimeError("document query planner is not configured")
        raw_plan = planner.plan(request) if hasattr(planner, "plan") else planner(request)
        plan = DocumentQueryPlan.model_validate(raw_plan)
    except ValidationError as exc:
        meta.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        return (
            None,
            meta,
            [
                "DB-informed document query planner output was invalid; document retrieval "
                "used the existing document-only fallback."
            ],
        )
    except Exception as exc:
        meta.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        return (
            None,
            meta,
            [
                "DB-informed document query planner failed; document retrieval "
                "used the existing document-only fallback."
            ],
        )

    meta["plan"] = plan.model_dump(mode="json")
    reasons: list[str] = []
    if not plan.queries:
        meta["status"] = "no_queries"
        _extend_unique(reasons, [gap.description for gap in plan.data_gaps])
        _extend_unique(
            reasons,
            ["The DB-informed document query planner returned no document queries."],
        )
    else:
        meta["status"] = "planned"
    return plan, meta, reasons


def _dispatch_planned_queries(
    plan: DocumentQueryPlan,
    allowed_scopes: tuple[DocumentScope, ...],
) -> tuple[
    dict[DocumentScope, list[str]],
    dict[DocumentScope, list[str]],
    list[str],
]:
    queries_by_scope: dict[DocumentScope, list[str]] = {}
    query_ids_by_scope: dict[DocumentScope, list[str]] = {}
    excluded_query_ids: list[str] = []
    allowed = set(allowed_scopes)
    ordered_queries = sorted(
        enumerate(plan.queries),
        key=lambda item: (item[1].priority, item[0]),
    )

    for _, query in ordered_queries:
        requested_scopes = _planner_query_scopes(query.scope)
        dispatched = [scope for scope in allowed_scopes if scope in requested_scopes and scope in allowed]
        if not dispatched:
            excluded_query_ids.append(query.query_id)
            continue
        for scope in dispatched:
            queries_by_scope.setdefault(scope, []).append(query.query)
            query_ids_by_scope.setdefault(scope, []).append(query.query_id)
    return queries_by_scope, query_ids_by_scope, excluded_query_ids


def _planner_query_scopes(scope: DocumentQueryScope) -> tuple[DocumentScope, ...]:
    if scope == DocumentQueryScope.INTERNAL:
        return (INTERNAL_SCOPE,)
    if scope == DocumentQueryScope.LITERATURE:
        return (EXTERNAL_SCOPE,)
    return (INTERNAL_SCOPE, EXTERNAL_SCOPE)


def _invoke_document_query_rewriter(rewriter: Any, raw_query: str) -> DocumentQueryRewrite:
    raw_rewrite = rewriter(raw_query)
    return DocumentQueryRewrite.model_validate(raw_rewrite)


def _unique_queries(queries: Any) -> list[str]:
    unique: list[str] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return unique


def _extend_unique(values: list[str], additions: Any) -> None:
    for addition in additions:
        if addition not in values:
            values.append(addition)


def _invoke_agent(agent: Any, state: AgentState) -> Any:
    if hasattr(agent, "invoke"):
        return agent.invoke(state)
    return agent(state)


def _result_value(result: Any, key: str, default: Any) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _document_agent_for_scope(
    scope: DocumentScope,
    *,
    internal_document_agent: Any,
    external_document_agent: Any,
) -> Any:
    if scope == INTERNAL_SCOPE:
        return internal_document_agent
    return external_document_agent


def _document_packet(result: Any, scope: DocumentScope) -> dict:
    if isinstance(result, dict):
        document_evidence = result.get("document_evidence") or {}
        if scope in document_evidence:
            return document_evidence[scope]
        return {}

    document_evidence = getattr(result, "document_evidence", {}) or {}
    if scope in document_evidence:
        return document_evidence[scope]
    return {}

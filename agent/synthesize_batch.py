"""Run a JSON question batch through the synthesis graph.

Usage:
    python -m agent.synthesize_batch questions.json synthesize.csv --model claude-sonnet
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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent.graph import build_supervisor_agent
from agent.state import AgentState, SourceMode, UserQuery


_CSV_FIELDS = [
    "eval_id",
    "original_query",
    "run_id",
    "request_plan",
    "database_evidence",
    "document_evidence",
    "source_documents",
    "evidence_packet",
    "final_answer",
    "supervisor_meta",
    "status",
    "execution_status",
    "request_status",
    "error",
    "diagnostic_id",
    "failure_explanation",
    "traceback",
    "stage_timings",
    "attempts",
]
_RESPONSE_JSON_FIELDS = [
    "request_plan",
    "database_evidence",
    "document_evidence",
    "source_documents",
    "evidence_packet",
    "final_answer",
    "supervisor_meta",
]

AgentBuilder = Callable[..., Any]
FailureExplainer = Callable[[str, BaseException], str]


def run_batch(
    input_path: Path,
    output_path: Path,
    *,
    model: str | None = None,
    agent_builder: AgentBuilder | None = None,
    failure_explainer: FailureExplainer | None = None,
) -> None:
    """Run planning, retrieval, and synthesis once per question; stop at synthesis."""
    questions = _load_questions(input_path)
    if agent_builder is None:
        agent_builder = lambda *, model: _build_batch_agent(model=model)

    diagnostic_path = output_path.with_suffix(output_path.suffix + ".jsonl")
    with output_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        with diagnostic_path.open("w", encoding="utf-8") as diagnostic_file:
            for index, item in enumerate(questions, start=1):
                row, diagnostic = _run_question(
                    eval_id=f"Q{index:03d}",
                    question=item["question"],
                    source_mode=item.get("source_mode"),
                    document_ids=item.get("document_ids", []),
                    model=model,
                    agent_builder=agent_builder,
                    failure_explainer=failure_explainer,
                )
                writer.writerow(row)
                diagnostic_file.write(json.dumps(_redact_value(diagnostic), ensure_ascii=False, default=str) + "\n")
                output_file.flush()
                diagnostic_file.flush()


def _build_batch_agent(*, model: str | None):
    return build_supervisor_agent(model=model, include_verifier=False)


def _load_questions(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open(encoding="utf-8") as input_file:
        payload = json.load(input_file)

    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array")
    questions = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each input item must be an object with a string question or query")
        question = item.get("question")
        if not isinstance(question, str):
            question = item.get("query")
        if not isinstance(question, str):
            raise ValueError("each input item must be an object with a string question or query")
        questions.append({**item, "question": question})
    return questions


def _run_question(
    *,
    eval_id: str,
    question: str,
    source_mode: SourceMode | None = None,
    document_ids: list[str] | None = None,
    model: str | None,
    agent_builder: AgentBuilder,
    failure_explainer: FailureExplainer | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    started = time.monotonic()
    diagnostic_id = str(uuid.uuid4())
    result = {
        "eval_id": eval_id,
        "original_query": question,
        "run_id": "",
        "request_plan": "",
        "database_evidence": "",
        "document_evidence": "",
        "source_documents": "",
        "evidence_packet": "",
        "final_answer": "",
        "status": "error",
        "execution_status": "error",
        "request_status": "",
        "error": "",
        "diagnostic_id": diagnostic_id,
        "failure_explanation": "",
        "traceback": "",
        "stage_timings": "",
        "attempts": "",
    }
    diagnostic: dict[str, Any] = {
        "diagnostic_id": diagnostic_id,
        "eval_id": eval_id,
        "input": {"question": question, "source_mode": source_mode, "document_ids": document_ids or []},
        "stages": [],
        "attempts": [{"attempt": 1}],
        "execution_status": "error",
        "request_status": "",
    }
    current_stage = "input_validation"
    stage_started = started
    try:
        normalized_document_ids = _normalize_document_ids(document_ids or [])
        effective_source_mode = source_mode or "all"
        if effective_source_mode == "selected" and not normalized_document_ids:
            raise ValueError("document_ids must be provided when source_mode is selected")
        current_stage = "graph_build"
        stage_started = time.monotonic()
        agent = agent_builder(model=model)
        diagnostic["stages"].append({"stage": "graph_build", "status": "succeeded", "duration_ms": _duration_ms(stage_started)})
        current_stage = "synthesis_graph"
        stage_started = time.monotonic()
        response = _response_from_result(agent.invoke(AgentState(
            query=UserQuery(raw_text=question),
            source_mode=effective_source_mode,
            selected_document_ids=normalized_document_ids,
            source_filters=(
                {"document_id": normalized_document_ids}
                if normalized_document_ids
                else {}
            ),
            include_debug_trace=True,
        )))
        diagnostic["response"] = response
        diagnostic["stages"].append({"stage": "synthesis_graph", "status": "succeeded", "duration_ms": _duration_ms(stage_started)})
        result["run_id"] = str(response.get("run_id") or "")
        for field in _RESPONSE_JSON_FIELDS:
            result[field] = json.dumps(response.get(field), ensure_ascii=False, default=str)
        result["execution_status"] = "succeeded"
        result["request_status"] = _request_status(response.get("request_plan"))
        result["status"] = result["request_status"] or result["execution_status"]
        diagnostic["execution_status"] = result["execution_status"]
        diagnostic["request_status"] = result["request_status"]
    except Exception as exc:
        error = _exception_record(exc)
        diagnostic["error"] = error
        diagnostic["stages"].append({"stage": current_stage, "status": "failed", "duration_ms": _duration_ms(stage_started), "error": error["summary"]})
        result["error"] = error["summary"]
        result["traceback"] = error["traceback"]
        result["failure_explanation"] = _explain_failure(question, exc, failure_explainer)
        result["final_answer"] = json.dumps({"synthesis": {"answer": result["failure_explanation"], "failure": True}}, ensure_ascii=False)
        print(error["traceback"], file=sys.stderr)
    diagnostic["duration_ms"] = _duration_ms(started)
    diagnostic["output"] = {field: result.get(field, "") for field in _RESPONSE_JSON_FIELDS}
    database_evidence = diagnostic.get("response", {}).get("database_evidence")
    if isinstance(database_evidence, dict) and isinstance(database_evidence.get("debug_trace"), dict):
        diagnostic["database_debug_trace"] = database_evidence["debug_trace"]
        planner_spec = _planner_spec(database_evidence["debug_trace"])
        if planner_spec is not None:
            diagnostic["planner_spec"] = planner_spec
    result["stage_timings"] = json.dumps(diagnostic["stages"], ensure_ascii=False)
    result["attempts"] = json.dumps(diagnostic["attempts"], ensure_ascii=False)
    return result, diagnostic


def _request_status(request_plan: Any) -> str:
    if not isinstance(request_plan, Mapping):
        return ""
    status = request_plan.get("status")
    return str(status) if isinstance(status, str) else ""


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _exception_record(exc: BaseException) -> dict[str, Any]:
    chain = []
    current: BaseException | None = exc
    while current is not None:
        chain.append({"type": type(current).__name__, "message": _redact(str(current))})
        current = current.__cause__ or current.__context__
    return {
        "type": type(exc).__name__,
        "message": _redact(str(exc)),
        "summary": f"{type(exc).__name__}: {_redact(str(exc))}",
        "cause_chain": chain,
        "traceback": _redact("".join(traceback.format_exception(exc))),
    }


def _redact(text: str) -> str:
    text = re.sub(
        r"(?i)(\bauthorization\b\s*[:=]\s*bearer\s+)[^\s,]+",
        r"\1[REDACTED]",
        text,
    )
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


def _planner_spec(debug_trace: dict[str, Any]) -> Any:
    for stage in debug_trace.get("stages") or []:
        if isinstance(stage, dict) and stage.get("stage") == "planning":
            return stage.get("planner_spec")
    return debug_trace.get("planner_spec")


def _explain_failure(
    question: str,
    exc: BaseException,
    failure_explainer: FailureExplainer | None,
) -> str:
    if failure_explainer is not None:
        try:
            explanation = str(failure_explainer(question, exc)).strip()
            if explanation:
                return _redact(explanation)
        except Exception:
            pass
    return _failure_explanation(question, exc)


def _failure_explanation(question: str, exc: BaseException) -> str:
    """Return a user-facing, question-specific explanation without exposing internals."""
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        messages.append(str(current).lower())
        if isinstance(current, PermissionError):
            return f"I could not retrieve evidence for “{question}” because access to a required source was denied."
        current = current.__cause__ or current.__context__
    message = " ".join(messages)
    if isinstance(exc, PermissionError) or any(word in message for word in ("permission", "forbidden", "unauthorized", "403")):
        return f"I could not retrieve evidence for “{question}” because access to a required source was denied."
    if any(word in message for word in ("zero rows", "no rows", "rows_returned: 0")):
        return f"I could not find database rows matching “{question}”."
    if any(word in message for word in ("unresolved identifier", "no candidates", "ambiguous identifier")):
        return f"I could not resolve an identifier in “{question}” to a database field."
    if any(word in message for word in ("invalid query", "validation", "sql", "syntax")):
        return f"I could not run the requested retrieval for “{question}” because its database query was invalid."
    if any(word in message for word in ("insufficient evidence", "no evidence", "empty packet")):
        return f"I could not answer “{question}” because the retrieved evidence was insufficient."
    return f"I could not complete retrieval and synthesis for “{question}”."


def _normalize_document_ids(document_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    for document_id in document_ids:
        text = str(document_id).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _response_from_result(result: object) -> dict[str, Any]:
    """Match the non-streaming ``/agent/synthesize`` response structure."""
    def get_value(key: str, default: Any = None) -> Any:
        if isinstance(result, dict):
            return result.get(key, default)
        return getattr(result, key, default)

    final_answer = get_value("final_answer")
    try:
        final_answer = json.loads(final_answer.model_dump_json()) if final_answer is not None else None
    except Exception:
        final_answer = None

    document_evidence = get_value("document_evidence")
    return {
        "run_id": get_value("run_id"),
        "request_plan": _dump_model(get_value("request_plan")),
        "database_evidence": get_value("database_evidence"),
        "document_evidence": document_evidence,
        "source_documents": _source_documents(document_evidence),
        "evidence_packet": get_value("evidence_packet"),
        "final_answer": final_answer,
        "supervisor_meta": get_value("supervisor_meta"),
    }


def _dump_model(value: object) -> object:
    if value is None:
        return None
    try:
        return json.loads(value.model_dump_json())
    except Exception:
        return value if isinstance(value, dict) else None


def _source_documents(document_evidence: object) -> list[dict[str, str]]:
    if not isinstance(document_evidence, dict):
        return []

    sources: dict[tuple[str, str], dict[str, str]] = {}
    document_store = None

    def original_filename_from_store(document_id: str) -> str:
        nonlocal document_store
        try:
            if document_store is None:
                from storage.bigquery import BigQueryChunkStore

                document_store = BigQueryChunkStore()
            document = document_store.get_document(document_id)
        except Exception:
            return ""
        metadata = document.metadata
        if not isinstance(metadata, Mapping):
            return ""
        return str(metadata.get("original_filename") or "").strip()

    for scope, packet in document_evidence.items():
        if not isinstance(packet, dict):
            continue
        document_scope = str(packet.get("document_scope") or scope)
        contexts = packet.get("contexts")
        if not isinstance(contexts, list):
            continue
        for context in contexts:
            if not isinstance(context, dict):
                continue
            document_id = str(context.get("document_id") or context.get("source_hash") or "").strip()
            if not document_id:
                continue
            metadata = context.get("metadata")
            original_filename = ""
            if isinstance(metadata, Mapping):
                original_filename = str(metadata.get("original_filename") or "").strip()
            if not original_filename:
                original_filename = original_filename_from_store(document_id)
            key = (document_id, document_scope)
            sources.setdefault(
                key,
                {
                    "document_id": document_id,
                    "original_filename": original_filename or document_id,
                    "document_scope": document_scope,
                    "pdf_url": f"/documents/{document_id}/pdf",
                },
            )
    return sorted(sources.values(), key=lambda item: (item["document_scope"], item["original_filename"], item["document_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run JSON questions through the synthesis graph.")
    parser.add_argument("input", type=Path, help="path to a JSON array of question objects")
    parser.add_argument("output", type=Path, help="path to write the CSV results")
    parser.add_argument("--model", help="model passed to agent.graph.build_supervisor_agent")
    args = parser.parse_args()
    run_batch(args.input, args.output, model=args.model)


if __name__ == "__main__":
    main()

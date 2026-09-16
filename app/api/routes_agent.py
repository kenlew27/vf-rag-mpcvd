"""
app/api/routes_agent.py

Agentic routes — invoke compiled LangGraph subagents.

POST /agent/data-query
  Invokes the table-pulling subagent directly. Returns a database evidence parcel.

POST /agent/query
  Invokes the full supervisor graph: plan request → page subagents → synthesize → verify.
  Returns raw evidence parcels plus a final_answer with run_id for log correlation.

POST /agent/synthesize
  Same pipeline as /agent/query. Returns final_answer from the reasoning agent.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from collections.abc import Iterator, Mapping

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.graph import build_supervisor_agent, build_table_agent
from agent.request_plan import KnowledgeSource, RequestPlan, RequestStatus, RequestTask
from agent.run_log import log_error, write_run_log
from agent.state import AgentState, SourceMode, UserQuery
from tools.timing import agent_timing, agent_timing_context

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)

_SSE_HEARTBEAT_INTERVAL_SECONDS = 15


class DataQueryRequest(BaseModel):
    question: str
    supervisor_query: str | None = None
    include_debug_trace: bool = False
    source_mode: SourceMode | None = None
    document_ids: list[str] = Field(default_factory=list)


def _get_document_store():
    from storage.bigquery import BigQueryChunkStore

    return BigQueryChunkStore()


@router.post("/data-query")
def data_query(req: DataQueryRequest):
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")
    if req.supervisor_query is not None and not req.supervisor_query.strip():
        raise HTTPException(400, "supervisor_query must not be empty when provided")

    model = os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise HTTPException(503, "ANTHROPIC_MODEL env var is not set")

    timing_context = {"route": "/agent/data-query"}
    with agent_timing_context(timing_context), agent_timing("route.total"):
        request_plan = None
        if req.supervisor_query is not None:
            request_plan = RequestPlan(
                tasks=[RequestTask.LOOKUP],
                knowledge_sources=[KnowledgeSource.STRUCTURED_DATA],
                status=RequestStatus.READY,
                reasons=[],
                structured_data_question=req.supervisor_query,
            )
        initial_state = AgentState(
            query=UserQuery(raw_text=req.question),
            request_plan=request_plan,
            include_debug_trace=req.include_debug_trace,
        )
        agent = build_table_agent(model=model)
        with agent_timing("table_agent.invoke"):
            result = agent.invoke(initial_state)

        parcel = result.get("database_evidence") if isinstance(result, dict) else result.database_evidence
        return parcel


@router.post("/query")
def query(req: DataQueryRequest):
    """Supervisor graph: plan the request, page subagents, return raw evidence parcels."""
    initial_state, model = _agent_query_input(req)
    timing_context = {"route": "/agent/query"}
    with agent_timing_context(timing_context), agent_timing("route.total"):
        agent = build_supervisor_agent(model=model)
        with agent_timing("supervisor_graph.invoke"):
            result = _invoke_supervisor_agent(agent, initial_state)
        threading.Thread(target=write_run_log, args=(initial_state.run_id,), daemon=True).start()
        return _agent_query_response(result)


@router.post("/synthesize")
def synthesize(req: DataQueryRequest):
    """Full reasoning pipeline: plan → retrieve → synthesize → verify → final answer."""
    initial_state, model = _agent_query_input(req)
    timing_context = {"route": "/agent/synthesize"}
    with agent_timing_context(timing_context), agent_timing("route.total"):
        agent = build_supervisor_agent(model=model)
        with agent_timing("supervisor_graph.invoke"):
            result = _invoke_supervisor_agent(agent, initial_state)
        threading.Thread(target=write_run_log, args=(initial_state.run_id,), daemon=True).start()
        return _agent_query_response(result)


@router.post("/query/stream")
def query_stream(req: DataQueryRequest):
    """Stream supervisor graph progress as server-sent events."""
    initial_state, model = _agent_query_input(req)
    return StreamingResponse(
        _agent_query_events(initial_state, model, timing_context={"route": "/agent/query/stream"}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _agent_query_input(req: DataQueryRequest) -> tuple[AgentState, str]:
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")

    model = os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise HTTPException(503, "ANTHROPIC_MODEL env var is not set")

    source_mode = req.source_mode or "all"
    document_ids = _normalize_document_ids(req.document_ids)
    if source_mode == "selected" and not document_ids:
        raise HTTPException(400, "document_ids must be provided when source_mode is selected")
    source_filters = {"document_id": document_ids} if document_ids else {}

    initial_state = AgentState(
        query=UserQuery(raw_text=req.question),
        source_mode=source_mode,
        selected_document_ids=document_ids,
        source_filters=source_filters,
        include_debug_trace=req.include_debug_trace,
    )
    return initial_state, model


def _normalize_document_ids(document_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    for document_id in document_ids:
        text = str(document_id).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _invoke_supervisor_agent(agent: object, initial_state: AgentState) -> object:
    try:
        return agent.invoke(initial_state)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Agent query failed during supervisor graph invocation.")
        log_error(initial_state.run_id, initial_state.query.raw_text, exc)
        threading.Thread(
            target=write_run_log,
            args=(initial_state.run_id,),
            kwargs={"error": exc},
            daemon=True,
        ).start()
        raise HTTPException(
            status_code=503,
            detail=f"Unable to complete the agent query. Backend logs include {exc.__class__.__name__}.",
        ) from exc


def _agent_query_response(result: object) -> dict:
    def _get(key: str, default=None):
        if isinstance(result, dict):
            return result.get(key, default)
        return getattr(result, key, default)

    final_answer_obj = _get("final_answer")
    final_answer_dict = None
    if final_answer_obj is not None:
        try:
            final_answer_dict = json.loads(final_answer_obj.model_dump_json())
        except Exception:
            final_answer_dict = None

    return {
        "run_id": _get("run_id"),
        "request_plan": _dump_model(_get("request_plan")),
        "database_evidence": _get("database_evidence"),
        "document_evidence": _get("document_evidence"),
        "source_documents": _source_documents(_get("document_evidence")),
        "final_answer": final_answer_dict,
    }


def _dump_model(value: object) -> object:
    if value is None:
        return None
    try:
        return json.loads(value.model_dump_json())
    except Exception:
        return value if isinstance(value, dict) else None


def _agent_query_events(
    initial_state: AgentState,
    model: str,
    *,
    timing_context: Mapping[str, object] | None = None,
) -> Iterator[str]:
    events: queue.Queue[tuple[str, dict] | None] = queue.Queue()

    def progress_callback(stage: str, status: str) -> None:
        events.put(("progress", {"stage": stage, "state": status}))

    def run_agent() -> None:
        with agent_timing_context(timing_context):
            try:
                with agent_timing("route.total"):
                    agent = build_supervisor_agent(model=model, progress_callback=progress_callback)
                    with agent_timing("supervisor_graph.invoke"):
                        result = agent.invoke(initial_state)
                    events.put(("result", _agent_query_response(result)))
            except Exception:
                logger.exception("Agent query failed during supervisor graph invocation.")
                events.put(("error", {"message": "Unable to complete the agent query."}))
            finally:
                events.put(None)

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    while True:
        try:
            event = events.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if event is None:
            break
        event_name, data = event
        yield _sse(event_name, data)

    thread.join(timeout=0)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _source_documents(document_evidence: object) -> list[dict[str, str]]:
    if not isinstance(document_evidence, dict):
        return []

    sources: dict[tuple[str, str], dict[str, str]] = {}
    document_store = None

    def original_filename_from_store(document_id: str) -> str:
        nonlocal document_store
        try:
            if document_store is None:
                document_store = _get_document_store()
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

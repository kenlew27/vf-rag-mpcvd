"""
agent/graph.py

Compiled LangGraph subagents and the top-level supervisor graph.

Architecture:
  plan_request → supervisor (pages subagents) → synthesize_answer → verify_answer → compose_verified_answer → END

Subagents:
  build_table_agent()      — pulls structured evidence from BigQuery.
  build_document_agent()   — retrieves scoped document context.
  build_supervisor_agent() — plan the request → page subagents → return parcels and synthesize answer.
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from agent.document_query_planner import AnthropicDocumentQueryPlanner
from agent.request_plan import KnowledgeSource
from agent.nodes.plan_request import plan_request
from agent.nodes.retrieve_bigquery import retrieve_bigquery
from agent.nodes.retrieve_document import rewrite_document_query
from agent.nodes.retrieve_external_document import retrieve_external_document
from agent.nodes.retrieve_internal_document import retrieve_internal_document
from agent.nodes.supervisor import supervisor
from agent.nodes.synthesize_answer import synthesize_answer
from agent.nodes.verify_answer import verify_answer
from agent.nodes.compose_verified_answer import compose_verified_answer
from agent.schemas import WorkflowOutcome
from agent.state import AgentState
from tools.retrieval.document_scope import EXTERNAL_SCOPE, INTERNAL_SCOPE, normalize_document_scope
from tools.timing import agent_timing

ProgressCallback = Callable[[str, str], None]

def build_table_agent(
    client: Any = None,
    model: str | None = None,
    bq_client: Any = None,
    progress_callback: ProgressCallback | None = None,
):
    """Compile the table-pulling subagent.

    Single-node graph: the database_agent node extracts filters from the user
    question via Claude, queries BigQuery, and stores a structured knowledge
    parcel (evidence packet) in state.database_evidence.

    client / model  — Anthropic client + model string (injected; falls back to env vars).
    bq_client       — BigQuery client (injected for testing; ADC used if omitted).

    Returns a compiled LangGraph runnable. Invoke with:
        result = agent.invoke({"query": {"raw_text": "your question"}})
        parcel = result["database_evidence"]
    """

    def database_agent(state: AgentState) -> dict:
        _emit_progress(progress_callback, "bigquery", "started")
        with agent_timing("agent.database.total"):
            updated = retrieve_bigquery(state, client=client, model=model, bq_client=bq_client)
        _emit_progress(progress_callback, "bigquery", "completed")
        return {
            "bigquery_results": updated.bigquery_results,
            "database_evidence": updated.database_evidence,
        }

    builder = StateGraph(AgentState)
    builder.add_node("database_agent", database_agent)
    builder.add_edge(START, "database_agent")
    builder.add_edge("database_agent", END)

    return builder.compile()


def build_document_agent(
    document_scope: str,
    client: Any = None,
    model: str | None = None,
    vector_index: Any = None,
    keyword_index: Any = None,
    chunk_store: Any = None,
    query_embedder: Any = None,
    reranker: Any = None,
    limit: int = 10,
    progress_callback: ProgressCallback | None = None,
):
    """Compile a scoped document retrieval subagent.

    Single-node graph: the scoped document node rewrites the user question,
    runs the retrieval pipeline, and stores a structured knowledge parcel in
    state.document_evidence[scope].
    """
    scope = normalize_document_scope(document_scope)
    node_name = f"{scope}_document_agent"
    retrieve_scoped_document = retrieve_internal_document if scope == INTERNAL_SCOPE else retrieve_external_document
    progress_stage = "internal_documents" if scope == INTERNAL_SCOPE else "external_documents"

    def document_retrieval_agent(state: AgentState) -> dict:
        _emit_progress(progress_callback, progress_stage, "started")
        with agent_timing("agent.document.total", timing_context={"scope": scope}):
            updated = retrieve_scoped_document(
                state,
                client=client,
                model=model,
                vector_index=vector_index,
                keyword_index=keyword_index,
                chunk_store=chunk_store,
                query_embedder=query_embedder,
                reranker=reranker,
                limit=limit,
            )
        _emit_progress(progress_callback, progress_stage, "completed")
        return {
            "document_evidence": updated.document_evidence,
            "document_query_rewrite": updated.document_query_rewrite,
            "request_plan": updated.request_plan,
            "resolved_document_targets": updated.resolved_document_targets,
        }

    builder = StateGraph(AgentState)
    builder.add_node(node_name, document_retrieval_agent)
    builder.add_edge(START, node_name)
    builder.add_edge(node_name, END)

    return builder.compile()


def build_internal_document_agent(**kwargs: Any):
    return build_document_agent(INTERNAL_SCOPE, **kwargs)


def build_external_document_agent(**kwargs: Any):
    return build_document_agent(EXTERNAL_SCOPE, **kwargs)


def build_supervisor_agent(
    client: Any = None,
    model: str | None = None,
    verifier_model: str | None = None,
    document_query_planner: Any = None,
    bq_client: Any = None,
    vector_index: Any = None,
    keyword_index: Any = None,
    chunk_store: Any = None,
    query_embedder: Any = None,
    reranker: Any = None,
    document_limit: int = 10,
    progress_callback: ProgressCallback | None = None,
    include_verifier: bool = True,
):
    """Compile the top-level supervisor graph.

    Graph: plan_request → supervisor_node → synthesize_answer → verifier →
    executive-summary composer → END. Set ``include_verifier=False`` to stop
    after synthesis.

    plan_request separates requested tasks from selected knowledge sources.
    supervisor_node pages the appropriate compiled subagents and writes their
    evidence parcels into state (database_evidence, document_evidence).

    The synthesize_answer node receives the populated state and compiles a draft answer. 
    The verify_answer node checks the answer against evidence and compose_verified_answer finalizes it.

    All heavyweight dependencies (bq_client, vector_index, …) are injected so
    the graph can be constructed once at server startup and reused per request.
    """
    compiled_table_agent = build_table_agent(
        client=client,
        model=model,
        bq_client=bq_client,
        progress_callback=progress_callback,
    )
    compiled_internal_document_agent = build_internal_document_agent(
        client=client,
        model=model,
        vector_index=vector_index,
        keyword_index=keyword_index,
        chunk_store=chunk_store,
        query_embedder=query_embedder,
        reranker=reranker,
        limit=document_limit,
        progress_callback=progress_callback,
    )
    compiled_external_document_agent = build_external_document_agent(
        client=client,
        model=model,
        vector_index=vector_index,
        keyword_index=keyword_index,
        chunk_store=chunk_store,
        query_embedder=query_embedder,
        reranker=reranker,
        limit=document_limit,
        progress_callback=progress_callback,
    )
    compiled_document_query_planner = (
        document_query_planner
        if document_query_planner is not None
        else AnthropicDocumentQueryPlanner(client=client, model=model)
    )

    def plan_node(state: AgentState) -> dict:
        _emit_progress(progress_callback, "supervisor", "started")
        with agent_timing("graph.plan_request"):
            updated = plan_request(state, client=client, model=model)
        _emit_progress(progress_callback, "supervisor", "completed")
        return {
            "request_plan": updated.request_plan,
            # Scopes are derived from the plan; never honor a caller override.
            "document_scopes": updated.document_scopes,
        }

    def supervisor_node(state: AgentState) -> dict:
        sources = set(state.request_plan.knowledge_sources) if state.request_plan else set()
        needs_db = KnowledgeSource.STRUCTURED_DATA in sources
        needs_document = bool({KnowledgeSource.INTERNAL_DOCUMENTS, KnowledgeSource.EXTERNAL_LITERATURE} & sources)
        dispatch_state = state
        if needs_document and not needs_db:
            dispatch_state = _with_shared_document_rewrite(state, client=client, model=model)

        fallback_rewriter = None
        if needs_db and needs_document:
            def fallback_rewriter(raw_query: str):
                return rewrite_document_query(
                    raw_query,
                    client=client,
                    model=model,
                    timing_context={"scope": "shared"},
                )

        with agent_timing("graph.supervisor_dispatch"):
            updated = supervisor(
                dispatch_state,
                table_agent=compiled_table_agent,
                internal_document_agent=compiled_internal_document_agent,
                external_document_agent=compiled_external_document_agent,
                document_query_planner=compiled_document_query_planner,
                document_query_rewriter=fallback_rewriter,
            )
        return {
            "database_evidence": updated.database_evidence,
            "document_evidence": updated.document_evidence,
            "document_query_plan": updated.document_query_plan,
            "document_query_rewrite": updated.document_query_rewrite,
            "retrieval_queries": updated.retrieval_queries,
            "bigquery_results": updated.bigquery_results,
            "request_plan": updated.request_plan,
            "resolved_document_targets": updated.resolved_document_targets,
            "supervisor_meta": updated.supervisor_meta,
        }

    def synthesize_node(state: AgentState) -> dict:
        _emit_progress(progress_callback, "synthesize_answer", "started")
        with agent_timing("graph.synthesize_answer"):
            result = synthesize_answer(state, client=client, model=model)
        _emit_progress(progress_callback, "synthesize_answer", "completed")
        return result

    def verify_node(state: AgentState) -> dict:
        _emit_progress(progress_callback, "verify_answer", "started")
        with agent_timing("graph.verify_answer"):
            result = verify_answer(state, client=client, verifier_model=verifier_model)
        _emit_progress(progress_callback, "verify_answer", "completed")
        return result

    def compose_node(state: AgentState) -> dict:
        _emit_progress(progress_callback, "compose_verified_answer", "started")
        with agent_timing("graph.compose_verified_answer"):
            result = compose_verified_answer(state, client=client, model=model)
        _emit_progress(progress_callback, "compose_verified_answer", "completed")
        return result

    builder = StateGraph(AgentState)
    builder.add_node("plan_request", plan_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("synthesize_answer", synthesize_node)
    builder.add_edge(START, "plan_request")
    builder.add_edge("plan_request", "supervisor")
    builder.add_edge("supervisor", "synthesize_answer")
    if include_verifier:
        builder.add_node("verify_answer", verify_node)
        builder.add_node("compose_verified_answer", compose_node)
        builder.add_edge("synthesize_answer", "verify_answer")
        builder.add_conditional_edges("verify_answer", _route_after_verify)
        builder.add_edge("compose_verified_answer", END)
    else:
        builder.add_edge("synthesize_answer", END)

    return builder.compile()


def _route_after_verify(state: AgentState) -> str:
    """Retry once when needed; otherwise compose the verified user-facing summary."""
    verification = state.final_answer.verification if state.final_answer is not None else None
    if verification is not None and verification.status == WorkflowOutcome.RETRY_TRIGGERED:
        return "synthesize_answer"
    return "compose_verified_answer"


def _with_shared_document_rewrite(
    state: AgentState,
    *,
    client: Any = None,
    model: str | None = None,
) -> AgentState:
    if state.document_query_rewrite is not None:
        return state
    if state.request_plan is None or not ({KnowledgeSource.INTERNAL_DOCUMENTS, KnowledgeSource.EXTERNAL_LITERATURE} & set(state.request_plan.knowledge_sources)):
        return state

    rewrite = rewrite_document_query(
        state.query.raw_text,
        client=client,
        model=model,
        timing_context={"scope": "shared"},
    )
    return state.model_copy(update={"document_query_rewrite": rewrite})


def _emit_progress(callback: ProgressCallback | None, stage: str, status: str) -> None:
    if callback is not None:
        callback(stage, status)

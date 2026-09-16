"""Declarative module registry for the internal workflow workbench."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _port(name: str, contract: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "contract": contract, "required": required}


def _module(
    module_id: str,
    label: str,
    category: str,
    purpose: str,
    *,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    dependencies: list[str] | None = None,
    runtime_state: str = "live",
    adapter_id: str | None = None,
    configuration_fields: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "id": module_id,
        "label": label,
        "category": category,
        "purpose": purpose,
        "input_contract": inputs or [],
        "output_contract": outputs or [],
        "configuration_fields": configuration_fields or [],
        "optional_required_rules": [],
        "dependencies": dependencies or [],
        "runtime_state": runtime_state,
        "execution_adapter": adapter_id,
        "future_unwired": runtime_state == "future_unwired",
        "note": note,
    }


_MODULES = [
    _module("question", "Question / manual input", "input", "Supplies a question or typed fixture.", outputs=[_port("question", "Question")], adapter_id="question"),
    _module("request_planner", "Request planner", "routing", "Plans requested tasks, knowledge sources, and status.", inputs=[_port("question", "Question")], outputs=[_port("request_plan", "RequestPlan")], dependencies=["question"], adapter_id="request_planner"),
    _module("supervisor", "Supervisor / router", "routing", "Routes the request plan through enabled operator lanes.", inputs=[_port("request_plan", "RequestPlan", False), _port("question", "Question")], outputs=[_port("route", "OperatorRoute"), _port("routed_question", "Question")], dependencies=["request_planner"], adapter_id="supervisor"),
    _module("database_agent", "Database agent", "retrieval", "Validates conditions and queries BigQuery for database evidence.", inputs=[_port("question", "Question")], outputs=[_port("database_evidence", "DatabaseEvidence")], adapter_id="database_agent"),
    _module("db_document_query_planner", "DB-informed document-query planner", "retrieval", "Plans document searches from database results.", inputs=[_port("question", "Question"), _port("database_evidence", "DatabaseEvidence")], outputs=[_port("planned_queries", "PlannedDocumentQueries")], dependencies=["database_agent"], adapter_id="db_document_query_planner"),
    _module("document_query_rewrite", "Document query rewrite", "retrieval", "Rewrites a question for document retrieval and planner fallback.", inputs=[_port("question", "Question")], outputs=[_port("rewritten_query", "RewrittenQuestion")], adapter_id="document_query_rewrite"),
    _module("internal_document_retrieval", "Internal document retrieval", "retrieval", "Runs the current combined document agent against the internal corpus.", inputs=[_port("question", "Question", False), _port("rewritten_query", "RewrittenQuestion", False), _port("planned_queries", "PlannedDocumentQueries", False)], outputs=[_port("internal_evidence", "InternalDocumentEvidence")], adapter_id="internal_document_retrieval", configuration_fields=[{"id": "top_k", "type": "integer", "minimum": 1, "maximum": 50, "default": 10}]),
    _module("external_document_retrieval", "External document retrieval", "retrieval", "Runs the current combined document agent against the external corpus.", inputs=[_port("question", "Question", False), _port("rewritten_query", "RewrittenQuestion", False), _port("planned_queries", "PlannedDocumentQueries", False)], outputs=[_port("external_evidence", "ExternalDocumentEvidence")], adapter_id="external_document_retrieval", configuration_fields=[{"id": "top_k", "type": "integer", "minimum": 1, "maximum": 50, "default": 10}]),
    _module("internal_vector_search", "Vector search", "internal", "Enabled within the current internal document agent."),
    _module("internal_keyword_search", "Keyword search", "internal", "Enabled within the current internal document agent."),
    _module("internal_reranker", "Reranker", "internal", "Enabled within the current internal document agent."),
    _module("internal_document_evidence_builder", "Document evidence builder", "internal", "Enabled within the current internal document agent."),
    _module("external_vector_search", "Vector search", "external", "Enabled within the current external document agent."),
    _module("external_keyword_search", "Keyword search", "external", "Enabled within the current external document agent."),
    _module("external_reranker", "Reranker", "external", "Enabled within the current external document agent."),
    _module("external_document_evidence_builder", "Document evidence builder", "external", "Enabled within the current external document agent."),
    _module("evidence_packet", "Evidence packet", "evidence", "Combines enabled database and document evidence.", inputs=[_port("database_evidence", "DatabaseEvidence", False), _port("internal_evidence", "InternalDocumentEvidence", False), _port("external_evidence", "ExternalDocumentEvidence", False)], outputs=[_port("evidence_packet", "EvidencePacket")], adapter_id="evidence_packet"),
    _module("evidence_planner", "Evidence planner", "evidence", "Creates a plan from the separate normalized evidence-planning contract.", inputs=[_port("question", "Question"), _port("evidence_bundle", "NormalizedEvidenceBundle")], outputs=[_port("evidence_plan", "EvidencePlan")], runtime_state="probe_only", adapter_id="evidence_planner", note="Available in code, not connected to live workflow"),
    _module("synthesizer", "Synthesizer", "answer", "Creates a final answer from an evidence packet.", inputs=[_port("evidence_packet", "EvidencePacket")], outputs=[_port("final_answer", "FinalAnswer")], adapter_id="synthesizer"),
    _module("verifier", "Verifier", "answer", "Checks a final answer against its evidence packet.", inputs=[_port("final_answer", "FinalAnswer"), _port("evidence_packet", "EvidencePacket")], outputs=[_port("verification", "VerificationResult")], adapter_id="verifier"),
    _module("summary_composer", "Summary composer", "answer", "Creates executive prose from supported verifier claims.", inputs=[_port("final_answer", "FinalAnswer"), _port("verification", "VerificationResult")], outputs=[_port("final_answer", "FinalAnswer")], adapter_id="summary_composer"),
    _module("result", "Result / output", "output", "Displays verified or explicitly unverified output.", inputs=[_port("final_answer", "FinalAnswer", False), _port("verification", "VerificationResult", False)], outputs=[_port("result", "WorkflowResult")], adapter_id="result"),
]

# Only independently executable contracts are edges. Combined document internals
# deliberately have no edges.
def _edge(source: str, source_port: str, target: str, target_port: str, contract: str) -> dict[str, str]:
    return {"id": f"{source}.{source_port}->{target}.{target_port}", "source": source, "source_port": source_port, "target": target, "target_port": target_port, "contract": contract}


_EDGES = [
    _edge("question", "question", "request_planner", "question", "Question"),
    _edge("question", "question", "supervisor", "question", "Question"),
    _edge("request_planner", "request_plan", "supervisor", "request_plan", "RequestPlan"),
    _edge("supervisor", "routed_question", "database_agent", "question", "Question"),
    _edge("supervisor", "routed_question", "document_query_rewrite", "question", "Question"),
    _edge("database_agent", "database_evidence", "db_document_query_planner", "database_evidence", "DatabaseEvidence"),
    _edge("question", "question", "db_document_query_planner", "question", "Question"),
    _edge("document_query_rewrite", "rewritten_query", "internal_document_retrieval", "rewritten_query", "RewrittenQuestion"),
    _edge("document_query_rewrite", "rewritten_query", "external_document_retrieval", "rewritten_query", "RewrittenQuestion"),
    _edge("db_document_query_planner", "planned_queries", "internal_document_retrieval", "planned_queries", "PlannedDocumentQueries"),
    _edge("db_document_query_planner", "planned_queries", "external_document_retrieval", "planned_queries", "PlannedDocumentQueries"),
    _edge("database_agent", "database_evidence", "evidence_packet", "database_evidence", "DatabaseEvidence"),
    _edge("internal_document_retrieval", "internal_evidence", "evidence_packet", "internal_evidence", "InternalDocumentEvidence"),
    _edge("external_document_retrieval", "external_evidence", "evidence_packet", "external_evidence", "ExternalDocumentEvidence"),
    _edge("evidence_packet", "evidence_packet", "synthesizer", "evidence_packet", "EvidencePacket"),
    _edge("evidence_packet", "evidence_packet", "verifier", "evidence_packet", "EvidencePacket"),
    _edge("synthesizer", "final_answer", "verifier", "final_answer", "FinalAnswer"),
    _edge("synthesizer", "final_answer", "result", "final_answer", "FinalAnswer"),
    _edge("synthesizer", "final_answer", "summary_composer", "final_answer", "FinalAnswer"),
    _edge("verifier", "verification", "summary_composer", "verification", "VerificationResult"),
    _edge("summary_composer", "final_answer", "result", "final_answer", "FinalAnswer"),
    _edge("verifier", "verification", "result", "verification", "VerificationResult"),
]

_PRESETS = [
    {"id": "full_answer", "label": "Full answer", "description": "All retrieval lanes through verified output.", "configuration": {}, "start_node": "question", "end_node": "result", "enabled_nodes": ["question", "request_planner", "supervisor", "database_agent", "db_document_query_planner", "document_query_rewrite", "internal_document_retrieval", "external_document_retrieval", "internal_vector_search", "internal_keyword_search", "internal_reranker", "internal_document_evidence_builder", "external_vector_search", "external_keyword_search", "external_reranker", "external_document_evidence_builder", "evidence_packet", "synthesizer", "verifier", "summary_composer", "result"]},
    {"id": "database_only", "label": "Database only", "start_node": "question", "end_node": "database_agent", "enabled_nodes": ["question", "request_planner", "supervisor", "database_agent"]},
    {"id": "internal_documents_only", "label": "Internal documents only", "start_node": "question", "end_node": "internal_document_retrieval", "enabled_nodes": ["question", "request_planner", "supervisor", "document_query_rewrite", "internal_document_retrieval", "internal_vector_search", "internal_keyword_search", "internal_reranker", "internal_document_evidence_builder"]},
    {"id": "external_documents_only", "label": "External documents only", "start_node": "question", "end_node": "external_document_retrieval", "enabled_nodes": ["question", "request_planner", "supervisor", "document_query_rewrite", "external_document_retrieval", "external_vector_search", "external_keyword_search", "external_reranker", "external_document_evidence_builder"]},
    {"id": "database_external", "label": "Database + external", "start_node": "question", "end_node": "result", "enabled_nodes": ["question", "request_planner", "supervisor", "database_agent", "db_document_query_planner", "document_query_rewrite", "external_document_retrieval", "external_vector_search", "external_keyword_search", "external_reranker", "external_document_evidence_builder", "evidence_packet", "synthesizer", "verifier", "summary_composer", "result"]},
    {"id": "database_internal_external", "label": "Database + internal + external", "start_node": "question", "end_node": "evidence_packet", "enabled_nodes": ["question", "request_planner", "supervisor", "database_agent", "db_document_query_planner", "document_query_rewrite", "internal_document_retrieval", "external_document_retrieval", "internal_vector_search", "internal_keyword_search", "internal_reranker", "internal_document_evidence_builder", "external_vector_search", "external_keyword_search", "external_reranker", "external_document_evidence_builder", "evidence_packet"]},
    {"id": "evidence_packet_synthesis", "label": "Evidence packet → synthesis", "start_node": "synthesizer", "end_node": "result", "enabled_nodes": ["synthesizer", "result"]},
    {"id": "verification_only", "label": "Verification only", "start_node": "verifier", "end_node": "result", "enabled_nodes": ["verifier", "result"]},
    {"id": "custom", "label": "Custom slice", "start_node": None, "end_node": None, "enabled_nodes": []},
]


def get_registry() -> dict[str, Any]:
    presets = deepcopy(_PRESETS)
    for preset in presets:
        preset.setdefault("description", preset["label"])
        preset.setdefault("configuration", {})
    return {
        "schema_version": "1.0",
        "modules": deepcopy(_MODULES),
        "edges": deepcopy(_EDGES),
        "presets": presets,
        "selection_states": ["required/locked", "enabled", "disabled", "unavailable", "future/unwired"],
        "execution_states": ["queued", "running", "succeeded", "failed", "skipped", "manual input", "interrupted"],
    }


def module_order() -> list[str]:
    return [module["id"] for module in _MODULES]


def preset_by_id(preset_id: str) -> dict[str, Any] | None:
    return next((deepcopy(preset) for preset in _PRESETS if preset["id"] == preset_id), None)

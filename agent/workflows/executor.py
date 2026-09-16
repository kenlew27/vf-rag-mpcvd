"""Sequential slice executor around currently exposed agent boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from agent.workflows.registry import get_registry
from agent.workflows.validation import ValidationResult

Adapter = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class WorkflowExecutor:
    def __init__(self, adapters: dict[str, Adapter] | None = None) -> None:
        self.adapters = adapters or {}

    def execute(
        self,
        validation: ValidationResult,
        input_data: dict[str, Any],
        configuration: dict[str, dict[str, Any]],
        *,
        on_node: Callable[[list[dict[str, Any]], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        modules = {module["id"]: module for module in get_registry()["modules"]}
        artifact = json.loads(json.dumps(input_data, default=str))
        artifact["_enabled_nodes"] = list(validation.enabled_nodes)
        nodes: list[dict[str, Any]] = []
        for node_id in validation.selected_path:
            started = time.monotonic()
            request_data = json.loads(json.dumps(artifact, default=str))
            node = {
                "node_id": node_id,
                "attempt": 1,
                "status": "running",
                "input_origin": "manual" if node_id == validation.start_node else "upstream",
                "request": request_data,
                "response": None,
                "duration_ms": None,
                "started_at": _timestamp(),
                "finished_at": None,
                "error": None,
                "output_contract": modules[node_id]["output_contract"],
                "output_contracts": [],
                "artifact_hash": None,
            }
            nodes.append(node)
            if on_node:
                on_node(nodes, artifact)
            try:
                if (
                    node_id == validation.start_node
                    and (
                        node_id == "question"
                        or artifact.get("_prior_source_node") == node_id
                    )
                ):
                    output = artifact
                    node["status"] = "manual input"
                else:
                    adapter = self.adapters.get(node_id) or getattr(self, f"_run_{node_id}")
                    output = adapter(artifact, configuration.get(node_id, {}))
                    artifact = output
                    node["status"] = "succeeded"
                node["response"] = json.loads(json.dumps(output, default=str))
                node["artifact_hash"] = _artifact_hash(node["response"])
                node["output_contracts"] = _artifact_contracts(node["response"])
            except Exception as exc:
                node["status"] = "failed"
                node["error"] = {"type": exc.__class__.__name__, "message": _safe_message(exc)}
                node["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                node["finished_at"] = _timestamp()
                if on_node:
                    on_node(nodes, artifact)
                return {"status": "failed", "nodes": nodes, "artifact": artifact, "error": node["error"]}
            node["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            node["finished_at"] = _timestamp()
            if on_node:
                on_node(nodes, artifact)
            if (
                node_id == "verifier"
                and _verification_status(artifact) == "retry_triggered"
                and "synthesizer" in validation.selected_path
            ):
                for retry_node_id in ("synthesizer", "verifier"):
                    retry_started = time.monotonic()
                    retry = {
                        "node_id": retry_node_id,
                        "attempt": 2,
                        "status": "running",
                        "input_origin": "upstream",
                        "request": json.loads(json.dumps(artifact, default=str)),
                        "response": None,
                        "duration_ms": None,
                        "started_at": _timestamp(),
                        "finished_at": None,
                        "error": None,
                        "output_contract": modules[retry_node_id]["output_contract"],
                        "output_contracts": [],
                        "artifact_hash": None,
                    }
                    nodes.append(retry)
                    if on_node:
                        on_node(nodes, artifact)
                    try:
                        retry_adapter = self.adapters.get(retry_node_id) or getattr(self, f"_run_{retry_node_id}")
                        artifact = retry_adapter(artifact, configuration.get(retry_node_id, {}))
                        retry["status"] = "succeeded"
                        retry["response"] = json.loads(json.dumps(artifact, default=str))
                        retry["artifact_hash"] = _artifact_hash(retry["response"])
                        retry["output_contracts"] = _artifact_contracts(retry["response"])
                    except Exception as exc:
                        retry["status"] = "failed"
                        retry["error"] = {"type": exc.__class__.__name__, "message": _safe_message(exc)}
                        retry["duration_ms"] = round((time.monotonic() - retry_started) * 1000, 3)
                        retry["finished_at"] = _timestamp()
                        return {"status": "failed", "nodes": nodes, "artifact": artifact, "error": retry["error"]}
                    retry["duration_ms"] = round((time.monotonic() - retry_started) * 1000, 3)
                    retry["finished_at"] = _timestamp()
                    if on_node:
                        on_node(nodes, artifact)
        return {"status": "succeeded", "nodes": nodes, "artifact": artifact, "error": None}

    def _state(self, artifact: dict[str, Any]):
        from agent.schemas import FinalAnswer
        from agent.request_plan import RequestPlan
        from agent.state import AgentState, UserQuery

        update: dict[str, Any] = {
            "query": UserQuery(raw_text=str(artifact.get("question") or "")),
            "database_evidence": artifact.get("database_evidence", {}),
            "bigquery_results": artifact.get("bigquery_results", []),
            "document_evidence": artifact.get("document_evidence", {}),
            "evidence_packet": artifact.get("evidence_packet"),
            "supervisor_meta": artifact.get("supervisor_meta", {}),
            "synthesis_retry_count": artifact.get("synthesis_retry_count", 0),
            "retrieval_queries": artifact.get("retrieval_queries") or artifact.get("planned_queries", []),
        }
        if artifact.get("request_plan"):
            update["request_plan"] = RequestPlan.model_validate(artifact["request_plan"])
        rewrite_data = artifact.get("document_query_rewrite") or artifact.get("rewritten_query")
        if rewrite_data:
            from agent.state import DocumentQueryRewrite
            if isinstance(rewrite_data, str):
                rewrite_data = {"rewritten_query": rewrite_data, "rewrite_notes": ["manually supplied"]}
            update["document_query_rewrite"] = DocumentQueryRewrite.model_validate(rewrite_data)
        if artifact.get("document_query_plan"):
            from agent.schemas import DocumentQueryPlan
            update["document_query_plan"] = DocumentQueryPlan.model_validate(artifact["document_query_plan"])
        if artifact.get("final_answer"):
            final_answer = dict(artifact["final_answer"])
            if artifact.get("verification") and not final_answer.get("verification"):
                final_answer["verification"] = artifact["verification"]
            update["final_answer"] = FinalAnswer.model_validate(final_answer)
        return AgentState(**update)

    def _run_question(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        return artifact

    def _run_request_planner(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.nodes.plan_request import plan_request

        state = plan_request(self._state(artifact), model=os.environ.get("ANTHROPIC_MODEL"))
        request_plan = state.request_plan.model_dump(mode="json")
        return {**artifact, "request_plan": request_plan, "routing": request_plan}

    def _run_supervisor(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        selected = [node for node in ("database_agent", "internal_document_retrieval", "external_document_retrieval") if node in artifact.get("_enabled_nodes", [])]
        route = {"request_plan": artifact.get("request_plan"), "operator_selected_lanes": selected, "operator_override": True}
        return {**artifact, "routing": route, "route": route, "routed_question": artifact.get("question")}

    def _run_database_agent(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.graph import build_table_agent

        result = build_table_agent(model=os.environ.get("ANTHROPIC_MODEL")).invoke(self._state(artifact))
        return {**artifact, "database_evidence": result.get("database_evidence", {}), "bigquery_results": result.get("bigquery_results", [])}

    def _run_document_query_rewrite(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.nodes.retrieve_document import rewrite_document_query

        rewrite = rewrite_document_query(str(artifact.get("question") or ""), model=os.environ.get("ANTHROPIC_MODEL"))
        rewrite_data = rewrite.model_dump(mode="json")
        return {**artifact, "document_query_rewrite": rewrite_data, "rewritten_query": rewrite_data, "document_query": rewrite.rewritten_query}

    def _run_db_document_query_planner(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.document_query_planner import AnthropicDocumentQueryPlanner
        from agent.schemas import DocumentQueryPlanningRequest

        try:
            request = DocumentQueryPlanningRequest.model_validate({
                "question": artifact.get("question"),
                "planner_table": (artifact.get("database_evidence") or {}).get("planner_table"),
                "database_limitations": (artifact.get("database_evidence") or {}).get("limitations", []),
            })
            plan = AnthropicDocumentQueryPlanner(model=os.environ.get("ANTHROPIC_MODEL")).plan(request)
        except Exception as exc:
            return {**artifact, "document_query_plan": None, "planned_queries": [], "retrieval_queries": [], "planner_warning": f"DB-informed planning failed ({exc.__class__.__name__}); normal rewrite fallback will be used."}
        queries = [query.query for query in plan.queries]
        return {**artifact, "document_query_plan": plan.model_dump(mode="json"), "planned_queries": queries, "retrieval_queries": queries}

    def _run_internal_document_retrieval(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        return self._run_document_scope("internal", artifact, config)

    def _run_external_document_retrieval(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        return self._run_document_scope("external", artifact, config)

    def _run_document_scope(self, scope: str, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.graph import build_document_agent
        from agent.request_plan import KnowledgeSource, RequestPlan, RequestStatus, RequestTask

        source = KnowledgeSource.INTERNAL_DOCUMENTS if scope == "internal" else KnowledgeSource.EXTERNAL_LITERATURE
        request_plan = RequestPlan(
            tasks=[RequestTask.LOOKUP],
            knowledge_sources=[source],
            status=RequestStatus.READY,
        )
        state = self._state({**artifact, "request_plan": request_plan.model_dump(mode="json")})
        result = build_document_agent(scope, model=os.environ.get("ANTHROPIC_MODEL"), limit=int(config.get("top_k", 10))).invoke(state)
        evidence = result.get("document_evidence", {}).get(scope, {})
        document_evidence = dict(artifact.get("document_evidence", {}))
        document_evidence[scope] = evidence
        key = "internal_evidence" if scope == "internal" else "external_evidence"
        return {**artifact, key: evidence, "document_evidence": document_evidence, "document_query_rewrite": _dump(result.get("document_query_rewrite"))}

    def _run_evidence_packet(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        if artifact.get("evidence_packet") and not (artifact.get("database_evidence") or artifact.get("document_evidence")):
            return artifact
        from agent.nodes.synthesize_answer import _build_packet

        document_evidence = dict(artifact.get("document_evidence", {}))
        if artifact.get("internal_evidence"):
            document_evidence["internal"] = artifact["internal_evidence"]
        if artifact.get("external_evidence"):
            document_evidence["external"] = artifact["external_evidence"]
        state = self._state({**artifact, "document_evidence": document_evidence})
        packet = _build_packet(state)
        return {**artifact, "evidence_packet": packet.model_dump(mode="json")}

    def _run_evidence_planner(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.synthesis.models import EvidencePlanningRequest
        from agent.synthesis.normalization import normalize_evidence_packets
        from agent.synthesis.planner import AnthropicEvidencePlanner

        if artifact.get("evidence_bundle"):
            from agent.synthesis.models import NormalizedEvidenceBundle
            bundle = NormalizedEvidenceBundle.model_validate(artifact["evidence_bundle"])
        else:
            bundle = normalize_evidence_packets(
                artifact.get("document_evidence"),
                artifact.get("database_evidence"),
                question=artifact.get("question"),
            )
        plan = AnthropicEvidencePlanner().plan(EvidencePlanningRequest(question=str(artifact.get("question") or ""), evidence_bundle=bundle))
        return {**artifact, "evidence_plan": plan.model_dump(mode="json")}

    def _run_synthesizer(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.nodes.synthesize_answer import synthesize_answer

        result = synthesize_answer(self._state(artifact), model=os.environ.get("ANTHROPIC_MODEL"))
        return {**artifact, "evidence_packet": result["evidence_packet"], "final_answer": result["final_answer"].model_dump(mode="json")}

    def _run_verifier(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.nodes.verify_answer import verify_answer

        result = verify_answer(self._state(artifact))
        output = {**artifact, **{key: _dump(value) for key, value in result.items()}}
        answer = output.get("final_answer")
        if isinstance(answer, dict):
            output["verification"] = answer.get("verification")
        return output

    def _run_summary_composer(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from agent.nodes.compose_verified_answer import compose_verified_answer

        result = compose_verified_answer(self._state(artifact))
        if not result:
            return artifact
        return {**artifact, "final_answer": result["final_answer"].model_dump(mode="json")}

    def _run_result(self, artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        answer = artifact.get("final_answer")
        verification = answer.get("verification") if isinstance(answer, dict) else None
        verification = verification or artifact.get("verification")
        verified = bool(
            verification
            and verification.get("status") in {"passed", "passed_after_retry"}
            and verification.get("action") == "allow"
        )
        partial = bool(
            verification
            and verification.get("status") == "partial_after_retry"
        )
        result = {
            "final_answer": answer,
            "verification": verification,
            "verified": verified,
            "label": "verified" if verified else "partially verified" if partial else "unverified",
        }
        return {**artifact, "verified": verified, "result_label": result["label"], "result": result}


def _artifact_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _verification_status(artifact: dict[str, Any]) -> str | None:
    answer = artifact.get("final_answer")
    verification = answer.get("verification") if isinstance(answer, dict) else None
    if not isinstance(verification, dict):
        verification = artifact.get("verification")
    return verification.get("status") if isinstance(verification, dict) else None


def _artifact_contracts(value: dict[str, Any]) -> list[str]:
    mapping = {
        "question": "Question",
        "request_plan": "RequestPlan",
        "routing": "RequestPlan",
        "route": "OperatorRoute",
        "routed_question": "Question",
        "database_evidence": "DatabaseEvidence",
        "rewritten_query": "RewrittenQuestion",
        "planned_queries": "PlannedDocumentQueries",
        "internal_evidence": "InternalDocumentEvidence",
        "external_evidence": "ExternalDocumentEvidence",
        "evidence_packet": "EvidencePacket",
        "evidence_bundle": "NormalizedEvidenceBundle",
        "evidence_plan": "EvidencePlan",
        "final_answer": "FinalAnswer",
        "verification": "VerificationResult",
        "result": "WorkflowResult",
    }
    return sorted({contract for key, contract in mapping.items() if key in value})


def _safe_message(exc: Exception) -> str:
    return f"Workflow component failed ({exc.__class__.__name__}). See backend logs for details."


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _timestamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

"""Backend-authoritative workflow selection validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.workflows.registry import get_registry, module_order, preset_by_id


class InputSource(BaseModel):
    kind: Literal["manual", "fixture", "prior_run"] = "manual"
    data: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    node_id: str | None = None
    artifact_hash: str | None = None


class WorkflowSelection(BaseModel):
    preset_id: str = "custom"
    start_node: str | None = None
    end_node: str | None = None
    enabled_nodes: list[str] | None = None
    configuration: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_source: InputSource = Field(default_factory=InputSource)


class ValidationIssue(BaseModel):
    code: str
    node_id: str | None = None
    input_port: str | None = None
    reason: str


class NodeSelectionState(BaseModel):
    state: Literal["required/locked", "enabled", "disabled", "unavailable", "future/unwired"]
    reason: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    preset_id: str
    start_node: str | None
    end_node: str | None
    enabled_nodes: list[str]
    selected_path: list[str]
    node_states: dict[str, NodeSelectionState]
    cascade_effects: list[dict[str, str]]
    missing_inputs: list[ValidationIssue]
    issues: list[ValidationIssue]
    recipe_hash: str


def validate_selection(selection: WorkflowSelection) -> ValidationResult:
    registry = get_registry()
    modules = {module["id"]: module for module in registry["modules"]}
    preset = preset_by_id(selection.preset_id)
    issues: list[ValidationIssue] = []
    cascades: list[dict[str, str]] = []

    if preset is None:
        issues.append(ValidationIssue(code="unknown_preset", reason=f"Unknown preset: {selection.preset_id}"))
        preset = preset_by_id("custom")

    start = selection.start_node or preset["start_node"]
    end = selection.end_node or preset["end_node"]
    requested = list(selection.enabled_nodes if selection.enabled_nodes is not None else preset["enabled_nodes"])
    enabled = {node for node in requested if node in modules}
    for node in requested:
        if node not in modules:
            issues.append(ValidationIssue(code="unknown_node", node_id=node, reason=f"Unknown workflow node: {node}"))

    states = {node_id: NodeSelectionState(state="disabled") for node_id in modules}
    for node_id in enabled:
        states[node_id] = NodeSelectionState(state="enabled")

    for node_id, module in modules.items():
        if module["runtime_state"] == "future_unwired":
            states[node_id] = NodeSelectionState(state="future/unwired", reason=module["note"])
            if node_id in enabled or node_id in (start, end):
                enabled.discard(node_id)
                issues.append(ValidationIssue(code="future_unwired", node_id=node_id, reason=module["note"]))

    doc_nodes = {"internal_document_retrieval", "external_document_retrieval"}
    if "db_document_query_planner" in enabled and (
        "database_agent" not in enabled or not (enabled & doc_nodes)
    ):
        reason = "Requires database and at least one document source."
        enabled.remove("db_document_query_planner")
        states["db_document_query_planner"] = NodeSelectionState(state="disabled", reason=reason)
        cascades.append({"node_id": "db_document_query_planner", "from": "enabled", "to": "disabled", "reason": reason})

    data = selection.input_source.data
    supplied_packet = isinstance(data.get("evidence_packet"), dict)
    supplied_parcels = any(data.get(key) for key in ("database_evidence", "internal_evidence", "external_evidence", "document_evidence"))
    supplied_answer = isinstance(data.get("final_answer"), dict)
    question_seeded = start == "question"

    if question_seeded and enabled & doc_nodes and "document_query_rewrite" not in enabled:
        reason = "Question-seeded document retrieval requires Document query rewrite for normal and planner-fallback queries."
        for node_id in sorted(enabled & doc_nodes):
            _cascade(enabled, states, cascades, node_id, reason)
        if "db_document_query_planner" in enabled and not (enabled & doc_nodes):
            _cascade(enabled, states, cascades, "db_document_query_planner", "Requires database and at least one enabled document source.")

    retrieval_enabled = bool(enabled & (doc_nodes | {"database_agent"}))
    if "evidence_packet" in enabled and not retrieval_enabled and not supplied_packet and not supplied_parcels:
        _cascade(enabled, states, cascades, "evidence_packet", "Requires enabled database/document evidence or a supplied packet.")
        _cascade(enabled, states, cascades, "synthesizer", "Evidence packet was disabled by an upstream dependency.")

    if "synthesizer" in enabled and "evidence_packet" not in enabled and not supplied_packet:
        _cascade(enabled, states, cascades, "synthesizer", "Requires an enabled Evidence packet or a manually supplied packet.")
    if "verifier" in enabled and "synthesizer" not in enabled and not (supplied_answer and supplied_packet):
        _cascade(enabled, states, cascades, "verifier", "Requires synthesis or manually supplied final_answer and evidence_packet.")
    if "verifier" in enabled and "synthesizer" not in enabled and supplied_answer and not supplied_packet:
        _cascade(enabled, states, cascades, "verifier", "Verification requires a manually supplied evidence_packet.")
    if "result" in enabled and "synthesizer" not in enabled and "verifier" not in enabled:
        issues.append(ValidationIssue(code="missing_input", node_id="result", input_port="result", reason="Result requires synthesis or verification output."))

    if "evidence_planner" in enabled:
        states["evidence_planner"] = NodeSelectionState(state="enabled", reason="Available in code, not connected to live workflow.")
        if start != "evidence_planner" or end != "evidence_planner":
            issues.append(ValidationIssue(code="probe_only", node_id="evidence_planner", reason="Evidence planner is a standalone probe and cannot continue into the production synthesizer."))

    if start not in modules:
        issues.append(ValidationIssue(code="invalid_start", node_id=start, reason="Select a valid start node."))
    elif start not in enabled:
        issues.append(ValidationIssue(code="invalid_start", node_id=start, reason="The selected start node is disabled or unavailable."))
    if end not in modules:
        issues.append(ValidationIssue(code="invalid_end", node_id=end, reason="Select a valid end node."))
    elif end not in enabled:
        issues.append(ValidationIssue(code="invalid_end", node_id=end, reason="The selected end node is disabled or unavailable after dependency cascading."))

    issues.extend(_start_input_issues(start, data))
    issues.extend(_fixture_schema_issues(start, data))

    top_k_issue = _validate_configuration(selection.configuration)
    issues.extend(top_k_issue)

    ordered = [node for node in module_order() if node in enabled]
    path = _contract_path(start, end, enabled, registry["edges"], ordered)
    if start in enabled and end in enabled and not path:
        issues.append(ValidationIssue(code="invalid_path", node_id=end, reason="No enabled contract path connects the selected start and end nodes."))

    for node in (start, end):
        if node in enabled and states[node].state == "enabled":
            states[node] = NodeSelectionState(state="required/locked")

    normalized_config = {
        node: dict(sorted(config.items()))
        for node, config in sorted(selection.configuration.items())
        if node in modules
    }
    recipe_hash = hashlib.sha256(
        json.dumps(
            {"preset_id": selection.preset_id, "start_node": start, "end_node": end, "enabled_nodes": sorted(enabled), "configuration": normalized_config},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()[:12]
    missing = [issue for issue in issues if issue.code == "missing_input"]
    return ValidationResult(
        valid=not issues,
        preset_id=selection.preset_id,
        start_node=start,
        end_node=end,
        enabled_nodes=ordered,
        selected_path=path,
        node_states=states,
        cascade_effects=cascades,
        missing_inputs=missing,
        issues=issues,
        recipe_hash=recipe_hash,
    )


def _cascade(
    enabled: set[str],
    states: dict[str, NodeSelectionState],
    cascades: list[dict[str, str]],
    node_id: str,
    reason: str,
) -> None:
    if node_id not in enabled:
        return
    enabled.remove(node_id)
    states[node_id] = NodeSelectionState(state="disabled", reason=reason)
    cascades.append({"node_id": node_id, "from": "enabled", "to": "disabled", "reason": reason})
    if node_id == "synthesizer":
        _cascade(enabled, states, cascades, "verifier", "Synthesizer was disabled by an upstream dependency.")


def _validate_configuration(configuration: dict[str, dict[str, Any]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    allowed = {
        "internal_document_retrieval": {"top_k"},
        "external_document_retrieval": {"top_k"},
    }
    for node_id, fields in configuration.items():
        if node_id not in allowed:
            issues.append(ValidationIssue(code="invalid_configuration", node_id=node_id, reason="This node has no operator-configurable fields."))
            continue
        for field in fields:
            if field not in allowed[node_id]:
                issues.append(ValidationIssue(code="invalid_configuration", node_id=node_id, input_port=field, reason="Unknown configuration field."))
    for node_id in ("internal_document_retrieval", "external_document_retrieval"):
        value = configuration.get(node_id, {}).get("top_k", 10)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
            issues.append(ValidationIssue(code="invalid_configuration", node_id=node_id, input_port="top_k", reason="top_k must be an integer from 1 to 50."))
    return issues


def _start_input_issues(start: str | None, data: dict[str, Any]) -> list[ValidationIssue]:
    requirements: dict[str, list[tuple[str, str]]] = {
        "question": [("question", "A non-empty question is required.")],
        "request_planner": [("question", "Request planner requires a question.")],
        "supervisor": [("question", "Supervisor probe requires a question.")],
        "database_agent": [("question", "Database probe requires a question.")],
        "db_document_query_planner": [
            ("question", "DB-informed planner requires a question."),
            ("database_evidence", "DB-informed planner requires database evidence."),
        ],
        "synthesizer": [("evidence_packet", "Synthesizer requires an evidence packet.")],
        "verifier": [
            ("final_answer", "Verifier requires a final answer."),
            ("evidence_packet", "Verifier requires an evidence packet."),
        ],
        "summary_composer": [
            ("final_answer", "Summary composer requires a final answer."),
            ("verification", "Summary composer requires verification results."),
        ],
    }
    issues: list[ValidationIssue] = []
    if start in {"internal_document_retrieval", "external_document_retrieval"}:
        if not str(data.get("question", "")).strip() and not any(data.get(key) for key in ("rewritten_query", "planned_queries", "document_query")):
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port="rewritten_query", reason="Document probe requires a question, rewritten_query, or planned_queries."))
        return issues
    if start == "evidence_packet":
        if not data.get("evidence_packet") and not any(data.get(key) for key in ("database_evidence", "internal_evidence", "external_evidence", "document_evidence")):
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port="evidence", reason="Evidence-packet probe requires database or document parcels."))
        return issues
    if start == "evidence_planner":
        if not str(data.get("question", "")).strip():
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port="question", reason="Evidence planner requires a question."))
        if not data.get("evidence_bundle") and not any(data.get(key) for key in ("database_evidence", "document_evidence")):
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port="evidence_bundle", reason="Evidence planner requires a NormalizedEvidenceBundle or database/document parcels to normalize."))
        return issues
    if start == "result":
        if not data.get("final_answer") and not data.get("verification"):
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port="result", reason="Result requires final_answer or verification input."))
        return issues
    for port, reason in requirements.get(start or "", []):
        value = data.get(port)
        if value is None or (port == "question" and not str(value).strip()):
            issues.append(ValidationIssue(code="missing_input", node_id=start, input_port=port, reason=reason))
    return issues


def _contract_path(
    start: str | None,
    end: str | None,
    enabled: set[str],
    edges: list[dict[str, Any]],
    ordered: list[str],
) -> list[str]:
    if start not in enabled or end not in enabled:
        return []
    outgoing: dict[str, set[str]] = {}
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        if edge["source"] in enabled and edge["target"] in enabled:
            outgoing.setdefault(edge["source"], set()).add(edge["target"])
            incoming.setdefault(edge["target"], set()).add(edge["source"])

    def visit(seed: str, graph: dict[str, set[str]]) -> set[str]:
        seen = {seed}
        stack = [seed]
        while stack:
            stack.extend(node for node in graph.get(stack.pop(), ()) if node not in seen and not seen.add(node))
        return seen

    downstream = visit(start, outgoing)
    if end not in downstream:
        return []
    upstream = visit(end, incoming)
    return [node for node in ordered if node in downstream and node in upstream]


def _fixture_schema_issues(start: str | None, data: dict[str, Any]) -> list[ValidationIssue]:
    validators: list[tuple[str, Any]] = []
    if data.get("evidence_packet") is not None and start in {"synthesizer", "verifier"}:
        from agent.schemas import EvidencePacket
        validators.append(("evidence_packet", EvidencePacket))
    if data.get("final_answer") is not None and start in {"verifier", "summary_composer"}:
        from agent.schemas import FinalAnswer
        validators.append(("final_answer", FinalAnswer))
    if data.get("evidence_bundle") is not None and start == "evidence_planner":
        from agent.synthesis.models import NormalizedEvidenceBundle
        validators.append(("evidence_bundle", NormalizedEvidenceBundle))
    issues = []
    for port, contract in validators:
        try:
            contract.model_validate(data[port])
        except Exception:
            issues.append(ValidationIssue(code="invalid_fixture", node_id=start, input_port=port, reason=f"{port} does not match the required {contract.__name__} contract."))
    return issues

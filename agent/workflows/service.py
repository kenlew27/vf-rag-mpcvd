"""Queueing workflow service with persisted immutable snapshots."""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from agent.workflows.executor import WorkflowExecutor
from agent.workflows.registry import get_registry
from agent.workflows.store import WorkflowRunStore
from agent.workflows.validation import ValidationIssue, WorkflowSelection, validate_selection

MAX_FIXTURE_BYTES = 2 * 1024 * 1024


class CreateWorkflowRunRequest(WorkflowSelection):
    label: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class WorkflowValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        super().__init__("Invalid workflow selection")
        self.issues = issues


class WorkflowRunService:
    def __init__(self, store: WorkflowRunStore, executor: WorkflowExecutor | None = None) -> None:
        self.store = store
        self.executor = executor or WorkflowExecutor()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="workflow-workbench")
        self._thread.start()

    def validate(self, selection: WorkflowSelection):
        resolved = self._resolve_prior_input(selection)
        _check_fixture_size(resolved)
        return validate_selection(resolved)

    def create(self, request: CreateWorkflowRunRequest) -> dict[str, Any]:
        selection = WorkflowSelection.model_validate(request.model_dump(exclude={"label", "note"}))
        selection = self._resolve_prior_input(selection)
        _check_fixture_size(selection)
        validation = validate_selection(selection)
        if not validation.valid:
            raise WorkflowValidationError(validation.issues)

        now = _now()
        run_id = str(uuid4())
        source = selection.input_source
        lineage = None
        if source.kind == "prior_run":
            lineage = {
                "parent_run_id": source.run_id,
                "source_node_id": source.node_id,
                "artifact_hash": source.artifact_hash,
            }
        modules = {module["id"]: module for module in get_registry()["modules"]}
        presets = {preset["id"]: preset for preset in get_registry()["presets"]}
        snapshot = {
            "run_id": run_id,
            "status": "queued",
            "label": request.label,
            "note": request.note,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "recipe": {
                "preset_id": validation.preset_id,
                "name": presets.get(validation.preset_id, {}).get("label", "Custom slice"),
                "start_node": validation.start_node,
                "end_node": validation.end_node,
                "enabled_nodes": validation.enabled_nodes,
                "selected_path": validation.selected_path,
                "configuration": selection.configuration,
                "recipe_hash": validation.recipe_hash,
            },
            "lineage": lineage,
            "parent_run_id": lineage["parent_run_id"] if lineage else None,
            "source_node_id": lineage["source_node_id"] if lineage else None,
            "source_artifact_hash": lineage["artifact_hash"] if lineage else None,
            "input_source": source.model_dump(mode="json"),
            "nodes": [
                {
                    "node_id": node_id,
                    "status": "queued" if node_id in validation.selected_path else "skipped",
                    "input_origin": "manual" if node_id == validation.start_node else "upstream",
                    "request": None,
                    "response": None,
                    "duration_ms": None,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "output_contract": modules[node_id]["output_contract"],
                    "output_contracts": [port["contract"] for port in modules[node_id]["output_contract"]],
                    "artifact_hash": None,
                }
                for node_id in modules
            ],
            "result": None,
            "error": None,
        }
        self.store.create(snapshot)
        self._queue.put(run_id)
        return snapshot

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get(run_id)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list(limit=limit)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2)

    def _resolve_prior_input(self, selection: WorkflowSelection) -> WorkflowSelection:
        source = selection.input_source
        if source.kind != "prior_run":
            return selection
        if not source.run_id or not source.node_id or not source.artifact_hash:
            raise WorkflowValidationError([ValidationIssue(code="invalid_prior_artifact", reason="Prior-run input requires run_id, node_id, and artifact_hash.")])
        parent = self.store.get(source.run_id)
        if parent is None:
            raise WorkflowValidationError([ValidationIssue(code="unknown_prior_run", reason="Prior workflow run was not found.")])
        node = next((item for item in parent.get("nodes", []) if item.get("node_id") == source.node_id), None)
        if not node or node.get("status") not in {"succeeded", "manual input"}:
            raise WorkflowValidationError([ValidationIssue(code="invalid_prior_artifact", node_id=source.node_id, reason="Selected prior node has no successful artifact.")])
        if node.get("artifact_hash") != source.artifact_hash:
            raise WorkflowValidationError([ValidationIssue(code="artifact_hash_mismatch", node_id=source.node_id, reason="Prior artifact hash does not match the immutable saved output.")])
        registry = get_registry()
        modules = {module["id"]: module for module in registry["modules"]}
        preset = next(
            (
                item
                for item in registry["presets"]
                if item["id"] == selection.preset_id
            ),
            None,
        )
        start_node = selection.start_node or (preset or {}).get("start_node")
        start_module = modules.get(start_node or "")
        produced = set(node.get("output_contracts") or [])
        input_ports = (start_module or {}).get("input_contract", [])
        accepted = {port["contract"] for port in input_ports}
        required = {
            port["contract"] for port in input_ports if port.get("required", True)
        }
        incompatible = (
            bool(required and not required.issubset(produced))
            or bool(not required and accepted and produced.isdisjoint(accepted))
        )
        if incompatible:
            raise WorkflowValidationError([ValidationIssue(code="incompatible_prior_artifact", node_id=source.node_id, reason="Prior artifact output contracts are incompatible with the selected start node.")])
        copied = json.loads(json.dumps(node.get("response") or {}))
        copied["_prior_source_node"] = source.node_id
        return selection.model_copy(update={"input_source": source.model_copy(update={"data": copied})})

    def _worker(self) -> None:
        while True:
            run_id = self._queue.get()
            if run_id is None:
                return
            try:
                self._execute_run(run_id)
            except Exception as exc:
                current = self.store.get(run_id)
                if current is not None and current["status"] not in {"succeeded", "failed", "interrupted"}:
                    current["status"] = "failed"
                    current["error"] = {
                        "type": exc.__class__.__name__,
                        "message": f"Workflow worker failed ({exc.__class__.__name__}). See backend logs for details.",
                    }
                    current["completed_at"] = _now()
                    current["updated_at"] = current["completed_at"]
                    for node in current.get("nodes", []):
                        if node.get("status") in {"queued", "running"}:
                            node["status"] = "skipped"
                    self.store.update(run_id, current)

    def _execute_run(self, run_id: str) -> None:
        snapshot = self.store.get(run_id)
        if snapshot is None or snapshot["status"] != "queued":
            return
        snapshot["status"] = "running"
        snapshot["started_at"] = _now()
        snapshot["updated_at"] = snapshot["started_at"]
        self.store.update(run_id, snapshot)
        recipe = snapshot["recipe"]
        selection = WorkflowSelection(
            preset_id=recipe["preset_id"],
            start_node=recipe["start_node"],
            end_node=recipe["end_node"],
            enabled_nodes=recipe["enabled_nodes"],
            configuration=recipe["configuration"],
            input_source=snapshot["input_source"],
        )
        validation = validate_selection(selection)

        def progress(nodes: list[dict[str, Any]], artifact: dict[str, Any]) -> None:
            current = self.store.get(run_id)
            if current is None or current["status"] != "running":
                return
            completed_ids = {node["node_id"] for node in nodes}
            queued = [node for node in current["nodes"] if node["node_id"] not in completed_ids]
            current["nodes"] = json.loads(json.dumps(nodes, default=str)) + queued
            current["updated_at"] = _now()
            self.store.update(run_id, current)

        result = self.executor.execute(
            validation,
            selection.input_source.data,
            selection.configuration,
            on_node=progress,
        )
        current = self.store.get(run_id)
        if current is None or current["status"] != "running":
            return
        current["status"] = result["status"]
        executed_ids = {node["node_id"] for node in result["nodes"]}
        current["nodes"] = result["nodes"] + [
            node for node in current["nodes"] if node["node_id"] not in executed_ids
        ]
        if result["status"] == "failed":
            for node in current["nodes"]:
                if node.get("status") == "queued":
                    node["status"] = "skipped"
        current["result"] = result["artifact"]
        current["error"] = result["error"]
        current["completed_at"] = _now()
        current["updated_at"] = current["completed_at"]
        self.store.update(run_id, current)


def _check_fixture_size(selection: WorkflowSelection) -> None:
    if selection.input_source.kind != "fixture":
        return
    size = len(json.dumps(selection.input_source.data, separators=(",", ":"), default=str).encode())
    if size > MAX_FIXTURE_BYTES:
        raise WorkflowValidationError([ValidationIssue(code="fixture_too_large", reason="Fixture JSON must not exceed 2 MiB.")])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

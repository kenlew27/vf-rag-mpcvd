"""Internal workflow workbench API."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException, Query

from agent.workflows.registry import get_registry
from agent.workflows.service import CreateWorkflowRunRequest, WorkflowRunService, WorkflowValidationError
from agent.workflows.store import WorkflowRunStore
from agent.workflows.validation import WorkflowSelection

router = APIRouter(prefix="/agent/workflows", tags=["workflow-workbench"])
_service_instance: WorkflowRunService | None = None
_service_lock = Lock()


def _service() -> WorkflowRunService:
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = WorkflowRunService(
                    WorkflowRunStore(Path("data") / "workflow_runs.sqlite3")
                )
    return _service_instance


def close_workflow_service() -> None:
    """Stop the worker if the workbench was used during this process."""
    global _service_instance
    with _service_lock:
        if _service_instance is not None:
            _service_instance.close()
            _service_instance = None


@router.get("/registry")
def registry():
    return get_registry()


@router.post("/validate")
def validate_workflow(selection: WorkflowSelection):
    try:
        return _service().validate(selection)
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=422, detail={"issues": [issue.model_dump() for issue in exc.issues]}) from exc


@router.post("/runs", status_code=202)
def create_run(request: CreateWorkflowRunRequest):
    try:
        return _service().create(request)
    except WorkflowValidationError as exc:
        raise HTTPException(status_code=422, detail={"issues": [issue.model_dump() for issue in exc.issues]}) from exc


@router.get("/runs")
def list_runs(limit: int = Query(default=100, ge=1, le=500)):
    return {"runs": _service().list(limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    snapshot = _service().get(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return snapshot

"""SQLite persistence for immutable workflow run snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}


class WorkflowRunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()
        self.mark_incomplete_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
                """
            )

    def create(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = _json(snapshot)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO workflow_runs (run_id, status, created_at, updated_at, snapshot_json) VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot["run_id"],
                    snapshot["status"],
                    snapshot["created_at"],
                    snapshot.get("updated_at", snapshot["created_at"]),
                    payload,
                ),
            )
        return snapshot

    def update(self, run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in TERMINAL_STATUSES:
                raise ValueError("terminal workflow run snapshots are immutable")
            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, snapshot_json = ? WHERE run_id = ?",
                (
                    snapshot["status"],
                    snapshot.get("updated_at", snapshot.get("created_at", "")),
                    _json(snapshot),
                    run_id,
                ),
            )
        return snapshot

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM workflow_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(row["snapshot_json"]) for row in rows]

    def mark_incomplete_interrupted(self) -> None:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, snapshot_json FROM workflow_runs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                snapshot = json.loads(row["snapshot_json"])
                interrupted_at = datetime.now(timezone.utc).isoformat()
                snapshot["status"] = "interrupted"
                snapshot["completed_at"] = interrupted_at
                snapshot["updated_at"] = interrupted_at
                snapshot["error"] = {"type": "BackendRestart", "message": "Run was interrupted by a backend restart."}
                for node in snapshot.get("nodes", []):
                    if node.get("status") in {"queued", "running"}:
                        node["status"] = "interrupted"
                        node["finished_at"] = interrupted_at
                connection.execute(
                    "UPDATE workflow_runs SET status = 'interrupted', updated_at = ?, snapshot_json = ? WHERE run_id = ?",
                    (interrupted_at, _json(snapshot), row["run_id"]),
                )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

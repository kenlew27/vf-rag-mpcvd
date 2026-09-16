"""Eval harness API — batch result CSV listing, serving, and launching.

The harness HTML is served statically by nginx from frontend/dist/testing/.
These routes handle only the data endpoints, under /agent/testing/ so nginx
proxies them to FastAPI without any additional nginx config.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

router = APIRouter(prefix="/agent/testing", tags=["eval-harness"])

_RESULTS_DIR = Path("tests/workflows/results")
_REPO_ROOT = Path(__file__).parents[2]
_SLUG_RE = re.compile(r"^batch_(\d{8}_\d{6})$")
_JOB_RE = re.compile(r"^job_(\d{8}_\d{6})$")

_ALLOWED_PRESETS = {
    "full_answer",
    "database_only",
    "internal_documents_only",
    "external_documents_only",
    "database_external",
    "database_internal_external",
    "evidence_packet_synthesis",
    "verification_only",
}

# Any safe filename ending in .jsonl or .json — no path traversal
_BANK_RE = re.compile(r"^[\w\-]+\.(jsonl|json)$")

# in-process job registry: job_id -> state dict
_jobs: dict[str, dict[str, Any]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

_BANKS_DIR = _REPO_ROOT / "tests/workflows"


def _subprocess_env() -> dict[str, str]:
    """Build env for batch_runner subprocess: current env + .env file overrides."""
    import os
    env = dict(os.environ)
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env.setdefault(k, v)  # don't override vars already in the process env
    return env


@router.get("/banks")
def list_banks():
    """List available question bank files in tests/workflows/."""
    if not _BANKS_DIR.exists():
        return []
    files = sorted(
        p.name for p in _BANKS_DIR.iterdir()
        if p.is_file() and _BANK_RE.fullmatch(p.name)
    )
    return files


def _result_files() -> list[Path]:
    if not _RESULTS_DIR.exists():
        return []
    return sorted(_RESULTS_DIR.glob("batch_*.csv"), reverse=True)


def _drain_proc(job_id: str, proc: subprocess.Popen) -> None:
    job = _jobs[job_id]
    for raw_line in proc.stdout:  # type: ignore[union-attr]
        line = raw_line.rstrip()
        job["lines"].append(line)
        # pick up the CSV slug the runner prints on completion
        m = re.search(r"batch_(\d{8}_\d{6})\.csv", line)
        if m:
            job["csv_slug"] = f"batch_{m.group(1)}"
    proc.wait()
    job["done"] = True
    job["exit_code"] = proc.returncode


# ── run listing / fetching ────────────────────────────────────────────────────

@router.get("/runs")
def list_runs():
    """List available batch result CSVs with basic metadata."""
    runs = []
    for csv_path in _result_files():
        slug = csv_path.stem
        m = _SLUG_RE.match(slug)
        timestamp = m.group(1) if m else slug
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        data_lines = [l for l in lines[1:] if l.strip()]
        runs.append({
            "slug": slug,
            "timestamp": timestamp,
            "filename": csv_path.name,
            "total_questions": len(data_lines),
        })
    return runs


@router.get("/runs/{slug}", response_class=PlainTextResponse)
def get_run(slug: str):
    """Return the raw CSV content for a batch run."""
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid run slug.")
    csv_path = _RESULTS_DIR / f"{slug}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Run not found.")
    return PlainTextResponse(
        csv_path.read_text(encoding="utf-8"),
        media_type="text/csv",
    )


# ── launching a new run ───────────────────────────────────────────────────────

class RunStartRequest(BaseModel):
    preset: str = "full_answer"
    questions_bank: str = "question_bank.jsonl"
    tags: str = ""
    workers: int = 1
    timeout: int = 300


@router.post("/runs/start")
def start_run(body: RunStartRequest):
    """Launch batch_runner.py as a subprocess and return a job_id for polling."""
    if body.preset not in _ALLOWED_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {body.preset}")
    if not _BANK_RE.fullmatch(body.questions_bank):
        raise HTTPException(status_code=400, detail=f"Invalid question bank filename: {body.questions_bank}")
    if not re.fullmatch(r"[a-zA-Z0-9_,\-]*", body.tags):
        raise HTTPException(status_code=400, detail="tags may only contain letters, digits, _, -, ,")
    workers = max(1, min(8, body.workers))
    timeout = max(30, min(600, body.timeout))

    job_id = datetime.now(timezone.utc).strftime("job_%Y%m%d_%H%M%S")
    questions_path = f"tests/workflows/{body.questions_bank}"

    cmd = [
        sys.executable, "-m", "tests.workflows.batch_runner",
        "--preset", body.preset,
        "--questions", questions_path,
        "--workers", str(workers),
        "--timeout", str(timeout),
    ]
    if body.tags.strip():
        cmd += ["--tags", body.tags.strip()]

    proc = subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_subprocess_env(),
    )
    _jobs[job_id] = {
        "process": proc,
        "lines": [],
        "done": False,
        "exit_code": None,
        "csv_slug": None,
        "preset": body.preset,
        "bank": body.questions_bank,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    threading.Thread(target=_drain_proc, args=(job_id, proc), daemon=True).start()
    return {"job_id": job_id}


@router.get("/runs/status/{job_id}")
def job_status(job_id: str):
    """Poll progress of a running or completed batch job."""
    if not _JOB_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id.")
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = _jobs[job_id]

    pct = 0
    succeeded = 0
    total = 0
    for line in reversed(job["lines"]):
        if m := re.search(r"\[(\d+)%\]", line):
            pct = int(m.group(1))
        if m := re.search(r"(\d+)/(\d+) succeeded", line):
            succeeded, total = int(m.group(1)), int(m.group(2))
        if pct and (succeeded or total):
            break

    return {
        "done": job["done"],
        "exit_code": job["exit_code"],
        "csv_slug": job["csv_slug"],
        "pct": pct,
        "succeeded": succeeded,
        "total": total,
        "lines": job["lines"][-40:],
        "started_at": job.get("started_at"),
        "preset": job.get("preset"),
        "bank": job.get("bank"),
    }

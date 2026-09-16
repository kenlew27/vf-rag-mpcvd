"""
agent/run_log.py

Assembles a human-readable .txt run log from all per-node JSONL logs.
One file per run_id, written to run_logs/<run_id>.txt.

Usage:
    python -m agent.run_log <run_id> [log_dir]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CLASSIFY_PATH = os.environ.get("CLASSIFY_INTENT_LOG_PATH", "classify_intent_steps.jsonl")
_BIGQUERY_PATH = os.environ.get("RETRIEVE_BIGQUERY_LOG_PATH", "retrieve_bigquery_steps.jsonl")
_DOCUMENT_PATH = os.environ.get("RETRIEVE_DOCUMENT_LOG_PATH", "retrieve_document_steps.jsonl")
_VERIFY_PATH = os.environ.get("VERIFICATION_LOG_PATH", "verification_steps.jsonl")
_REASONING_PATH = os.environ.get("REASONING_LOG_PATH", "reasoning_steps.jsonl")
_SUPERVISOR_PATH = os.environ.get("SUPERVISOR_LOG_PATH", "supervisor_calls.jsonl")
_ERRORS_PATH = os.environ.get("AGENT_ERRORS_LOG_PATH", "agent_errors.jsonl")
_RUN_LOG_DIR = os.environ.get("RUN_LOG_DIR", "run_logs")


def write_run_log(run_id: str, log_dir: str | None = None, error: Exception | None = None) -> str:
    """Assemble all JSONL entries for run_id into a single .txt file. Returns the output path."""
    out_dir = Path(log_dir or _RUN_LOG_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.txt"

    classify = _first(_CLASSIFY_PATH, run_id)
    bigquery = _first(_BIGQUERY_PATH, run_id)
    doc_entries = _read_jsonl(_DOCUMENT_PATH, run_id)
    rewrite_entry = next((e for e in doc_entries if e.get("node") == "rewrite_document_query"), None)
    doc_agents = [e for e in doc_entries if e.get("node") != "rewrite_document_query"]
    verify = _last(_VERIFY_PATH, run_id)
    reasoning = _last(_REASONING_PATH, run_id)
    supervisor = _first(_SUPERVISOR_PATH, run_id)
    errors = _read_jsonl(_ERRORS_PATH, run_id)

    lines: list[str] = []

    lines += [_section("RUN"), f"run_id : {run_id}", f"logged : {datetime.now(timezone.utc).isoformat()}", ""]

    if classify:
        lines += [_section("QUERY")]
        lines += [f"raw_query      : {classify.get('query', '')}", ""]

    if classify:
        lines += [_section("INTENT CLASSIFICATION")]
        lines += [
            f"model          : {classify.get('model', '')}",
            f"prompt_version : {classify.get('prompt_version', '')}",
            f"intents        : {', '.join(classify.get('intents', []))}",
            f"doc_scopes     : {', '.join(classify.get('document_scopes', []))}",
            f"tokens         : {classify.get('input_tokens')} in / {classify.get('output_tokens')} out",
            f"latency_ms     : {classify.get('latency_ms')}",
            "",
            "raw_response:",
            classify.get("raw_response", "(none)"),
            "",
        ]

    if rewrite_entry:
        lines += [_section("QUERY REWRITE")]
        lines += [
            f"model          : {rewrite_entry.get('model', '')}",
            f"prompt_version : {rewrite_entry.get('prompt_version', '')}",
            f"rewritten      : {rewrite_entry.get('rewritten_query', '')}",
            f"notes          : {'; '.join(rewrite_entry.get('rewrite_notes', []))}",
            f"tokens         : {rewrite_entry.get('input_tokens')} in / {rewrite_entry.get('output_tokens')} out",
            f"llm_latency_ms : {rewrite_entry.get('llm_latency_ms')}",
            "",
        ]

    if supervisor:
        lines += [_section("SUPERVISOR DISPATCH")]
        lines += [_fmt_json(supervisor), ""]

    if bigquery:
        lines += [_section("DATABASE RETRIEVAL")]
        lines += [
            f"model          : {bigquery.get('model', '')}",
            f"prompt_version : {bigquery.get('prompt_version', '')}",
            f"parse_status   : {bigquery.get('parse_status', '')}",
            f"dry_run_passed : {bigquery.get('dry_run_passed')}",
            f"rows_returned  : {bigquery.get('rows_returned')}",
            f"llm_latency_ms : {bigquery.get('llm_latency_ms')}",
            "",
            "filters_extracted:",
            _fmt_json(bigquery.get("filters_extracted", {})),
            "",
            "sql_generated:",
            str(bigquery.get("sql_generated", "(none)")),
            "",
        ]

    if doc_agents:
        lines += [_section("DOCUMENT RETRIEVAL")]
        for entry in doc_agents:
            scope = entry.get("scope") or entry.get("node", "")
            outcome = entry.get("outcome", "")
            lines += [
                f"[{scope}] outcome={outcome}  contexts={entry.get('contexts_returned', 0)}",
                f"  rewritten_query : {entry.get('rewritten_query', entry.get('query', ''))}",
                f"  from_cache      : {entry.get('rewrite_from_cache', False)}",
                f"  embedding_model : {entry.get('embedding_model', '')}",
            ]
            if entry.get("clarification_reason"):
                lines.append(f"  clarification   : {entry['clarification_reason']}")
            lines.append("")

    if reasoning:
        lines += [_section("SYNTHESIS REASONING")]
        lines += [
            f"model          : {reasoning.get('model', '')}",
            f"thinking_tokens: {reasoning.get('thinking_tokens', 0)}",
            f"output_tokens  : {reasoning.get('output_tokens', 0)}",
            f"latency_ms     : {reasoning.get('latency_ms', '')}",
            "",
        ]
        thinking = reasoning.get("thinking_trace") or reasoning.get("thinking", "")
        if thinking:
            lines += ["--- thinking trace ---", str(thinking), "---", ""]
        answer_text = _nested(reasoning, "final_answer", "synthesis", "answer") or reasoning.get("answer_text", "")
        if answer_text:
            lines += ["--- answer draft ---", str(answer_text), "---", ""]

    if verify:
        lines += [_section("VERIFIER")]
        lines += [
            f"model          : {verify.get('verifier_model', '')}",
            f"prompt_version : {verify.get('verifier_prompt_version', '')}",
            f"status         : {verify.get('status', '')}",
            f"action         : {verify.get('action', '')}",
            f"retry_used     : {verify.get('retry_used', False)}",
            f"claims_total   : {verify.get('claims_checked', 0)}",
            f"claims_checked : {verify.get('claims_llm_checked', 0)}",
            f"latency_ms     : {verify.get('latency_ms', '')}",
            "",
        ]
        assessments = verify.get("assessments", [])
        if assessments:
            lines.append("claim assessments:")
            for assessment in assessments:
                lines.append(
                    f"  {assessment.get('claim_id')} — "
                    f"{assessment.get('status')} "
                    f"(evidence: {', '.join(assessment.get('evidence_ids', [])) or 'none'})"
                )
            lines.append("")

    all_errors = list(errors)
    if error:
        all_errors.append({
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__,
            "error_message": str(error),
        })
    parse_errors = [
        e for e in _read_jsonl(_BIGQUERY_PATH, run_id) if e.get("parse_status") == "fallback"
    ]
    partial_results = [
        e for e in _read_jsonl(_VERIFY_PATH, run_id) if e.get("status") == "partial_after_retry"
    ]
    if all_errors or parse_errors or partial_results:
        lines += [_section("ERRORS / WARNINGS")]
        for e in all_errors:
            lines += [
                f"[ERROR] {e.get('timestamp', '')}",
                f"  query : {e.get('query', '')}",
                f"  type  : {e.get('error_type', '')}",
                f"  msg   : {e.get('error_message', '')}",
                "",
            ]
            if e.get("traceback"):
                lines += ["  traceback:", *[f"    {line}" for line in e["traceback"].splitlines()], ""]
        for e in parse_errors:
            lines += [f"[WARN] BigQuery filter parse fell back to empty at {e.get('timestamp', '')}", ""]
        for e in partial_results:
            lines += [f"[WARN] Verifier returned a partially verified answer after retry at {e.get('timestamp', '')}", ""]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return str(out_path)


def log_error(run_id: str, query: str, error: Exception) -> None:
    """Append an error record to agent_errors.jsonl."""
    record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": traceback.format_exc(),
    }
    try:
        with open(_ERRORS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("run_log | could not write error log: %s", exc)


def _read_jsonl(path: str, run_id: str) -> list[dict[str, Any]]:
    results = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("run_id") == run_id:
                        results.append(record)
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("run_log | could not read %s: %s", path, exc)
    return results


def _first(path: str, run_id: str) -> dict[str, Any] | None:
    entries = _read_jsonl(path, run_id)
    return entries[0] if entries else None


def _last(path: str, run_id: str) -> dict[str, Any] | None:
    entries = _read_jsonl(path, run_id)
    return entries[-1] if entries else None


def _section(title: str) -> str:
    bar = "=" * 60
    return f"\n{bar}\n{title}\n{bar}"


def _fmt_json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return str(obj)


def _nested(record: dict, *keys: str) -> Any:
    value: Any = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m agent.run_log <run_id> [log_dir]")
        sys.exit(1)
    _run_id = sys.argv[1]
    _log_dir = sys.argv[2] if len(sys.argv) > 2 else None
    _out = write_run_log(_run_id, log_dir=_log_dir)
    print(f"Written: {_out}")

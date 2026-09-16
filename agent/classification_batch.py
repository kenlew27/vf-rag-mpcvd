"""Run request classification for a JSON question batch.

Usage:
    python -m agent.classification_batch questions.json classifications.csv --model claude-sonnet
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from agent.nodes.plan_request import plan_request
from agent.state import AgentState, UserQuery
from agent.supervisor_routing import routed_agents

_CSV_FIELDS = [
    "query",
    "predicted_tasks",
    "predicted_knowledge_sources",
    "status",
    "reasons",
    "routed_agents",
    "diagnostic_id",
    "duration_ms",
    "attempts",
    "error",
    "traceback",
]


def run_batch(
    input_path: Path,
    output_path: Path,
    *,
    model: str | None = None,
    client: Any = None,
    planner: Callable[..., AgentState] | None = None,
) -> None:
    """Classify each question in a JSON array and write its plan to CSV."""
    questions = _load_questions(input_path)
    planner = planner or plan_request

    diagnostic_path = output_path.with_suffix(output_path.suffix + ".jsonl")
    with output_path.open("w", newline="", encoding="utf-8") as output_file, diagnostic_path.open(
        "w", encoding="utf-8"
    ) as diagnostic_file:
        writer = csv.DictWriter(output_file, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for item in questions:
            row, diagnostic = _classify(item["question"], planner, client, model)
            writer.writerow(row)
            diagnostic_file.write(json.dumps(_redact_value(diagnostic), ensure_ascii=False, default=str) + "\n")


def _load_questions(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open(encoding="utf-8") as input_file:
        payload = json.load(input_file)

    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array")
    if not all(isinstance(item, dict) and isinstance(item.get("question"), str) for item in payload):
        raise ValueError("each input item must be an object with a string question")
    return payload


def _classify(
    question: str,
    planner: Callable[..., AgentState],
    client: Any,
    model: str | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    started = time.monotonic()
    diagnostic_id = str(uuid.uuid4())
    attempts = [{"attempt": 1}]
    try:
        state = planner(
            AgentState(query=UserQuery(raw_text=question)),
            client=client,
            model=model,
        )
        if state.request_plan is None:
            raise ValueError("planner returned no request plan")
        request_plan = state.request_plan
        row = {
            "query": question,
            "predicted_tasks": _join(request_plan.tasks),
            "predicted_knowledge_sources": _join(request_plan.knowledge_sources),
            "status": str(request_plan.status),
            "reasons": _join(request_plan.reasons),
            "routed_agents": _join(routed_agents(request_plan)),
            "diagnostic_id": diagnostic_id,
            "error": "",
            "traceback": "",
        }
        diagnostic = {
            "diagnostic_id": diagnostic_id,
            "input": {"question": question, "model": model},
            "output": request_plan.model_dump(mode="json"),
            "routed_agents": routed_agents(request_plan),
            "attempts": attempts,
            "status": "succeeded",
        }
    except Exception as exc:
        exception_traceback = _redact("".join(traceback.format_exception(exc)))
        print(exception_traceback, file=sys.stderr)
        error = _redact(f"{type(exc).__name__}: {exc}")
        row = {
            "query": question,
            "predicted_tasks": "",
            "predicted_knowledge_sources": "",
            "status": "error",
            "reasons": error,
            "routed_agents": "",
            "diagnostic_id": diagnostic_id,
            "error": error,
            "traceback": exception_traceback,
        }
        diagnostic = {
            "diagnostic_id": diagnostic_id,
            "input": {"question": question, "model": model},
            "attempts": attempts,
            "status": "failed",
            "error": error,
            "traceback": exception_traceback,
        }
    duration_ms = int((time.monotonic() - started) * 1000)
    row["duration_ms"] = str(duration_ms)
    row["attempts"] = json.dumps(attempts)
    diagnostic["duration_ms"] = duration_ms
    return row, diagnostic


def _redact(text: str) -> str:
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|authorization|bearer|token|password)\b\s*[:=]\s*)[^\s,]+",
        r"\1[REDACTED]",
        text,
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _join(values: list[Any]) -> str:
    return ";".join(str(value) for value in values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify JSON questions with the request planner.")
    parser.add_argument("input", type=Path, help="path to a JSON array of question objects")
    parser.add_argument("output", type=Path, help="path to write the CSV results")
    parser.add_argument("--model", help="model passed to agent.nodes.plan_request.plan_request")
    args = parser.parse_args()
    run_batch(args.input, args.output, model=args.model)


if __name__ == "__main__":
    main()

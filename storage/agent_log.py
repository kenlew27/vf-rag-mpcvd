"""Structured reasoning log appender."""

from typing import Any


def append_run_log(record: Any, path: str | None = None) -> None:
    """Append a reasoning-trace record to the JSONL log."""

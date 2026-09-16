"""Lightweight timing helpers for agent step instrumentation."""

from contextlib import contextmanager


class agent_timing:
    """Dual-use: works as both a decorator and a context manager for timing agent steps."""

    def __init__(self, name: str):
        self.name = name

    def __call__(self, fn):
        return fn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def emit_agent_timing(name: str, elapsed_ms: float) -> None:
    """Emit a timing measurement to the structured log."""


@contextmanager
def agent_timing_context(context: dict | str = ""):
    """Context manager that sets timing context for nested agent_timing calls."""
    yield

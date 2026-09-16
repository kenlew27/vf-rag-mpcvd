"""Lightweight timing helpers for agent step instrumentation."""

from contextlib import contextmanager


def agent_timing(name: str):
    """Decorator that records execution time of an agent step."""
    def decorator(fn):
        return fn
    return decorator


def emit_agent_timing(name: str, elapsed_ms: float) -> None:
    """Emit a timing measurement to the structured log."""


@contextmanager
def agent_timing_context(name: str):
    """Context manager variant of :func:`agent_timing`."""
    yield

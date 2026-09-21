"""Shared internal utilities for the agent package.

These helpers are used across multiple modules. Centralising them here
eliminates copy-paste duplication and ensures consistent behaviour.
"""

from __future__ import annotations

from typing import Any

#: Number of hex characters used when truncating SHA-256 digests for logging.
HASH_PREFIX_LEN = 12


def attr(obj: Any, key: str, default: Any = None) -> Any:
    """Return obj[key] if obj is a dict, else getattr(obj, key, default)."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def strip_json_fence(text: str) -> str:
    """Remove a leading ```json / ``` fence and trailing ``` fence, if present."""
    lines = text.strip().splitlines()
    if not lines:
        return text
    if lines[0].strip().lower() in {"```", "```json"}:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def build_anthropic_client() -> Any:
    """Construct a default Anthropic client using environment credentials."""
    from anthropic import Anthropic  # deferred to avoid import-time side effects
    return Anthropic()

"""Configuration helpers for evidence planning and synthesis."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "f", "no", "n", "off", ""}


@dataclass(frozen=True)
class EvidencePlannerConfig:
    """Runtime config for the evidence planner pipeline.

    The planner is intentionally disabled by default until route integration is
    added explicitly.
    """

    enabled: bool = False
    planner_model: str | None = None
    synthesizer_model: str | None = None
    planner_max_tokens: int = 4096
    synthesizer_max_tokens: int = 8192
    strict_planner_failure: bool = False


def load_evidence_planner_config(
    environ: Mapping[str, str] | None = None,
) -> EvidencePlannerConfig:
    """Load evidence planner config from env-like values."""

    env = os.environ if environ is None else environ
    return EvidencePlannerConfig(
        enabled=_parse_bool(env.get("EVIDENCE_PLANNER_ENABLED"), default=False),
        planner_model=env.get("EVIDENCE_PLANNER_MODEL") or env.get("ANTHROPIC_MODEL"),
        synthesizer_model=env.get("EVIDENCE_SYNTHESIZER_MODEL") or env.get("ANTHROPIC_MODEL"),
        planner_max_tokens=_parse_int(env.get("EVIDENCE_PLANNER_MAX_TOKENS"), default=4096),
        synthesizer_max_tokens=_parse_int(env.get("EVIDENCE_SYNTHESIZER_MAX_TOKENS"), default=8192),
        strict_planner_failure=_parse_bool(env.get("EVIDENCE_PLANNER_STRICT"), default=False),
    )


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def _parse_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default

"""Prompt loaders for evidence planning and synthesis."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parents[1] / "prompts"
PLANNER_PROMPT_PATH = _PROMPTS_DIR / "evidence_planner.md"
SYNTHESIZER_PROMPT_PATH = _PROMPTS_DIR / "evidence_synthesizer.md"


def load_planner_system_prompt() -> str:
    return PLANNER_PROMPT_PATH.read_text().strip()


def load_synthesizer_system_prompt() -> str:
    return SYNTHESIZER_PROMPT_PATH.read_text().strip()


PLANNER_SYSTEM_PROMPT = load_planner_system_prompt()
SYNTHESIZER_SYSTEM_PROMPT = load_synthesizer_system_prompt()

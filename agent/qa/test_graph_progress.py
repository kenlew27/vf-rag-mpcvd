"""Progress callback coverage for the request-planning graph."""

import pytest

pytest.importorskip("langgraph")

from agent.graph import build_supervisor_agent


def test_graph_exposes_request_planning_node():
    events = []
    agent = build_supervisor_agent(progress_callback=lambda stage, status: events.append((stage, status)))
    assert agent is not None
    assert "verify_answer" in agent.get_graph().nodes
    assert events == []


def test_graph_can_stop_after_synthesis_without_verifier():
    agent = build_supervisor_agent(include_verifier=False)

    assert "synthesize_answer" in agent.get_graph().nodes
    assert "verify_answer" not in agent.get_graph().nodes
    assert "compose_verified_answer" not in agent.get_graph().nodes

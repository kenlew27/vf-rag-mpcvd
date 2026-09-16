"""Tests for synthesize_answer node.

Strategy:
- Inject a FakeClient so no Anthropic API key is needed.
- Build minimal AgentState fixtures to exercise each code path.
- Every test is deterministic — no network calls, no filesystem side effects.

Evidence ID convention: EvidenceIdMinter generates EV-L-001 for the first
LITERATURE item, EV-D-001 for the first DOCUMENT item, EV-S-001 for STRUCTURED.
Tests must use these IDs so citation validation passes.
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from agent.nodes.synthesize_answer import synthesize_answer
from agent.request_plan import KnowledgeSource, RequestPlan, RequestStatus, RequestTask
from agent.schemas import ConfidenceLabel, ScopedAbstention
from agent.state import AgentState, UserQuery


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Usage:
    input_tokens = 100
    output_tokens = 200


class _Response:
    def __init__(self, text: str, include_thinking: bool = True) -> None:
        self.content = []
        if include_thinking:
            self.content.append(_Block("thinking", thinking="<scratchpad>reasoning</scratchpad>"))
        self.content.append(_Block("text", text=text))
        self.usage = _Usage()


class _Messages:
    def __init__(self, text: str, include_thinking: bool = True) -> None:
        self._text = text
        self._include_thinking = include_thinking
        self.last_kwargs: dict = {}

    def create(self, **kwargs: Any) -> _Response:
        self.last_kwargs = kwargs
        return _Response(self._text, include_thinking=self._include_thinking)


class _Client:
    def __init__(self, text: str, include_thinking: bool = True) -> None:
        self.messages = _Messages(text, include_thinking)


# ---------------------------------------------------------------------------
# JSON response builders
# ---------------------------------------------------------------------------

def _synthesis_json(
    *,
    answer: str = "The CVD growth rate is 2 µm/h [EV-L-001].",
    citations: list[str] | None = None,
    confidence: str = "high",
    confidence_basis: list[str] | None = None,
    abstention: dict | None = None,
) -> str:
    payload: dict = {
        "answer": answer,
        "citations": citations if citations is not None else ["EV-L-001"],
        "confidence": confidence,
        "confidence_basis": confidence_basis or ["Direct measurement reported in source."],
    }
    if abstention is not None:
        payload["abstention"] = abstention
    return json.dumps(payload)


def _abstention_json(reason: str = "No evidence retrieved.") -> str:
    return _synthesis_json(
        answer=reason,
        citations=[],
        confidence="not_assessable",
        confidence_basis=["No evidence was available."],
        abstention={
            "cannot_conclude": [reason],
            "can_still_state": [],
            "would_resolve": ["Relevant source documents."],
        },
    )


# ---------------------------------------------------------------------------
# State fixtures
# ---------------------------------------------------------------------------

_LIT_CONTEXT = {
    "context_id": "chunk-lit-001",
    "text": "CVD growth rate is 2 µm/h under standard hydrogen plasma conditions.",
    "rank": 1,
    "retrieval_score": 0.95,
    "document_id": "doc-external-abc",
    "chunk_id": "chunk-lit-001",
    "metadata": {"title": "Diamond CVD Study 2023"},
}

_DOC_CONTEXT = {
    "context_id": "chunk-int-001",
    "text": "Internal process standard requires 2 µm/h minimum deposition rate.",
    "rank": 1,
    "retrieval_score": 0.88,
    "document_id": "doc-internal-xyz",
    "chunk_id": "chunk-int-001",
    "metadata": {"title": "MDSAA Process Standard v4"},
}


def _plan(
    tasks: list[RequestTask] | None = None,
    sources: list[KnowledgeSource] | None = None,
    status: RequestStatus = RequestStatus.READY,
) -> RequestPlan:
    return RequestPlan(
        tasks=tasks or [RequestTask.LOOKUP],
        knowledge_sources=sources or [KnowledgeSource.EXTERNAL_LITERATURE],
        status=status,
    )


def _state_lit(question: str = "What is the CVD growth rate?") -> AgentState:
    """State with one external literature chunk."""
    return AgentState(
        query=UserQuery(raw_text=question),
        request_plan=_plan(),
        document_evidence={
            "external": {
                "agent": "external_document_agent",
                "rewritten_query": question,
                "contexts": [_LIT_CONTEXT],
            }
        },
        supervisor_meta={"agents_paged": ["external_document_agent"], "agents_empty": []},
    )


def _state_internal(question: str = "What does the process standard say?") -> AgentState:
    """State with one internal document chunk."""
    return AgentState(
        query=UserQuery(raw_text=question),
        request_plan=_plan(sources=[KnowledgeSource.INTERNAL_DOCUMENTS]),
        document_evidence={
            "internal": {
                "agent": "internal_document_agent",
                "rewritten_query": question,
                "contexts": [_DOC_CONTEXT],
            }
        },
        supervisor_meta={"agents_paged": ["internal_document_agent"], "agents_empty": []},
    )


def _state_structured() -> AgentState:
    """State with one BigQuery row."""
    return AgentState(
        query=UserQuery(raw_text="What is the hardness of sample S-042?"),
        request_plan=_plan(
            tasks=[RequestTask.LOOKUP],
            sources=[KnowledgeSource.STRUCTURED_DATA],
        ),
        database_evidence={
            "agent": "table_agent",
            "question": "hardness S-042",
            "evidence_rows": [{"Sample ID": "S-042", "Hardness (GPa)": "95.2", "Method": "Vickers"}],
        },
        supervisor_meta={"agents_paged": ["table_agent"], "agents_empty": []},
    )


def _state_empty() -> AgentState:
    """State with no evidence — synthesizer should abstain without calling LLM."""
    return AgentState(
        query=UserQuery(raw_text="What is the CVD growth rate?"),
        request_plan=_plan(),
        supervisor_meta={"agents_paged": ["external_document_agent"], "agents_empty": ["external_document_agent"]},
    )


def _state_not_ready(status: RequestStatus) -> AgentState:
    return AgentState(
        query=UserQuery(raw_text="some question"),
        request_plan=RequestPlan(
            tasks=[RequestTask.LOOKUP],
            knowledge_sources=[KnowledgeSource.EXTERNAL_LITERATURE],
            status=status,
            reasons=["Cannot proceed without clarification."],
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSynthesizeAnswerGuards(unittest.TestCase):
    """Guard conditions that raise before any LLM call."""

    def test_no_request_plan_raises(self) -> None:
        state = AgentState(query=UserQuery(raw_text="question"))
        with self.assertRaises(ValueError, msg="A request plan is required"):
            synthesize_answer(state, client=_Client("{}"), model="test")

    def test_no_api_key_and_no_client_raises(self) -> None:
        state = AgentState(query=UserQuery(raw_text="q"), request_plan=_plan())
        env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                synthesize_answer(state, model="test")
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_backup


class TestNotReadyStatus(unittest.TestCase):
    """When request_plan.status != READY the synthesizer returns early without calling the LLM."""

    def _run_not_ready(self, status: RequestStatus) -> dict:
        client = _Client(_synthesis_json())
        result = synthesize_answer(_state_not_ready(status), client=client, model="test")
        return result, client

    def test_needs_clarification_skips_llm(self) -> None:
        result, client = self._run_not_ready(RequestStatus.NEEDS_CLARIFICATION)
        self.assertFalse(client.messages.last_kwargs, "LLM must not be called for NEEDS_CLARIFICATION")
        fa = result["final_answer"]
        self.assertEqual(fa.synthesis.confidence, ConfidenceLabel.NOT_ASSESSABLE)

    def test_unsupported_request_skips_llm(self) -> None:
        result, client = self._run_not_ready(RequestStatus.UNSUPPORTED_REQUEST)
        self.assertFalse(client.messages.last_kwargs)

    def test_execution_failed_skips_llm(self) -> None:
        result, client = self._run_not_ready(RequestStatus.EXECUTION_FAILED)
        self.assertFalse(client.messages.last_kwargs)


class TestEmptyPacket(unittest.TestCase):
    """Empty evidence packet produces an abstention without calling the LLM."""

    def test_empty_packet_abstains(self) -> None:
        client = _Client(_synthesis_json())
        result = synthesize_answer(_state_empty(), client=client, model="test")
        fa = result["final_answer"]
        self.assertEqual(fa.synthesis.confidence, ConfidenceLabel.NOT_ASSESSABLE)
        self.assertIsNotNone(fa.synthesis.abstention)

    def test_empty_packet_no_llm_call(self) -> None:
        client = _Client(_synthesis_json())
        synthesize_answer(_state_empty(), client=client, model="test")
        self.assertFalse(client.messages.last_kwargs, "LLM must not be called for empty packet")


class TestNormalSynthesis(unittest.TestCase):
    """Happy path: evidence present → LLM called → FinalAnswer returned."""

    def _run(self, state: AgentState, response_json: str | None = None) -> tuple[dict, _Client]:
        client = _Client(response_json or _synthesis_json())
        result = synthesize_answer(state, client=client, model="claude-test")
        return result, client

    def test_returns_final_answer_and_evidence_packet(self) -> None:
        result, _ = self._run(_state_lit())
        self.assertIn("final_answer", result)
        self.assertIn("evidence_packet", result)

    def test_final_answer_has_answer_text(self) -> None:
        result, _ = self._run(_state_lit())
        self.assertIn("2 µm/h", result["final_answer"].synthesis.answer)

    def test_evidence_packet_contains_lit_item(self) -> None:
        result, _ = self._run(_state_lit())
        packet = result["evidence_packet"]
        items = packet["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["evidence_id"], "EV-L-001")
        self.assertIn("2 µm/h", items[0]["content"])

    def test_llm_called_with_thinking_enabled(self) -> None:
        _, client = self._run(_state_lit())
        kwargs = client.messages.last_kwargs
        self.assertEqual(kwargs.get("thinking", {}).get("type"), "enabled")

    def test_llm_called_with_injected_model(self) -> None:
        _, client = self._run(_state_lit())
        self.assertEqual(client.messages.last_kwargs.get("model"), "claude-test")

    def test_internal_doc_evidence_id_is_ev_d(self) -> None:
        internal_response = _synthesis_json(
            answer="The standard requires 2 µm/h minimum [EV-D-001].",
            citations=["EV-D-001"],
        )
        result, _ = self._run(_state_internal(), internal_response)
        items = result["evidence_packet"]["items"]
        self.assertEqual(items[0]["evidence_id"], "EV-D-001")

    def test_structured_evidence_id_is_ev_s(self) -> None:
        structured_response = _synthesis_json(
            answer="Sample S-042 has hardness 95.2 GPa [EV-S-001].",
            citations=["EV-S-001"],
        )
        result, _ = self._run(_state_structured(), structured_response)
        items = result["evidence_packet"]["items"]
        self.assertEqual(items[0]["evidence_id"], "EV-S-001")

    def test_run_id_propagated_to_final_answer(self) -> None:
        state = _state_lit()
        result, _ = self._run(state)
        self.assertEqual(result["final_answer"].run_id, state.run_id)

    def test_high_confidence_label(self) -> None:
        result, _ = self._run(_state_lit())
        self.assertEqual(result["final_answer"].synthesis.confidence, ConfidenceLabel.HIGH)


class TestCitationValidation(unittest.TestCase):
    """citation_check is computed deterministically from model output vs packet."""

    def _run_with_citations(self, citations: list[str]) -> dict:
        response = _synthesis_json(
            answer=" ".join(f"Claim [{c}]." for c in citations),
            citations=citations,
        )
        client = _Client(response)
        return synthesize_answer(_state_lit(), client=client, model="test")

    def test_valid_citation_passes_check(self) -> None:
        result = self._run_with_citations(["EV-L-001"])
        check = result["final_answer"].citation_check
        self.assertTrue(check.all_cited_ids_exist)
        self.assertEqual(check.unknown_ids, [])

    def test_fabricated_citation_id_flagged(self) -> None:
        result = self._run_with_citations(["EV-L-999"])
        check = result["final_answer"].citation_check
        self.assertFalse(check.all_cited_ids_exist)
        self.assertIn("EV-L-999", check.unknown_ids)

    def test_empty_citations_when_evidence_present_flagged(self) -> None:
        response = _synthesis_json(
            answer="The growth rate is 2 µm/h.",
            citations=[],
        )
        client = _Client(response)
        result = synthesize_answer(_state_lit(), client=client, model="test")
        check = result["final_answer"].citation_check
        self.assertTrue(check.uncited_answer)


class TestJsonParsing(unittest.TestCase):
    """Synthesizer parser robustness — it must recover from common model quirks."""

    def _run(self, text: str) -> dict:
        return synthesize_answer(_state_lit(), client=_Client(text), model="test")

    def test_json_in_markdown_fence_recovered(self) -> None:
        fenced = "```json\n" + _synthesis_json() + "\n```"
        result = self._run(fenced)
        self.assertIn("final_answer", result)

    def test_plain_json_fence_recovered(self) -> None:
        fenced = "```\n" + _synthesis_json() + "\n```"
        result = self._run(fenced)
        self.assertIn("final_answer", result)

    def test_extra_whitespace_handled(self) -> None:
        result = self._run("   \n" + _synthesis_json() + "\n   ")
        self.assertIn("final_answer", result)


class TestEvidencePacketStructure(unittest.TestCase):
    """The returned evidence_packet dict matches EvidencePacket schema."""

    def test_packet_has_required_keys(self) -> None:
        client = _Client(_synthesis_json())
        result = synthesize_answer(_state_lit(), client=client, model="test")
        packet = result["evidence_packet"]
        for key in ("run_id", "query_raw", "tasks", "knowledge_sources", "items"):
            self.assertIn(key, packet, f"evidence_packet missing key: {key}")

    def test_packet_query_matches_state(self) -> None:
        q = "What is the CVD growth rate?"
        client = _Client(_synthesis_json())
        result = synthesize_answer(_state_lit(q), client=client, model="test")
        self.assertEqual(result["evidence_packet"]["query_raw"], q)

    def test_packet_lanes_tracked(self) -> None:
        client = _Client(_synthesis_json())
        result = synthesize_answer(_state_lit(), client=client, model="test")
        packet = result["evidence_packet"]
        self.assertIn("external", packet.get("lanes_attempted", []))

    def test_empty_lanes_recorded_when_agent_returned_nothing(self) -> None:
        state = AgentState(
            query=UserQuery(raw_text="Q"),
            request_plan=_plan(),
            document_evidence={
                "external": {"agent": "external_document_agent", "contexts": []},
            },
            supervisor_meta={
                "agents_paged": ["external_document_agent"],
                "agents_empty": ["external_document_agent"],
            },
        )
        client = _Client(_synthesis_json())
        result = synthesize_answer(state, client=client, model="test")
        self.assertIn("external", result["evidence_packet"].get("lanes_empty", []))


class TestReasoningLogDoesNotCrash(unittest.TestCase):
    """_append_log must not raise even with a bad REASONING_LOG_PATH."""

    def test_bad_log_path_does_not_raise(self) -> None:
        os.environ["REASONING_LOG_PATH"] = "/nonexistent_dir/reasoning.jsonl"
        try:
            client = _Client(_synthesis_json())
            result = synthesize_answer(_state_lit(), client=client, model="test")
            # If we got here, the log failure was swallowed — expected behaviour
            self.assertIn("final_answer", result)
        finally:
            os.environ.pop("REASONING_LOG_PATH", None)


if __name__ == "__main__":
    unittest.main()

"""Tests for app/api/routes_agent.py."""

from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.qa.test_routes_vector_search import (
    FakeChunkStore,
    FakeEmbedder,
    FakeKeywordIndex,
    FakeReranker,
    FakeVectorIndex,
)
from storage.storage_contracts import DocumentRecord


class FakeDocumentStore:
    def get_document(self, document_id: str) -> DocumentRecord:
        if document_id != "doc-1":
            raise KeyError(document_id)
        return DocumentRecord(
            document_id="doc-1",
            source_uri="gs://bucket/parsed-pdf/doc-1/source.pdf",
            metadata={"original_filename": "stored-paper.pdf", "document_scope": "internal"},
            ingest_status="embedded",
        )


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        schema = ((kwargs.get("output_config") or {}).get("format") or {}).get("schema") or {}
        properties = schema.get("properties") or {}
        if "tasks" in properties:
            return _text_response(
                {
                    "tasks": ["lookup"],
                    "knowledge_sources": ["external_literature"],
                    "status": "ready",
                    "reasons": [],
                }
            )
        if "checks" in properties:
            return _text_response({"checks": []})
        if "thinking" in kwargs:
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", thinking="checked retrieved context"),
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "answer": "The paper discusses microwave plasma assisted CVD diamond growth at high gas pressure. [EV-L-001]",
                                "citations": ["EV-L-001"],
                                "confidence": "medium",
                                "confidence_basis": ["one retrieved paper context"],
                                "contradictions_noted": [],
                                "abstention": None,
                            }
                        ),
                    ),
                ],
                usage=SimpleNamespace(input_tokens=100, output_tokens=80),
            )
        return _text_response(
            {
                "rewritten_query": "microwave plasma assisted CVD diamond growth high gas pressure",
                "rewrite_notes": ["expanded retrieval terms"],
            }
        )


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def _text_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class TestAppStartupWarmup(unittest.TestCase):
    def test_lifespan_warms_default_reranker_once(self):
        import app.main as main

        with patch("app.main.warm_default_leaf_reranker") as warmup:
            with TestClient(main.app):
                pass

        warmup.assert_called_once_with()

    def test_lifespan_warmup_failure_logs_warning_without_crashing(self):
        import app.main as main

        with patch("app.main.warm_default_leaf_reranker", side_effect=RuntimeError("model unavailable")):
            with self.assertLogs("app.main", level="WARNING") as logs:
                with TestClient(main.app):
                    pass

        self.assertIn("Default leaf reranker warmup failed during startup.", logs.output[0])


class TestAgentQueryRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    def test_blank_question_returns_400(self):
        response = self.client.post("/agent/query", json={"question": " \n\t "})

        self.assertEqual(400, response.status_code)
        self.assertEqual("question must not be empty", response.json()["detail"])

    def test_missing_model_returns_503(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_MODEL", None)

            response = self.client.post("/agent/query", json={"question": "Raman stress"})

        self.assertEqual(503, response.status_code)
        self.assertEqual("ANTHROPIC_MODEL env var is not set", response.json()["detail"])

    def test_data_query_passes_original_and_supervisor_queries_to_database_agent(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "database_evidence": {"query_type": "aggregate", "evidence_rows": []}
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_table_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/data-query",
                    json={
                        "question": "Compare our nitrogen runs with the selected study.",
                        "supervisor_query": "Calculate average H2N2 and growth rate by reactor.",
                        "include_debug_trace": True,
                    },
                )

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertEqual(
            "Compare our nitrogen runs with the selected study.",
            state.query.raw_text,
        )
        self.assertEqual(
            "Calculate average H2N2 and growth rate by reactor.",
            state.request_plan.structured_data_question,
        )
        self.assertTrue(state.include_debug_trace)

    def test_data_query_rejects_blank_supervisor_query(self):
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            response = self.client.post(
                "/agent/data-query",
                json={"question": "Valid question", "supervisor_query": "  "},
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "supervisor_query must not be empty when provided",
            response.json()["detail"],
        )

    def test_synthesize_propagates_opt_in_database_debug_trace(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {}

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch(
                "app.api.routes_agent.build_supervisor_agent", return_value=fake_agent
            ):
                response = self.client.post(
                    "/agent/synthesize",
                    json={
                        "question": "Compare this run with paper.pdf.",
                        "include_debug_trace": True,
                    },
                )

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertTrue(state.include_debug_trace)

    def test_query_returns_planned_document_evidence(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {
                "internal": {"agent": "internal_document_agent", "contexts_returned": 1}
            },
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/query",
                    json={"question": "Use internal reports"},
                )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(
            {"run_id", "request_plan", "database_evidence", "document_evidence", "source_documents", "final_answer"},
            set(body),
        )
        self.assertEqual("internal_document_agent", body["document_evidence"]["internal"]["agent"])
        state = fake_agent.invoke.call_args.args[0]

    def test_query_maps_document_ids_to_source_filters(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {},
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/query",
                    json={"question": "Use selected PDFs", "document_ids": ["doc-1", " doc-2 ", "doc-1", ""]},
                )

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertEqual({"document_id": ["doc-1", "doc-2"]}, state.source_filters)

    def test_query_defaults_missing_source_mode_to_all(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {},
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post("/agent/query", json={"question": "What is CVD diamond?"})

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertEqual("all", state.source_mode)
        self.assertEqual([], state.selected_document_ids)

    def test_query_accepts_source_mode_none_without_document_ids(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {},
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/query",
                    json={"question": "What is CVD diamond?", "source_mode": "none"},
                )

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertEqual("none", state.source_mode)
        self.assertEqual({}, state.source_filters)

    def test_query_accepts_selected_source_mode_with_document_ids(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {},
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/query",
                    json={
                        "question": "Summarize selected PDFs",
                        "source_mode": "selected",
                        "document_ids": ["doc-1", " doc-2 ", "doc-1"],
                    },
                )

        self.assertEqual(200, response.status_code)
        state = fake_agent.invoke.call_args.args[0]
        self.assertEqual("selected", state.source_mode)
        self.assertEqual(["doc-1", "doc-2"], state.selected_document_ids)
        self.assertEqual({"document_id": ["doc-1", "doc-2"]}, state.source_filters)

    def test_query_rejects_selected_source_mode_without_document_ids(self):
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            response = self.client.post(
                "/agent/query",
                json={"question": "Summarize selected PDFs", "source_mode": "selected"},
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "document_ids must be provided when source_mode is selected",
            response.json()["detail"],
        )

    def test_synthesize_returns_reasoning_pipeline_response(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {"agent": "database_agent", "evidence_rows": []},
            "document_evidence": {},
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post("/agent/synthesize", json={"question": "Use internal reports"})

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(
            {"run_id", "request_plan", "database_evidence", "document_evidence", "source_documents", "final_answer"},
            set(body),
        )

    def test_synthesize_includes_final_answer_when_graph_produces_one(self):
        from agent.request_plan import KnowledgeSource, RequestStatus, RequestTask
        from agent.schemas import FinalAnswer, SynthesisOutput, EvidencePacket, EvidenceIdMinter, SourceType
        from agent.schemas import EvidenceItem, Provenance, ConfidenceLabel

        minter = EvidenceIdMinter()
        item = EvidenceItem(
            evidence_id=minter.mint(SourceType.STRUCTURED),
            source_type=SourceType.STRUCTURED,
            source_label="Run EXP-001",
            content="growth_rate: 3.8 µm/hr",
            provenance=Provenance(origin_agent="table_agent", source_ref="bigquery"),
        )
        packet = EvidencePacket(
            run_id="test-run",
            query_raw="test question",
            tasks=[RequestTask.LOOKUP],
            knowledge_sources=[KnowledgeSource.STRUCTURED_DATA],
            request_status=RequestStatus.READY,
            items=[item],
            lanes_attempted=["structured"],
        )
        synthesis = SynthesisOutput(
            answer="Growth rate was 3.8 µm/hr [EV-S-001].",
            citations=["EV-S-001"],
            confidence=ConfidenceLabel.MEDIUM,
            confidence_basis=["one structured data point"],
        )
        final = FinalAnswer.from_synthesis(synthesis, packet, model="claude-test", prompt_version="test-v1")

        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {},
            "final_answer": final,
            "run_id": "test-run",
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post("/agent/synthesize", json={"question": "test question"})

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("test-run", body["run_id"])
        self.assertIsNotNone(body["final_answer"])
        self.assertEqual("Growth rate was 3.8 µm/hr [EV-S-001].", body["final_answer"]["synthesis"]["answer"])

    def test_synthesize_retrieval_question_runs_real_graph_with_injected_dependencies(self):
        from agent.graph import build_supervisor_agent as real_build_supervisor_agent

        fake_client = FakeAnthropicClient()

        def build_agent(**kwargs):
            return real_build_supervisor_agent(
                client=fake_client,
                model=kwargs["model"],
                vector_index=FakeVectorIndex(),
                keyword_index=FakeKeywordIndex(),
                chunk_store=FakeChunkStore(),
                query_embedder=FakeEmbedder(),
                reranker=FakeReranker(),
            )

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", side_effect=build_agent):
                with patch("app.api.routes_agent._get_document_store", return_value=FakeDocumentStore()):
                    response = self.client.post(
                        "/agent/synthesize",
                        json={
                            "question": "What does the retrieval paper say about CVD diamond growth?",
                        },
                    )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(["external_literature"], body["request_plan"]["knowledge_sources"])
        self.assertIn("external", body["document_evidence"])
        self.assertEqual(
            "The paper discusses microwave plasma assisted CVD diamond growth at high gas pressure. [EV-L-001]",
            body["final_answer"]["synthesis"]["answer"],
        )
        self.assertEqual(
            [
                {
                    "document_id": "doc-1",
                    "original_filename": "stored-paper.pdf",
                    "document_scope": "external",
                    "pdf_url": "/documents/doc-1/pdf",
                }
            ],
            body["source_documents"],
        )

    def test_synthesize_graph_failure_returns_503(self):
        fake_agent = MagicMock()
        fake_agent.invoke.side_effect = RuntimeError("retrieval dependency failed")

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post(
                    "/agent/synthesize",
                    json={"question": "What does retrieval say about diamond growth?"},
                )

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "Unable to complete the agent query. Backend logs include RuntimeError.",
            response.json()["detail"],
        )

    def test_query_returns_deduped_source_documents(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {
                "internal": {
                    "agent": "internal_document_agent",
                    "document_scope": "internal",
                    "contexts": [
                        {
                            "document_id": "doc-1",
                            "metadata": {"original_filename": "paper.pdf"},
                        },
                        {
                            "document_id": "doc-1",
                            "metadata": {"original_filename": "paper.pdf"},
                        },
                    ],
                }
            },
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent):
                response = self.client.post("/agent/query", json={"question": "Use internal reports"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [
                {
                    "document_id": "doc-1",
                    "original_filename": "paper.pdf",
                    "document_scope": "internal",
                    "pdf_url": "/documents/doc-1/pdf",
                }
            ],
            response.json()["source_documents"],
        )

    def test_query_source_documents_falls_back_to_stored_document_metadata(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {
                "internal": {
                    "agent": "internal_document_agent",
                    "document_scope": "internal",
                    "contexts": [{"document_id": "doc-1", "metadata": {}}],
                }
            },
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with (
                patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent),
                patch("app.api.routes_agent._get_document_store", return_value=FakeDocumentStore()),
            ):
                response = self.client.post("/agent/query", json={"question": "Use internal reports"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("stored-paper.pdf", response.json()["source_documents"][0]["original_filename"])

    def test_query_source_documents_keeps_document_id_when_metadata_lookup_fails(self):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            "request_plan": _ready_plan(),
            "database_evidence": {},
            "document_evidence": {
                "internal": {
                    "agent": "internal_document_agent",
                    "document_scope": "internal",
                    "contexts": [{"document_id": "doc-1", "metadata": {}}],
                }
            },
        }

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with (
                patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent),
                patch("app.api.routes_agent._get_document_store", side_effect=RuntimeError("missing config")),
            ):
                response = self.client.post("/agent/query", json={"question": "Use internal reports"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("doc-1", response.json()["source_documents"][0]["original_filename"])

    def test_query_schema_has_no_document_scope_routing_override(self):
        from app.api.routes_agent import DataQueryRequest

        self.assertNotIn("document_scopes", DataQueryRequest.model_fields)

    def test_query_stream_emits_progress_and_result_events(self):
        fake_agent = MagicMock()

        def fake_invoke(_state):
            callback_holder["progress"]("supervisor", "started")
            callback_holder["progress"]("supervisor", "completed")
            callback_holder["progress"]("bigquery", "started")
            callback_holder["progress"]("bigquery", "completed")
            return {
                "request_plan": {
                    "tasks": ["lookup"],
                    "knowledge_sources": ["structured_data"],
                    "status": "ready",
                    "reasons": [],
                },
                "database_evidence": {"agent": "database_agent"},
                "document_evidence": {},
            }

        callback_holder = {}

        def fake_build_supervisor_agent(**kwargs):
            callback_holder["progress"] = kwargs["progress_callback"]
            fake_agent.invoke.side_effect = fake_invoke
            return fake_agent

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with patch("app.api.routes_agent.build_supervisor_agent", side_effect=fake_build_supervisor_agent):
                response = self.client.post("/agent/query/stream", json={"question": "Raman stress"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/event-stream; charset=utf-8", response.headers["content-type"])
        events = _sse_events(response.text)
        self.assertEqual(
            [
                ("progress", {"stage": "supervisor", "state": "started"}),
                ("progress", {"stage": "supervisor", "state": "completed"}),
                ("progress", {"stage": "bigquery", "state": "started"}),
                ("progress", {"stage": "bigquery", "state": "completed"}),
            ],
            events[:4],
        )
        self.assertEqual("result", events[-1][0])
        self.assertEqual(
            {"run_id", "request_plan", "database_evidence", "document_evidence", "source_documents", "final_answer"},
            set(events[-1][1]),
        )

    def test_query_stream_emits_error_event_on_execution_failure(self):
        fake_agent = MagicMock()
        fake_agent.invoke.side_effect = RuntimeError("agent failed")

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with (
                patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent),
                patch("app.api.routes_agent.logger.exception") as log_exception,
            ):
                response = self.client.post("/agent/query/stream", json={"question": "Raman stress"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [("error", {"message": "Unable to complete the agent query."})],
            _sse_events(response.text),
        )
        log_exception.assert_called_once_with("Agent query failed during supervisor graph invocation.")

    def test_query_stream_emits_keepalive_while_agent_is_delayed(self):
        fake_agent = MagicMock()
        fake_agent.invoke.side_effect = lambda _state: (
            time.sleep(0.05)
            or {
                "request_plan": _ready_plan(),
                "database_evidence": {},
                "document_evidence": {},
            }
        )

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-test"}, clear=False):
            with (
                patch("app.api.routes_agent._SSE_HEARTBEAT_INTERVAL_SECONDS", 0.001),
                patch("app.api.routes_agent.build_supervisor_agent", return_value=fake_agent),
            ):
                response = self.client.post("/agent/query/stream", json={"question": "Raman stress"})

        self.assertEqual(200, response.status_code)
        self.assertIn(": keepalive\n\n", response.text)
        events = [event for event in _sse_events(response.text) if event[0]]
        self.assertEqual(
            (
                "result",
                {
                    "run_id": None,
                    "request_plan": _ready_plan(),
                    "database_evidence": {},
                    "document_evidence": {},
                    "source_documents": [],
                    "final_answer": None,
                },
            ),
            events[-1],
        )


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for record in body.strip().split("\n\n"):
        event_name = ""
        event_data = {}
        for line in record.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            if line.startswith("data: "):
                event_data = json.loads(line.removeprefix("data: "))
        events.append((event_name, event_data))
    return events


def _ready_plan() -> dict[str, object]:
    return {
        "tasks": ["lookup"],
        "knowledge_sources": ["general_knowledge"],
        "status": "ready",
        "reasons": [],
    }


if __name__ == "__main__":
    unittest.main()

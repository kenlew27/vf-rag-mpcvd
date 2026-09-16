"""Tests for app/api/routes_vector_search.py."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from storage.storage_contracts import ChunkRecord, VectorSearchHit
from tools.retrieval.bm25_index import BM25ChunkSearchIndex
from tools.retrieval.hybrid_retriever import KeywordSearchHit
from tools.retrieval.reranker import RerankScore


class FakeEmbedder:
    input_type = "query"
    model = "voyage-4"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_text(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.0] * 1024


class FakeVectorIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[list[float], int, dict[str, object] | None]] = []

    def search(self, vector, *, limit: int = 100, filters=None) -> list[VectorSearchHit]:
        self.calls.append((list(vector), limit, filters))
        return [
            VectorSearchHit("leaf-1", 0.1, {"level": "leaf", "tenant": "a"}),
            VectorSearchHit("leaf-2", 0.2, {"level": "leaf", "tenant": "a"}),
        ][:limit]


class FakeKeywordIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[str, object] | None]] = []

    def search(self, query_text: str, *, limit: int = 50, filters=None) -> list[KeywordSearchHit]:
        self.calls.append((query_text, limit, filters))
        return [KeywordSearchHit("leaf-2", 3.0, {"level": "leaf", "tenant": "a"})][:limit]


class FakeChunkStore:
    def __init__(self) -> None:
        self.chunks = {
            "parent": ChunkRecord(
                chunk_id="parent",
                document_id="doc-1",
                text="Expanded parent text",
                metadata={"title": "Diamond Growth"},
                artifact_uris={"pdf": "gs://bucket/source.pdf"},
            ),
            "leaf-1": ChunkRecord(
                chunk_id="leaf-1",
                document_id="doc-1",
                text="Leaf one text",
                metadata={"level": "leaf", "tenant": "a"},
                is_leaf=True,
                parent_chunk_id="parent",
            ),
            "leaf-2": ChunkRecord(
                chunk_id="leaf-2",
                document_id="doc-1",
                text="Leaf two text",
                metadata={"level": "leaf", "tenant": "a"},
                is_leaf=True,
                parent_chunk_id="parent",
            ),
        }

    def get_chunk_with_parent(self, chunk_id: str) -> tuple[ChunkRecord, ChunkRecord | None]:
        return self.get_chunks_with_parents([chunk_id])[chunk_id]

    def get_chunks_with_parents(self, chunk_ids) -> dict[str, tuple[ChunkRecord, ChunkRecord | None]]:
        results = {}
        for chunk_id in chunk_ids:
            if chunk_id not in self.chunks:
                continue
            chunk = self.chunks[chunk_id]
            results[chunk_id] = (chunk, self.chunks.get(chunk.parent_chunk_id))
        return results

    def count_child_chunks(self, parent_chunk_id: str) -> int:
        return self.count_child_chunks_many([parent_chunk_id]).get(parent_chunk_id, 0)

    def count_child_chunks_many(self, parent_chunk_ids) -> dict[str, int]:
        counts = {parent_chunk_id: 0 for parent_chunk_id in parent_chunk_ids}
        for chunk in self.chunks.values():
            if chunk.parent_chunk_id in counts:
                counts[chunk.parent_chunk_id] += 1
        return counts

    def get_chunks_by_ids(self, chunk_ids) -> list[ChunkRecord]:
        return [self.chunks[chunk_id] for chunk_id in chunk_ids if chunk_id in self.chunks]


class FakeReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query_text: str, candidates) -> list[RerankScore]:
        self.calls.append((query_text, [candidate.chunk_id for candidate in candidates]))
        scores = {"leaf-1": 0.4, "leaf-2": 0.9}
        return [RerankScore(candidate.chunk_id, scores[candidate.chunk_id]) for candidate in candidates]


class TestVectorSearchRoute(unittest.TestCase):
    def setUp(self) -> None:
        from app.main import app
        from app.api import routes_vector_search

        self.routes = routes_vector_search
        self.routes._reset_retrieval_dependencies_for_tests()
        self.addCleanup(self.routes._reset_retrieval_dependencies_for_tests)

        self.embedder = FakeEmbedder()
        self.vector_index = FakeVectorIndex()
        self.keyword_index = FakeKeywordIndex()
        self.chunk_store = FakeChunkStore()
        self.reranker = FakeReranker()

        self.patches = [
            patch.object(self.routes, "get_query_embedder", return_value=self.embedder),
            patch.object(self.routes, "get_vector_index", return_value=self.vector_index),
            patch.object(self.routes, "get_keyword_index", return_value=self.keyword_index),
            patch.object(self.routes, "get_chunk_store", return_value=self.chunk_store),
            patch.object(self.routes, "_get_route_reranker", return_value=self.reranker),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app)

    def test_query_request_returns_expanded_contexts(self) -> None:
        response = self.client.post(
            "/retrieval/vector-search",
            json={
                "query": "  diamond   Raman boron doping  ",
                "limit": 10,
                "filters": {"tenant": "a"},
                "document_scopes": ["external"],
            },
        )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual({"text": "diamond Raman boron doping", "embedding_model": "voyage-4"}, body["query"])
        self.assertEqual(
            [
                {
                    "context_id": "parent",
                    "document_id": "doc-1",
                    "text": "Expanded parent text",
                    "score": 0.9,
                    "source_leaf_chunk_ids": ["leaf-2", "leaf-1"],
                    "expansion_type": "parent",
                    "parent_chunk_id": "",
                    "metadata": {"title": "Diamond Growth"},
                    "artifact_uris": {"pdf": "gs://bucket/source.pdf"},
                }
            ],
            body["contexts"],
        )

    def test_rejects_blank_query_without_embedding_or_search(self) -> None:
        response = self.client.post("/retrieval/vector-search", json={"query": " \n\t "})

        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.embedder.calls)
        self.assertEqual([], self.vector_index.calls)
        self.assertEqual([], self.keyword_index.calls)
        self.assertEqual([], self.reranker.calls)

    def test_passes_normalized_query_to_keyword_search_and_reranker(self) -> None:
        response = self.client.post(
            "/retrieval/vector-search",
            json={"query": "diamond\n\nRaman", "limit": 2, "document_scopes": ["external"]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(["diamond Raman"], self.embedder.calls)
        self.assertEqual([("diamond Raman", 2, {})], self.keyword_index.calls)
        self.assertEqual([("diamond Raman", ["leaf-2", "leaf-1"])], self.reranker.calls)

    def test_caps_limit_and_passes_filters(self) -> None:
        filters = {"tenant": "a", "tags": ["diamond", "boron"]}

        response = self.client.post(
            "/retrieval/vector-search",
            json={"query": "diamond", "limit": 250, "filters": filters, "document_scopes": ["external"]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual([([0.0] * 1024, 50, filters)], self.vector_index.calls)
        self.assertEqual([("diamond", 50, filters)], self.keyword_index.calls)

    def test_document_ids_are_merged_into_filters(self) -> None:
        response = self.client.post(
            "/retrieval/vector-search",
            json={
                "query": "diamond",
                "limit": 3,
                "filters": {"tenant": "a"},
                "document_ids": ["doc-1", " doc-2 ", "doc-1", ""],
                "document_scopes": ["internal"],
            },
        )

        self.assertEqual(200, response.status_code)
        filters = {"tenant": "a", "document_id": ["doc-1", "doc-2"]}
        self.assertEqual([([0.0] * 1024, 3, filters)], self.vector_index.calls)
        self.assertEqual([("diamond", 3, filters)], self.keyword_index.calls)

    def test_unspecified_document_scopes_search_both(self) -> None:
        response = self.client.post("/retrieval/vector-search", json={"query": "diamond", "limit": 2})

        self.assertEqual(200, response.status_code)
        self.assertEqual([([0.0] * 1024, 2, {}), ([0.0] * 1024, 2, {})], self.vector_index.calls)
        self.assertEqual([("diamond", 2, {}), ("diamond", 2, {})], self.keyword_index.calls)


class TestVectorSearchDependencySingletons(unittest.TestCase):
    def setUp(self) -> None:
        from app.api import routes_vector_search

        self.routes = routes_vector_search
        self.routes._reset_retrieval_dependencies_for_tests()
        self.addCleanup(self.routes._reset_retrieval_dependencies_for_tests)

    def test_vector_index_downloads_snapshot_and_reuses_store(self) -> None:
        store = MagicMock()
        store.snapshot_uri = "gs://bucket/lancedb"
        client = object()

        with (
            patch.object(self.routes, "LanceDBVectorStore", return_value=store) as store_class,
            patch("google.cloud.storage.Client", return_value=client),
        ):
            self.assertIs(store, self.routes.get_vector_index("external"))
            self.assertIs(store, self.routes.get_vector_index("external"))

        store_class.assert_called_once_with(document_scope="external")
        store.download_snapshot.assert_called_once_with(client)

    def test_missing_vector_snapshot_returns_empty_store(self) -> None:
        store = MagicMock()
        store.snapshot_uri = "gs://bucket/lancedb"
        store.download_snapshot.side_effect = FileNotFoundError("missing manifest")
        client = object()

        with (
            patch.object(self.routes, "LanceDBVectorStore", return_value=store) as store_class,
            patch("google.cloud.storage.Client", return_value=client),
        ):
            self.assertIs(store, self.routes.get_vector_index("external"))
            self.assertIs(store, self.routes.get_vector_index("external"))

        store_class.assert_called_once_with(document_scope="external")
        store.download_snapshot.assert_called_once_with(client)

    def test_vector_snapshot_download_unexpected_error_still_raises(self) -> None:
        store = MagicMock()
        store.snapshot_uri = "gs://bucket/lancedb"
        store.download_snapshot.side_effect = RuntimeError("permission denied")

        with (
            patch.object(self.routes, "LanceDBVectorStore", return_value=store),
            patch("google.cloud.storage.Client", return_value=object()),
        ):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                self.routes.get_vector_index("external")

    def test_chunk_store_query_embedder_and_keyword_index_are_reused_and_reset(self) -> None:
        chunk_store = MagicMock()
        embedder = MagicMock()
        keyword_index = MagicMock()

        with (
            patch.object(self.routes, "BigQueryChunkStore", return_value=chunk_store) as chunk_store_class,
            patch.object(self.routes, "VoyageEmbedder", return_value=embedder) as embedder_class,
            patch.object(self.routes, "_load_keyword_index", return_value=keyword_index) as keyword_loader,
        ):
            self.assertIs(chunk_store, self.routes.get_chunk_store())
            self.assertIs(chunk_store, self.routes.get_chunk_store())
            self.assertIs(embedder, self.routes.get_query_embedder())
            self.assertIs(embedder, self.routes.get_query_embedder())
            self.assertIs(keyword_index, self.routes.get_keyword_index("external"))
            self.assertIs(keyword_index, self.routes.get_keyword_index("external"))

            self.routes._reset_retrieval_dependencies_for_tests()

            self.assertIs(chunk_store, self.routes.get_chunk_store())
            self.assertIs(embedder, self.routes.get_query_embedder())
            self.assertIs(keyword_index, self.routes.get_keyword_index("external"))

        self.assertEqual(2, chunk_store_class.call_count)
        self.assertEqual(2, embedder_class.call_count)
        self.assertEqual(2, keyword_loader.call_count)
        embedder_class.assert_called_with(input_type="query")
        keyword_loader.assert_called_with("external")

    def test_keyword_index_loads_configured_snapshot(self) -> None:
        loaded = MagicMock()

        with (
            patch.dict("os.environ", {"APP_BM25_SNAPSHOT_URI": "gs://bucket/bm25/index.json"}, clear=False),
            patch.object(self.routes, "load_bm25_snapshot", return_value=loaded) as loader,
        ):
            self.assertIs(loaded, self.routes._load_keyword_index("internal"))

        loader.assert_called_once_with("gs://bucket/bm25/index.json")

    def test_missing_scoped_keyword_snapshot_returns_empty_index(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "APP_GCS_BUCKET": "bucket",
                },
                clear=False,
            ),
            patch.object(self.routes, "load_bm25_snapshot", side_effect=FileNotFoundError) as loader,
        ):
            index = self.routes._load_keyword_index("internal")

        self.assertIsInstance(index, BM25ChunkSearchIndex)
        self.assertEqual([], index.search("diamond", limit=1))
        loader.assert_called_once_with("gs://bucket/bm25/internal.json")


if __name__ == "__main__":
    unittest.main()

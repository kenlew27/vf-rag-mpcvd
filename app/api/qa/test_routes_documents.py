"""
app/api/qa/test_routes_documents.py

Tests app/api/routes_documents.py. Docling is no longer imported by the route,
so no stub needed.
"""

import unittest
from unittest.mock import patch

from storage.storage_contracts import DocumentRecord


class FakeDocumentStore:
    def __init__(self) -> None:
        self.document = DocumentRecord(
            document_id="doc-1",
            source_uri="gs://bucket/parsed-pdf/doc-1/source.pdf",
            metadata={"document_scope": "internal", "original_filename": "paper.pdf"},
            ingest_status="embedded",
        )

    def get_document(self, document_id: str) -> DocumentRecord:
        if document_id != "doc-1":
            raise KeyError(document_id)
        return self.document

    def get_active_documents_by_filename_scope(self, *, original_filename: str, document_scope: str):
        if original_filename == "paper.pdf" and document_scope == "internal":
            return [self.document]
        return []

    def list_active_documents(self):
        return [self.document]


class TestUploadRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def test_healthz_lists_allowed_types(self):
        r = self.client.get("/documents/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertIn(".pdf", r.json()["allowed_types"])

    def test_valid_upload_returns_uploaded(self):
        r = self.client.post(
            "/documents/upload",
            files={"file": ("paper.pdf", b"fake pdf bytes", "application/pdf")},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertTrue(body["document_id"].startswith("DOC-"))
        self.assertIn("file_hash", body)
        self.assertIsNone(body["gcs_uri"])  # no bucket configured in tests

    def test_unsupported_type_rejected(self):
        r = self.client.post(
            "/documents/upload",
            files={"file": ("evil.exe", b"x", "application/octet-stream")},
        )
        self.assertEqual(r.status_code, 415)

    def test_empty_file_rejected(self):
        r = self.client.post(
            "/documents/upload",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        self.assertEqual(r.status_code, 400)

    def test_pdf_route_serves_inline_gcs_bytes(self):
        with (
            patch("app.api.routes_documents.BigQueryChunkStore", return_value=FakeDocumentStore()),
            patch("app.api.routes_documents.gcs.read_bytes", return_value=b"%PDF bytes") as read_bytes,
        ):
            r = self.client.get("/documents/doc-1/pdf")

        self.assertEqual(200, r.status_code)
        self.assertEqual("application/pdf", r.headers["content-type"])
        self.assertEqual('inline; filename="paper.pdf"', r.headers["content-disposition"])
        self.assertEqual(b"%PDF bytes", r.content)
        read_bytes.assert_called_once_with("gs://bucket/parsed-pdf/doc-1/source.pdf")

    def test_list_documents_returns_embedded_selector_records(self):
        with patch("app.api.routes_documents.BigQueryChunkStore", return_value=FakeDocumentStore()):
            r = self.client.get("/documents")

        self.assertEqual(200, r.status_code)
        self.assertEqual(
            [
                {
                    "document_id": "doc-1",
                    "original_filename": "paper.pdf",
                    "document_scope": "internal",
                    "pdf_url": "/documents/doc-1/pdf",
                    "ingest_status": "embedded",
                }
            ],
            r.json(),
        )

    def test_pdf_route_sanitizes_filename(self):
        store = FakeDocumentStore()
        store.document = DocumentRecord(
            document_id="doc-1",
            source_uri="gs://bucket/parsed-pdf/doc-1/source.pdf",
            metadata={"original_filename": '../bad"\r\nname.pdf'},
            ingest_status="embedded",
        )

        with (
            patch("app.api.routes_documents.BigQueryChunkStore", return_value=store),
            patch("app.api.routes_documents.gcs.read_bytes", return_value=b"%PDF bytes"),
        ):
            r = self.client.get("/documents/doc-1/pdf")

        self.assertEqual(200, r.status_code)
        self.assertEqual('inline; filename="bad_name.pdf"', r.headers["content-disposition"])

    def test_pdf_route_returns_404_when_gcs_pdf_missing(self):
        with (
            patch("app.api.routes_documents.BigQueryChunkStore", return_value=FakeDocumentStore()),
            patch("app.api.routes_documents.gcs.read_bytes", side_effect=FileNotFoundError("missing")),
        ):
            r = self.client.get("/documents/doc-1/pdf")

        self.assertEqual(404, r.status_code)

    def test_delete_document_resolves_filename_scope_and_deletes_artifacts(self):
        delete_result = type("DeleteResult", (), {"document_id": "doc-1", "chunk_count": 2, "deleted_gcs_objects": 3})()

        with (
            patch("app.api.routes_documents.BigQueryChunkStore", return_value=FakeDocumentStore()),
            patch("app.api.routes_ingestion.has_active_document_job", return_value=False),
            patch("app.api.routes_documents.delete_document_artifacts", return_value=delete_result) as delete_artifacts,
        ):
            r = self.client.request(
                "DELETE",
                "/documents",
                json={"original_filename": "paper.pdf", "document_scope": "internal"},
            )

        self.assertEqual(200, r.status_code)
        self.assertEqual("deleted", r.json()["status"])
        delete_artifacts.assert_called_once()

    def test_delete_document_conflicts_during_matching_ingestion_job(self):
        with patch("app.api.routes_ingestion.has_active_document_job", return_value=True):
            r = self.client.request(
                "DELETE",
                "/documents",
                json={"original_filename": "paper.pdf", "document_scope": "internal"},
            )

        self.assertEqual(409, r.status_code)


if __name__ == "__main__":
    unittest.main()

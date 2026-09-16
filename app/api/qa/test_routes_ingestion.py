"""Fake-only contracts for streaming ingestion upload and SSE status delivery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from storage.fakes import FakeChunkStore
from storage.gcs import PayloadTooLargeError, PromotedPdf, StagedPdf
from storage.ingestion_jobs import IngestionJob
from storage.storage_contracts import DocumentRecord


@dataclass(frozen=True)
class FakeEvent:
    event_id: int
    job_id: str
    payload: dict[str, object]


@dataclass(frozen=True)
class FakeBatch:
    batch_id: str
    owner_session_id: str
    expected_file_count: int
    status: str
    finalized_file_count: int = 0
    created_at: str | None = None
    sealed_at: str | None = None
    expires_at: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "owner_session_id": self.owner_session_id,
            "expected_file_count": self.expected_file_count,
            "finalized_file_count": self.finalized_file_count,
            "status": self.status,
            "created_at": self.created_at,
            "sealed_at": self.sealed_at,
            "expires_at": self.expires_at,
        }


class FakePostgresJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, IngestionJob] = {}
        self.events: dict[str, list[FakeEvent]] = {}
        self.batches: dict[str, FakeBatch] = {}

    def create_or_reuse(self, job: IngestionJob) -> SimpleNamespace:
        existing = self.find_by_hash_scope(job.content_hash, job.document_scope)
        if existing is not None:
            return SimpleNamespace(job=existing, created=False, other_scope_conflict=False)
        if self.has_other_scope_reference(job.content_hash, job.document_scope):
            return SimpleNamespace(job=None, created=False, other_scope_conflict=True)
        self.jobs[job.job_id] = job
        self.events.setdefault(job.job_id, []).append(FakeEvent(1, job.job_id, job.snapshot()))
        return SimpleNamespace(job=job, created=True, other_scope_conflict=False)

    def get(self, job_id: str) -> IngestionJob:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise KeyError(job_id) from None

    def list(self, *, active_only: bool = False) -> list[IngestionJob]:
        jobs = list(self.jobs.values())
        if active_only:
            jobs = [job for job in jobs if job.status in {"queued", "running", "paused_quota", "cancel_requested"}]
        return jobs

    def find_by_hash_scope(self, content_hash: str, document_scope: str) -> IngestionJob | None:
        return next(
            (
                job
                for job in self.jobs.values()
                if job.content_hash == content_hash and job.document_scope == document_scope
                and job.status in {"queued", "running", "paused_quota", "cancel_requested", "completed"}
            ),
            None,
        )

    def has_other_scope_reference(self, content_hash: str, document_scope: str) -> bool:
        return any(
            job.content_hash == content_hash
            and job.document_scope != document_scope
            and job.status not in {"cancelled", "failed"}
            for job in self.jobs.values()
        )

    def request_cancel(self, job_id: str) -> IngestionJob:
        job = self.get(job_id)
        replacement = IngestionJob(**{**job.__dict__, "status": "cancelled" if job.status == "queued" else "cancel_requested"})
        self.jobs[job_id] = replacement
        return replacement

    def resume_paused_jobs(self) -> int:
        resumed = 0
        for job_id, job in list(self.jobs.items()):
            if job.status == "paused_quota":
                self.jobs[job_id] = IngestionJob(**{**job.__dict__, "status": "queued"})
                resumed += 1
        return resumed

    def queue_status(self) -> dict[str, object]:
        paused = next((job for job in self.jobs.values() if job.status == "paused_quota"), None)
        return {
            "queued_count": sum(job.status == "queued" for job in self.jobs.values()),
            "active_count": sum(job.status in {"queued", "running", "paused_quota", "cancel_requested"} for job in self.jobs.values()),
            "paused": paused is not None,
            "provider": paused.provider if paused else None,
            "reason": paused.error if paused else None,
        }

    def get_events_after(self, job_id: str, last_event_id: int = 0) -> list[FakeEvent]:
        return [event for event in self.events.get(job_id, []) if event.event_id > last_event_id]

    def create_batch(self, *, owner_session_id: str, expected_file_count: int) -> FakeBatch:
        batch_id = f"BATCH-{len(self.batches) + 1}"
        batch = FakeBatch(
            batch_id=batch_id,
            owner_session_id=owner_session_id,
            expected_file_count=expected_file_count,
            status="open",
        )
        self.batches[batch_id] = batch
        return batch

    def seal_batch(self, batch_id: str) -> FakeBatch:
        batch = self.batches[batch_id]
        finalized = sum(job.batch_id == batch_id for job in self.jobs.values())
        sealed = FakeBatch(
            batch_id=batch.batch_id,
            owner_session_id=batch.owner_session_id,
            expected_file_count=batch.expected_file_count,
            status="ready" if finalized else "abandoned",
            finalized_file_count=finalized,
        )
        self.batches[batch_id] = sealed
        return sealed

    def attach_job_to_batch(self, job_id: str, batch_id: str) -> IngestionJob:
        if batch_id not in self.batches:
            raise KeyError(batch_id)
        job = self.get(job_id)
        if job.batch_id is not None and job.batch_id != batch_id:
            raise ValueError("Ingestion job is already attached to another batch.")
        replacement = IngestionJob(**{**job.__dict__, "batch_id": batch_id})
        self.jobs[job_id] = replacement
        return replacement

    def delete_failed_job(self, job_id: str, *, delete_raw_pdf: bool = False) -> bool:
        del delete_raw_pdf
        job = self.get(job_id)
        if job.status != "failed":
            raise ValueError("Only failed terminal ingestion jobs can be deleted.")
        del self.jobs[job_id]
        return True


class FakeStatusFanout:
    @asynccontextmanager
    async def subscribe(self, _job_id: str):
        import asyncio

        yield asyncio.Queue()


class TestIngestionRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from app.main import app

        cls.client = TestClient(app)

    def setUp(self) -> None:
        self.jobs = FakePostgresJobs()
        self.chunks = FakeChunkStore()
        self.store_patch = patch("app.api.routes_ingestion._job_store", return_value=self.jobs)
        self.chunk_patch = patch("app.api.routes_ingestion._chunk_store_or_none", return_value=self.chunks)
        self.configured_patch = patch("app.api.routes_ingestion.gcs.is_configured", return_value=True)
        self.store_patch.start()
        self.chunk_patch.start()
        self.configured_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(self.chunk_patch.stop)
        self.addCleanup(self.configured_patch.stop)

    @staticmethod
    def _staged(upload_id: str = "upload-1", data: bytes = b"fake pdf") -> StagedPdf:
        import hashlib

        return StagedPdf(
            upload_id=upload_id,
            content_hash=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            staging_gcs_uri=f"gs://bucket/staging/{upload_id}.pdf",
            generation=9,
            original_filename="paper.pdf",
            document_scope="internal",
        )

    def _promoted(self, upload_id: str = "upload-1", data: bytes = b"fake pdf") -> PromotedPdf:
        staged = self._staged(upload_id, data)
        return PromotedPdf(
            content_hash=staged.content_hash,
            size_bytes=staged.size_bytes,
            raw_gcs_uri=f"gs://bucket/raw/{staged.content_hash}.pdf",
            generation=12,
            original_filename=staged.original_filename,
            document_scope=staged.document_scope,
            staging_gcs_uri=staged.staging_gcs_uri,
            staging_generation=staged.generation,
            first_scope="internal",
        )

    def test_content_upload_streams_to_staging_without_creating_a_job(self) -> None:
        staged = self._staged()

        async def stream_and_check(_upload_id, chunks, **_kwargs):
            self.assertEqual([b"fake pdf"], [chunk async for chunk in chunks if chunk])
            return staged

        with patch("app.api.routes_ingestion.gcs.stream_staging_pdf", new=AsyncMock(side_effect=stream_and_check)) as stream:
            response = self.client.put(
                "/ingestion/uploads/upload-1/content?document_scope=internal&filename=paper.pdf",
                content=b"fake pdf",
                headers={"content-type": "application/pdf"},
            )

        self.assertEqual(202, response.status_code)
        self.assertEqual("staged", response.json()["status"])
        self.assertEqual({}, self.jobs.jobs)
        stream.assert_awaited_once()

    def test_finalize_promotes_then_creates_job_and_deletes_exact_staging_generation(self) -> None:
        promoted = self._promoted()
        with (
            patch("app.api.routes_ingestion.gcs.promote_staged_pdf", return_value=promoted) as promote,
            patch("app.api.routes_ingestion.gcs.delete_generation") as delete,
        ):
            response = self.client.post("/ingestion/uploads/upload-1/finalize", json={"job_id": "JOB-new"})

        self.assertEqual(202, response.status_code)
        self.assertEqual("queued", response.json()["status"])
        self.assertEqual("awaiting_worker", response.json()["stage"])
        self.assertEqual("JOB-new", response.json()["job_id"])
        promote.assert_called_once_with("upload-1")
        delete.assert_called_once_with(promoted.staging_gcs_uri, promoted.staging_generation)

    def test_create_batch_returns_public_snapshot(self) -> None:
        response = self.client.post(
            "/ingestion/batches",
            json={"owner_session_id": "session-1", "expected_file_count": 2},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "batch_id": "BATCH-1",
                "owner_session_id": "session-1",
                "expected_file_count": 2,
                "finalized_file_count": 0,
                "status": "open",
                "created_at": None,
                "sealed_at": None,
                "expires_at": None,
            },
            response.json(),
        )

    def test_finalize_attaches_created_job_to_batch(self) -> None:
        promoted = self._promoted()
        batch = self.jobs.create_batch(owner_session_id="session-1", expected_file_count=1)
        with patch("app.api.routes_ingestion.gcs.promote_staged_pdf", return_value=promoted), patch(
            "app.api.routes_ingestion.gcs.delete_generation"
        ):
            response = self.client.post(
                "/ingestion/uploads/upload-1/finalize",
                json={"job_id": "JOB-batched", "batch_id": batch.batch_id},
            )

        self.assertEqual(202, response.status_code)
        self.assertEqual(batch.batch_id, response.json()["batch_id"])
        self.assertEqual(batch.batch_id, self.jobs.get("JOB-batched").batch_id)

    def test_seal_batch_marks_ready_or_abandoned_by_finalized_files(self) -> None:
        empty_batch = self.jobs.create_batch(owner_session_id="session-1", expected_file_count=1)
        ready_batch = self.jobs.create_batch(owner_session_id="session-1", expected_file_count=1)
        self.jobs.jobs["JOB-ready"] = IngestionJob(
            job_id="JOB-ready",
            batch_id=ready_batch.batch_id,
            content_hash="hash",
            raw_gcs_uri="gs://bucket/raw/hash.pdf",
            document_scope="internal",
            original_filename="paper.pdf",
            size_bytes=8,
        )

        empty_response = self.client.post(f"/ingestion/batches/{empty_batch.batch_id}/seal")
        ready_response = self.client.post(f"/ingestion/batches/{ready_batch.batch_id}/seal")

        self.assertEqual(200, empty_response.status_code)
        self.assertEqual("abandoned", empty_response.json()["status"])
        self.assertEqual(0, empty_response.json()["finalized_file_count"])
        self.assertEqual(200, ready_response.status_code)
        self.assertEqual("ready", ready_response.json()["status"])
        self.assertEqual(1, ready_response.json()["finalized_file_count"])

    def test_content_upload_rejects_empty_and_over_limit_bodies(self) -> None:
        empty = StagedPdf(
            upload_id="upload-1",
            content_hash="empty",
            size_bytes=0,
            staging_gcs_uri="gs://bucket/staging/upload-1.pdf",
            generation=9,
            original_filename="paper.pdf",
            document_scope="internal",
        )
        with patch("app.api.routes_ingestion.gcs.stream_staging_pdf", new=AsyncMock(return_value=empty)), patch(
            "app.api.routes_ingestion.gcs.delete_generation"
        ):
            empty_response = self.client.put(
                "/ingestion/uploads/upload-1/content?document_scope=internal&filename=paper.pdf",
                content=b"",
            )
        with patch(
            "app.api.routes_ingestion.gcs.stream_staging_pdf",
            new=AsyncMock(side_effect=PayloadTooLargeError("too large")),
        ):
            oversized_response = self.client.put(
                "/ingestion/uploads/upload-1/content?document_scope=internal&filename=paper.pdf",
                content=b"large",
            )

        self.assertEqual(400, empty_response.status_code)
        self.assertEqual(413, oversized_response.status_code)

    def test_finalize_reuses_same_scope_job(self) -> None:
        promoted = self._promoted()
        self.jobs.create_or_reuse(
            IngestionJob(
                job_id="JOB-first",
                content_hash=promoted.content_hash,
                raw_gcs_uri=promoted.raw_gcs_uri,
                document_scope="internal",
                original_filename="paper.pdf",
                size_bytes=promoted.size_bytes,
                stage="awaiting_worker",
                status="queued",
            )
        )
        with patch("app.api.routes_ingestion.gcs.promote_staged_pdf", return_value=promoted), patch(
            "app.api.routes_ingestion.gcs.delete_generation"
        ):
            response = self.client.post("/ingestion/uploads/upload-1/finalize", json={"job_id": "JOB-second"})

        self.assertEqual(202, response.status_code)
        self.assertEqual("JOB-first", response.json()["job_id"])
        self.assertEqual(1, len(self.jobs.jobs))

    def test_finalize_rejects_cross_scope_duplicate(self) -> None:
        promoted = self._promoted()
        self.jobs.create_or_reuse(
            IngestionJob(
                job_id="JOB-internal",
                content_hash=promoted.content_hash,
                raw_gcs_uri=promoted.raw_gcs_uri,
                document_scope="external",
                original_filename="paper.pdf",
                size_bytes=promoted.size_bytes,
            )
        )
        with patch("app.api.routes_ingestion.gcs.promote_staged_pdf", return_value=promoted):
            response = self.client.post("/ingestion/uploads/upload-1/finalize", json={"job_id": "JOB-external"})

        self.assertEqual(409, response.status_code)
        self.assertIn("another document scope", response.json()["detail"])

    def test_known_document_returns_completed_without_reprocessing(self) -> None:
        promoted = self._promoted()
        self.chunks.put_document(
            DocumentRecord(
                document_id=promoted.content_hash,
                source_uri=promoted.raw_gcs_uri,
                metadata={"document_scope": "internal", "original_filename": "paper.pdf"},
                ingest_status="embedded",
            )
        )
        with patch("app.api.routes_ingestion.gcs.promote_staged_pdf", return_value=promoted), patch(
            "app.api.routes_ingestion.gcs.delete_generation"
        ):
            response = self.client.post("/ingestion/uploads/upload-1/finalize", json={"job_id": "JOB-known"})

        self.assertEqual(202, response.status_code)
        self.assertEqual("completed", response.json()["status"])
        self.assertEqual("finalizing", response.json()["stage"])

    def test_delete_failed_job_only_allows_failed_terminal_jobs(self) -> None:
        self.jobs.jobs["JOB-failed"] = IngestionJob(
            job_id="JOB-failed",
            content_hash="hash-failed",
            raw_gcs_uri="gs://bucket/raw/hash-failed.pdf",
            document_scope="internal",
            original_filename="failed.pdf",
            size_bytes=8,
            status="failed",
        )
        self.jobs.jobs["JOB-queued"] = IngestionJob(
            job_id="JOB-queued",
            content_hash="hash-queued",
            raw_gcs_uri="gs://bucket/raw/hash-queued.pdf",
            document_scope="internal",
            original_filename="queued.pdf",
            size_bytes=8,
            status="queued",
        )

        queued_response = self.client.delete("/ingestion/jobs/JOB-queued/failed")
        with patch("app.api.routes_ingestion.gcs.delete_prefix") as delete_prefix:
            failed_response = self.client.delete("/ingestion/jobs/JOB-failed/failed")

        self.assertEqual(409, queued_response.status_code)
        self.assertEqual(200, failed_response.status_code)
        self.assertEqual({"job_id": "JOB-failed", "deleted": True}, failed_response.json())
        self.assertNotIn("JOB-failed", self.jobs.jobs)
        delete_prefix.assert_called_once_with("gs://bucket/parsed-pdf/hash-failed")

    def test_status_stream_sends_initial_snapshot_and_replays_after_last_event_id(self) -> None:
        job = IngestionJob(
            job_id="JOB-events",
            content_hash="hash",
            raw_gcs_uri="gs://bucket/raw/hash.pdf",
            document_scope="internal",
            original_filename="paper.pdf",
            size_bytes=8,
            stage="awaiting_worker",
            status="queued",
        )
        self.jobs.jobs[job.job_id] = job
        self.jobs.events[job.job_id] = [
            FakeEvent(3, job.job_id, {"job_id": job.job_id, "status": "running", "stage": "parsing"}),
            FakeEvent(4, job.job_id, {"job_id": job.job_id, "status": "completed", "stage": "finalizing"}),
        ]
        with patch("app.api.routes_ingestion._status_fanout", return_value=FakeStatusFanout()):
            response = self.client.get(f"/ingestion/jobs/{job.job_id}/events", headers={"Last-Event-ID": "3"})

        self.assertEqual(200, response.status_code)
        self.assertIn('"job_id":"JOB-events"', response.text)
        self.assertIn('"batch_id":null', response.text)
        self.assertIn('"status":"queued"', response.text)
        self.assertIn('id: 4\ndata: {"job_id":"JOB-events","status":"completed"', response.text)

    def test_validation_rejects_invalid_streaming_input(self) -> None:
        self.assertEqual(
            400,
            self.client.put("/ingestion/uploads/upload-1/content?document_scope=partner&filename=paper.pdf", content=b"pdf").status_code,
        )
        self.assertEqual(
            415,
            self.client.put("/ingestion/uploads/upload-1/content?document_scope=internal&filename=paper.txt", content=b"pdf").status_code,
        )


if __name__ == "__main__":
    unittest.main()

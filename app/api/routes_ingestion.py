"""Server-owned PDF ingestion upload, queue, and status-event API."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, Protocol

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth import owner_session_id_for_request
import storage.gcs as gcs
from storage.bigquery import BigQueryChunkStore
from storage.ingestion_jobs import ACTIVE_JOB_STATUSES, DURABLE_REFERENCE_STATUSES, IngestionJob, IngestionJobStore
from storage.storage_contracts import DocumentRecord, ChunkStore
from tools.retrieval.document_scope import DocumentScope, normalize_document_scope


from app.api._constants import MAX_FILE_MB, MAX_FILE_BYTES, PDF_EXT

router = APIRouter(prefix="/ingestion", tags=["ingestion"])

_TERMINAL_EVENT_STATUSES = frozenset({"completed", "embedded", "failed", "cancelled", "paused_quota"})


class FinalizeIngestionUpload(BaseModel):
    """Optional public identifier for an idempotent finalize request."""

    job_id: str | None = None
    batch_id: str | None = None


class CreateIngestionBatch(BaseModel):
    owner_session_id: str
    expected_file_count: int


class JobStatusFanoutProtocol(Protocol):
    def subscribe(self, job_id: str) -> Any: ...


class JobStatusFanout:
    """Fan one blocking PostgreSQL LISTEN connection out to local SSE clients.

    The outbox is the source of truth. Notifications are only wakeups, so a full
    queue never drops a state transition: the SSE handler always reads the outbox
    after a wakeup and on its initial connection.
    """

    def __init__(self, listener_factory: Callable[[], Iterator[str]]) -> None:
        self._listener_factory = listener_factory
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._current_listener: Iterator[str] | None = None

    def _ensure_listener(self) -> None:
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._listen_forever())

    def start(self) -> None:
        self._ensure_listener()

    async def aclose(self) -> None:
        """Stop the listener task and unblock its pending database wait."""
        listener = self._current_listener
        closer = getattr(listener, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        task = self._listener_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
        self._current_listener = None

    @asynccontextmanager
    async def subscribe(self, job_id: str) -> AsyncIterator[asyncio.Queue[None]]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.setdefault(job_id, set()).add(queue)
        self._ensure_listener()
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(job_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(job_id, None)

    async def _listen_forever(self) -> None:
        while True:
            listener: Iterator[str] | None = None
            try:
                listener = self._listener_factory()
                self._current_listener = listener
                while True:
                    notification = await asyncio.to_thread(_next_notification, listener)
                    if notification is None:
                        break
                    self.publish(notification)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A reconnect is enough because the durable outbox fills any gap.
                await asyncio.sleep(1)
            finally:
                if self._current_listener is listener:
                    self._current_listener = None
                closer = getattr(listener, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

    def publish(self, job_id: str) -> None:
        for queue in tuple(self._subscribers.get(job_id, ())):
            if queue.full():
                continue
            queue.put_nowait(None)


def _next_notification(listener: Iterator[str]) -> str | None:
    try:
        return next(listener)
    except StopIteration:
        return None


_job_status_fanout: JobStatusFanout | None = None


def _status_fanout() -> JobStatusFanoutProtocol:
    global _job_status_fanout
    if _job_status_fanout is None:
        _job_status_fanout = JobStatusFanout(_listen_job_status)
    return _job_status_fanout


def start_job_status_listener() -> None:
    """Start the process-local LISTEN loop from the API lifespan hook."""
    fanout = _status_fanout()
    if isinstance(fanout, JobStatusFanout):
        fanout.start()


async def stop_job_status_listener() -> None:
    """Stop the API lifespan listener without leaving a blocked DB thread behind."""
    global _job_status_fanout
    fanout = _job_status_fanout
    if fanout is not None:
        await fanout.aclose()
    _job_status_fanout = None


def _listen_job_status() -> Iterator[str]:
    listener = getattr(_job_store(), "listen_job_status", None)
    if not callable(listener):
        raise RuntimeError("The configured ingestion store does not support PostgreSQL LISTEN.")
    return iter(listener())


@router.put("/uploads/{upload_id}/content", status_code=status.HTTP_202_ACCEPTED)
async def upload_ingestion_content(
    upload_id: str,
    request: Request,
    document_scope: str = Query(...),
    filename: str = Query(...),
) -> dict[str, Any]:
    """Stream a PDF into generation-addressed GCS staging storage.

    ``stream_staging_pdf`` owns the resumable GCS writer and must consume the
    async request stream incrementally, enforce ``max_bytes``, and write the
    final SHA-256/size/scope/filename metadata only after the object completes.
    No queue row exists until ``finalize`` commits.
    """
    _validate_upload_id(upload_id)
    scope = _validate_document_scope(document_scope)
    _validate_pdf_filename(filename)
    if not gcs.is_configured():
        raise HTTPException(503, "APP_GCS_BUCKET env var is required for queued PDF ingestion.")

    try:
        staged = await gcs.stream_staging_pdf(
            upload_id,
            request.stream(),
            max_bytes=MAX_FILE_BYTES,
            original_filename=filename,
            document_scope=scope,
        )
    except gcs.PayloadTooLargeError as exc:
        raise HTTPException(413, f"File exceeds the V1 limit of {MAX_FILE_MB} MB.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if int(_metadata_value(staged, "size_bytes")) == 0:
        try:
            gcs.delete_generation(
                _metadata_value(staged, "staging_gcs_uri", "uri"),
                _metadata_value(staged, "generation"),
            )
        except Exception:
            pass
        raise HTTPException(400, "Uploaded file is empty.")

    return {
        "upload_id": upload_id,
        "status": "staged",
        "content_hash": _metadata_value(staged, "content_hash"),
        "size_bytes": _metadata_value(staged, "size_bytes"),
        "staging_gcs_uri": _metadata_value(staged, "staging_gcs_uri", "uri"),
        "generation": _metadata_value(staged, "generation"),
    }


@router.post("/uploads/{upload_id}/finalize", status_code=status.HTTP_202_ACCEPTED)
async def finalize_ingestion_upload(upload_id: str, payload: FinalizeIngestionUpload) -> dict[str, Any]:
    """Promote a completed staged object and atomically create/reuse its job."""
    _validate_upload_id(upload_id)
    resolved_job_id = _validate_job_id(payload.job_id) if payload.job_id else f"JOB-{uuid.uuid4().hex[:12]}"
    batch_id = _validate_job_id(payload.batch_id) if payload.batch_id else None
    if not gcs.is_configured():
        raise HTTPException(503, "APP_GCS_BUCKET env var is required for queued PDF ingestion.")

    try:
        promoted = gcs.promote_staged_pdf(upload_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Completed staged upload not found.") from exc
    except gcs.RawPdfScopeConflict as exc:
        raise HTTPException(409, "Identical PDF bytes already exist in another document scope.") from exc
    except gcs.LegacyRawPdfMetadataError:
        staged = gcs.load_staged_pdf(upload_id)
        promoted = {
            "content_hash": staged.content_hash,
            "size_bytes": staged.size_bytes,
            "raw_gcs_uri": gcs.raw_pdf_uri(staged.content_hash),
            "original_filename": staged.original_filename,
            "document_scope": staged.document_scope,
            "staging_gcs_uri": staged.staging_gcs_uri,
            "staging_generation": staged.generation,
            "first_scope": None,
            "legacy_raw": True,
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    content_hash = str(_metadata_value(promoted, "content_hash"))
    size_bytes = int(_metadata_value(promoted, "size_bytes"))
    raw_gcs_uri = str(_metadata_value(promoted, "raw_gcs_uri", "uri"))
    original_filename = str(_metadata_value(promoted, "original_filename"))
    scope = _validate_document_scope(str(_metadata_value(promoted, "document_scope")))
    first_scope = _metadata_value(promoted, "first_scope")
    if first_scope is not None and first_scope != scope:
        raise HTTPException(409, "Identical PDF bytes already exist in another document scope.")

    jobs = _job_store()
    if batch_id is not None:
        _required_store_method(jobs, "attach_job_to_batch", "ingestion batch attachment")
    chunk_store = _chunk_store_or_none()
    try:
        existing_by_id = jobs.get(resolved_job_id)
    except KeyError:
        existing_by_id = None
    if existing_by_id is not None:
        if existing_by_id.content_hash == content_hash and existing_by_id.document_scope == scope:
            if batch_id is not None:
                existing_by_id = _attach_job_to_batch(jobs, existing_by_id.job_id, batch_id)
            _delete_staged_generation(upload_id, promoted)
            return existing_by_id.snapshot()
        raise HTTPException(409, "Ingestion job ID is already used for another upload.")

    _reject_cross_scope_duplicate(chunk_store, content_hash, scope)
    if jobs.has_other_scope_reference(content_hash, scope):
        raise HTTPException(409, "Identical PDF bytes already exist in another document scope.")
    known_document: DocumentRecord | None = None
    if chunk_store is not None:
        try:
            known_document = chunk_store.get_document(content_hash)
        except KeyError:
            known_document = None
    if _metadata_value(promoted, "legacy_raw", default=False):
        # A pre-cutover raw object cannot carry its historical scope.  Existing
        # BigQuery document metadata is the compatibility source until backfill.
        if known_document is None:
            raise HTTPException(409, "Existing raw PDF needs immutable metadata backfilled before ingestion.") from None
        if known_document.metadata.get("document_scope") != scope:
            raise HTTPException(409, "Identical PDF bytes already exist in another document scope.")
    known_embedded_document = known_document if known_document and known_document.ingest_status == "embedded" else None
    replacement_document = None if known_embedded_document else _resolve_active_document(
        chunk_store,
        original_filename=original_filename,
        document_scope=scope,
    )

    job = IngestionJob(
        job_id=resolved_job_id,
        content_hash=content_hash,
        raw_gcs_uri=raw_gcs_uri,
        document_scope=scope,
        original_filename=original_filename,
        size_bytes=size_bytes,
        replaces_document_id=replacement_document.document_id if replacement_document else None,
        stage="finalizing" if known_embedded_document else "awaiting_worker",
        status="completed" if known_embedded_document else "queued",
    )
    try:
        result = jobs.create_or_reuse(job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if bool(_metadata_value(result, "other_scope_conflict", default=False)):
        raise HTTPException(409, "Identical PDF bytes already exist in another document scope.")

    created = _metadata_value(result, "job", default=result)
    if batch_id is not None:
        created = _attach_job_to_batch(jobs, created.job_id, batch_id)
    # The job/outbox/NOTIFY transaction has committed. A failed cleanup is safe:
    # the exact staging generation remains lifecycle-managed and finalize is idempotent.
    _delete_staged_generation(upload_id, promoted)
    return created.snapshot()


@router.post("/batches")
def create_ingestion_batch(payload: CreateIngestionBatch, request: Request) -> dict[str, Any]:
    owner_session_id = _validate_owner_session_id(owner_session_id_for_request(request, payload.owner_session_id))
    if payload.expected_file_count < 0:
        raise HTTPException(400, "expected_file_count must be non-negative.")
    method = _required_store_method(_job_store(), "create_batch", "ingestion batches")
    return _batch_snapshot(method(owner_session_id=owner_session_id, expected_file_count=payload.expected_file_count))


@router.post("/batches/{batch_id}/seal")
def seal_ingestion_batch(batch_id: str) -> dict[str, Any]:
    _validate_job_id(batch_id)
    method = _required_store_method(_job_store(), "seal_batch", "ingestion batches")
    try:
        return _batch_snapshot(method(batch_id))
    except KeyError as exc:
        raise HTTPException(404, "Ingestion batch not found.") from exc


@router.get("/jobs/{job_id}/events")
async def stream_ingestion_job_events(job_id: str, request: Request) -> StreamingResponse:
    """Replay durable job events, then wait for committed PostgreSQL wakeups."""
    _validate_job_id(job_id)
    last_event_header = request.headers.get("last-event-id")
    after_event_id = _last_event_id(last_event_header)

    async def event_stream() -> AsyncIterator[str]:
        last_seen = after_event_id
        store = _job_store()
        async with _status_fanout().subscribe(job_id) as wakeups:
            try:
                if last_event_header is None:
                    # A new browser gets the current snapshot, not stale paused
                    # or completed transitions from a prior incarnation.  The
                    # cursor is still advanced to the durable outbox head.
                    history = _get_events_after(store, job_id, 0)
                    last_seen = max((int(_metadata_value(event, "event_id")) for event in history), default=0)
                initial = store.get(job_id)
            except KeyError:
                yield _sse_payload({"error": "Ingestion job not found."})
                return

            # Subscribe before snapshot/replay so a transition cannot be missed.
            yield _sse_payload(initial.snapshot())

            while True:
                events = _get_events_after(store, job_id, last_seen)
                for event in events:
                    event_id = int(_metadata_value(event, "event_id"))
                    if event_id <= last_seen:
                        continue
                    event_payload = _event_payload(event, store, job_id)
                    yield _sse_payload(event_payload, event_id=event_id)
                    last_seen = event_id
                    if _is_event_terminal(event_payload):
                        return

                if _is_event_terminal(initial.snapshot()):
                    return

                if await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(wakeups.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs")
def list_ingestion_jobs(active: bool = False) -> dict[str, list[dict[str, Any]]]:
    return {"jobs": [job.snapshot() for job in _job_store().list(active_only=active)]}


@router.get("/queue")
def ingestion_queue_status() -> dict[str, Any]:
    return _queue_snapshot(_job_store().queue_status())


@router.post("/queue/resume")
def resume_ingestion_queue() -> dict[str, Any]:
    jobs = _job_store()
    jobs.resume_paused_jobs()
    return _queue_snapshot(jobs.queue_status())


@router.get("/jobs/{job_id}")
def get_ingestion_job(job_id: str) -> dict[str, Any]:
    _validate_job_id(job_id)
    try:
        return _job_store().get(job_id).snapshot()
    except KeyError as exc:
        raise HTTPException(404, "Ingestion job not found.") from exc


@router.delete("/jobs/{job_id}")
def cancel_ingestion_job(job_id: str) -> dict[str, Any]:
    _validate_job_id(job_id)
    try:
        return _job_store().request_cancel(job_id).snapshot()
    except KeyError as exc:
        raise HTTPException(404, "Ingestion job not found.") from exc


@router.delete("/jobs/{job_id}/failed")
def delete_failed_ingestion_job(job_id: str) -> dict[str, Any]:
    _validate_job_id(job_id)
    jobs = _job_store()
    method = _required_store_method(jobs, "delete_failed_job", "failed ingestion job deletion")
    try:
        job = jobs.get(job_id)
        if job.status != "failed":
            raise ValueError("Only failed terminal ingestion jobs can be deleted.")
        if not _has_durable_content_reference(jobs, job):
            _delete_failed_job_artifacts(job)
        deleted = method(job_id, delete_raw_pdf=True)
    except KeyError as exc:
        raise HTTPException(404, "Ingestion job not found.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if deleted is False or getattr(deleted, "deleted", True) is False:
        raise HTTPException(409, "Only failed terminal ingestion jobs can be deleted.")
    return {"job_id": job_id, "deleted": True}


def has_active_document_job(*, original_filename: str, document_scope: str) -> bool:
    """Document deletion guard; unavailable queue configuration stays non-blocking."""
    try:
        return any(
            job.original_filename == original_filename
            and job.document_scope == document_scope
            and job.status in ACTIVE_JOB_STATUSES
            for job in _job_store().list(active_only=True)
        )
    except RuntimeError:
        return False


def _validate_document_scope(value: str) -> DocumentScope:
    try:
        return normalize_document_scope(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_pdf_filename(filename: str | None) -> None:
    if not filename:
        raise HTTPException(400, "No filename provided.")
    if Path(filename).suffix.lower() != PDF_EXT:
        raise HTTPException(415, "Only PDF uploads are supported.")


def _validate_upload_id(upload_id: str) -> str:
    return _validate_job_id(upload_id)


def _validate_owner_session_id(owner_session_id: str) -> str:
    value = owner_session_id.strip()
    if not value:
        raise HTTPException(400, "owner_session_id must not be empty.")
    if len(value) > 254 or any(char.isspace() for char in value):
        raise HTTPException(400, "owner_session_id must be a compact identifier.")
    return value


def _validate_job_id(job_id: str) -> str:
    value = job_id.strip()
    if not value:
        raise HTTPException(400, "job_id must not be empty.")
    if len(value) > 80 or any(char.isspace() for char in value):
        raise HTTPException(400, "job_id must be a compact identifier.")
    return value


def _job_store() -> IngestionJobStore:
    backend = os.environ.get("APP_INGESTION_BACKEND", "postgres").strip().lower()
    if backend != "postgres":
        raise HTTPException(503, "APP_INGESTION_BACKEND must be postgres for durable ingestion.")
    try:
        from storage.ingestion_jobs import PostgresIngestionJobStore

        return PostgresIngestionJobStore()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc


def _queue_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "paused_quota" if value["paused"] else "running",
        "paused": value["paused"],
        "paused_provider": value["provider"],
        "paused_reason": value["reason"],
        "queued_count": value["queued_count"],
        "active_count": value["active_count"],
    }


def _batch_snapshot(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    snapshot = getattr(value, "snapshot", None)
    if callable(snapshot):
        return dict(snapshot())
    return dict(value.__dict__)


def _required_store_method(store: Any, name: str, capability: str) -> Any:
    method = getattr(store, name, None)
    if not callable(method):
        raise HTTPException(503, f"The configured ingestion store does not support {capability}.")
    return method


def _attach_job_to_batch(store: Any, job_id: str, batch_id: str) -> IngestionJob:
    method = _required_store_method(store, "attach_job_to_batch", "ingestion batch attachment")
    try:
        return method(job_id, batch_id)
    except KeyError as exc:
        raise HTTPException(404, "Ingestion batch not found.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _has_durable_content_reference(store: Any, job: IngestionJob) -> bool:
    return any(
        other.job_id != job.job_id
        and other.content_hash == job.content_hash
        and other.status in DURABLE_REFERENCE_STATUSES
        for other in store.list()
    )


def _delete_failed_job_artifacts(job: IngestionJob) -> None:
    chunk_store = _chunk_store_or_none()
    deleted_document = False
    if chunk_store is not None:
        try:
            chunk_store.get_document(job.content_hash)
        except KeyError:
            pass
        else:
            from workers.delete_document import delete_document_artifacts

            delete_document_artifacts(job.content_hash, document_scope=job.document_scope, chunk_store=chunk_store)
            deleted_document = True
    if not deleted_document:
        gcs.delete_prefix(_parsed_artifacts_prefix(job))


def _parsed_artifacts_prefix(job: IngestionJob) -> str:
    bucket, _ = gcs.parse_gcs_uri(job.raw_gcs_uri)
    return f"gs://{bucket}/parsed-pdf/{job.content_hash}"


def _chunk_store_or_none() -> ChunkStore | None:
    try:
        return BigQueryChunkStore()
    except RuntimeError:
        return None


def _resolve_active_document(
    chunk_store: ChunkStore | None,
    *,
    original_filename: str,
    document_scope: str,
) -> DocumentRecord | None:
    if chunk_store is None:
        return None
    active_documents = chunk_store.get_active_documents_by_filename_scope(
        original_filename=original_filename,
        document_scope=document_scope,
    )
    if len(active_documents) > 1:
        raise HTTPException(409, "Multiple active documents match filename and scope.")
    return active_documents[0] if active_documents else None


def _reject_cross_scope_duplicate(chunk_store: ChunkStore | None, document_id: str, document_scope: str) -> None:
    if chunk_store is None:
        return
    try:
        existing = chunk_store.get_document(document_id)
    except KeyError:
        return
    existing_scope = existing.metadata.get("document_scope")
    if existing_scope is not None and existing_scope != document_scope:
        raise HTTPException(409, "Identical PDF bytes already exist in another document scope.")


def _metadata_value(value: Any, *names: str, default: Any = ... ) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not ...:
        return default
    raise ValueError(f"Missing required ingestion metadata: {names[0]}")


def _delete_staged_generation(_upload_id: str, promoted: Any) -> None:
    try:
        gcs.delete_generation(
            _metadata_value(promoted, "staging_gcs_uri"),
            _metadata_value(promoted, "staging_generation"),
        )
    except Exception:
        return


def _last_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _get_events_after(store: Any, job_id: str, after_event_id: int) -> list[Any]:
    method = getattr(store, "get_events_after", None) or getattr(store, "events_after", None)
    if not callable(method):
        raise RuntimeError("The configured ingestion store does not support durable job events.")
    return list(method(job_id, after_event_id))


def _event_payload(event: Any, store: Any, job_id: str) -> dict[str, Any]:
    payload = _metadata_value(event, "payload", default={})
    if isinstance(payload, Mapping) and "status" in payload:
        return dict(payload)
    return store.get(job_id).snapshot()


def _sse_payload(payload: Mapping[str, Any], *, event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}data: {json.dumps(payload, default=str, separators=(',', ':'))}\n\n"


def _is_event_terminal(payload: Mapping[str, Any]) -> bool:
    return payload.get("status") in _TERMINAL_EVENT_STATUSES

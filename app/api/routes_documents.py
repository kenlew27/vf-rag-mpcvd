"""
app/api/routes_documents.py

FastAPI routes for document upload + ingestion.

Upload stores the raw file to GCS immediately and returns. Parsing is a
separate pipeline step (tools/pdf_parser/) triggered later.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import storage.gcs as gcs
from storage.bigquery import BigQueryChunkStore
from tools.retrieval.document_scope import normalize_document_scope
from workers.delete_document import delete_document_artifacts

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXT = {".pdf", ".docx", ".pptx", ".txt", ".md", ".csv", ".xlsx"}
MAX_FILE_MB = 50
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class DeleteDocumentRequest(BaseModel):
    original_filename: str
    document_scope: str


class DocumentListItem(BaseModel):
    document_id: str
    original_filename: str
    document_scope: str
    pdf_url: str
    ingest_status: str


def _validate_ext(filename: str) -> str:
    if not filename:
        raise HTTPException(400, "No filename provided.")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            415,
            f"File type '{ext or 'unknown'}' not supported in V1. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXT))}.",
        )
    return ext


def _safe_pdf_filename(document_id: str, metadata: object) -> str:
    original_filename = ""
    if isinstance(metadata, Mapping):
        original_filename = str(metadata.get("original_filename") or "").strip()

    stem = Path(original_filename).stem if original_filename else document_id
    filename = SAFE_FILENAME_RE.sub("_", stem).strip(" ._") or document_id
    return f"{filename}.pdf"


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    """Upload a document to GCS. Parsing happens in a separate pipeline step."""
    ext = _validate_ext(file.filename)

    data = await file.read()
    if len(data) == 0:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            413, f"File is {len(data)/1024/1024:.1f} MB; V1 limit is {MAX_FILE_MB} MB."
        )

    document_id = f"DOC-{uuid.uuid4().hex[:12]}"
    file_hash = hashlib.sha256(data).hexdigest()

    gcs_uri = gcs.put_raw(document_id, ext, data) if gcs.is_configured() else None

    return {
        "document_id": document_id,
        "original_filename": file.filename,
        "ext": ext,
        "size_bytes": len(data),
        "file_hash": file_hash,
        "gcs_uri": gcs_uri,
        "status": "uploaded",  # uploaded -> parsed -> indexed
    }


@router.get("/healthz")
def healthz():
    return {"ok": True, "allowed_types": sorted(ALLOWED_EXT), "max_mb": MAX_FILE_MB}


@router.get("")
def list_documents() -> list[DocumentListItem]:
    try:
        documents = BigQueryChunkStore().list_active_documents()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    return [
        DocumentListItem(
            document_id=document.document_id,
            original_filename=str(document.metadata.get("original_filename") or document.document_id),
            document_scope=str(document.metadata.get("document_scope") or ""),
            pdf_url=f"/documents/{document.document_id}/pdf",
            ingest_status=document.ingest_status,
        )
        for document in documents
    ]


@router.get("/{document_id}/pdf")
def get_document_pdf(document_id: str):
    try:
        document = BigQueryChunkStore().get_document(document_id)
    except KeyError as exc:
        raise HTTPException(404, "Document not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    if not document.source_uri.startswith("gs://"):
        raise HTTPException(404, "Document PDF is not available.")

    try:
        data = gcs.read_bytes(document.source_uri)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Document PDF is not available.") from exc
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc

    filename = _safe_pdf_filename(document.document_id, document.metadata)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.delete("")
def delete_document(req: DeleteDocumentRequest):
    filename = req.original_filename.strip()
    if not filename:
        raise HTTPException(400, "original_filename must not be empty.")
    try:
        scope = normalize_document_scope(req.document_scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    from app.api.routes_ingestion import has_active_document_job

    if has_active_document_job(original_filename=filename, document_scope=scope):
        raise HTTPException(409, "A matching document upload is active; retry after it finishes.")

    try:
        chunk_store = BigQueryChunkStore()
        matches = chunk_store.get_active_documents_by_filename_scope(
            original_filename=filename,
            document_scope=scope,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    if not matches:
        raise HTTPException(404, "Document not found.")
    if len(matches) > 1:
        raise HTTPException(409, "Multiple active documents match filename and scope.")

    result = delete_document_artifacts(matches[0].document_id, document_scope=scope, chunk_store=chunk_store)
    return {
        "document_id": result.document_id,
        "document_scope": scope,
        "original_filename": filename,
        "chunk_count": result.chunk_count,
        "deleted_gcs_objects": result.deleted_gcs_objects,
        "status": "deleted",
    }

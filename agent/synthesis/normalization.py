"""Normalize upstream evidence packets for synthesis planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from agent.synthesis.models import (
    DatabaseCompleteness,
    NormalizedDatabaseEvidence,
    NormalizedDocumentEvidence,
    NormalizedEvidenceBundle,
)

_SYNTHESIS_KEY_NAMES = {
    "confidence": "relative_relevance",
    "retrieval_score": "retrieval_score_raw",
    "result_summary": "non_evidence_summary",
    "metric_definitions_used": "filter_keys_used",
}


def normalize_evidence_packets(
    document_packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    database_packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    question: str | None = None,
) -> NormalizedEvidenceBundle:
    """Return synthesis-only evidence with stable IDs and renamed packet fields."""

    document_packet_list = _coerce_document_packets(document_packets)
    database_packet_list = _coerce_packet_list(database_packets)
    normalized_document_packets = [_rename_synthesis_keys(packet) for packet in document_packet_list]
    normalized_database_packets = [_rename_synthesis_keys(packet) for packet in database_packet_list]

    document_evidence: list[NormalizedDocumentEvidence] = []
    for packet_index, packet in enumerate(normalized_document_packets):
        source_agent = _optional_str(packet.get("agent"))
        packet_question = _optional_str(packet.get("question")) or question
        scope = _optional_str(packet.get("document_scope"))
        contexts = packet.get("contexts", [])
        if not isinstance(contexts, list):
            continue
        for context_index, context in enumerate(contexts):
            if not isinstance(context, dict):
                continue
            evidence_id = _document_evidence_id(scope, context, packet_index, context_index)
            document_evidence.append(
                NormalizedDocumentEvidence(
                    evidence_id=evidence_id,
                    source_agent=source_agent,
                    question=packet_question,
                    payload=context,
                    document_scope=scope,
                    context_id=_optional_str(context.get("context_id")),
                    document_id=_optional_str(context.get("document_id")),
                    relative_relevance=_optional_float(context.get("relative_relevance")),
                    retrieval_score_raw=_optional_float(context.get("retrieval_score_raw")),
                )
            )

    database_evidence: list[NormalizedDatabaseEvidence] = []
    for packet_index, packet in enumerate(normalized_database_packets):
        source_agent = _optional_str(packet.get("agent"))
        packet_question = _optional_str(packet.get("question")) or question
        rows = packet.get("evidence_rows", [])
        if not isinstance(rows, list):
            continue
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            row_copy = dict(row)
            evidence_id = _database_evidence_id(row_copy, packet_index, row_index)
            database_evidence.append(
                NormalizedDatabaseEvidence(
                    evidence_id=evidence_id,
                    source_agent=source_agent,
                    question=packet_question,
                    payload=packet,
                    row_index=row_index,
                    row=row_copy,
                )
            )

    completeness = _infer_database_completeness(normalized_database_packets)
    missing_evidence = []
    if not document_evidence:
        missing_evidence.append("document evidence")
    if not database_evidence:
        missing_evidence.append("database evidence")
    missing_evidence.extend(completeness.reasons)

    return NormalizedEvidenceBundle(
        question=question,
        document_packets=normalized_document_packets,
        database_packets=normalized_database_packets,
        document_evidence=document_evidence,
        database_evidence=database_evidence,
        database_completeness=completeness,
        missing_evidence=missing_evidence,
    )


def _coerce_document_packets(packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if packets is None:
        return []
    if isinstance(packets, Mapping):
        if _looks_like_packet(packets):
            return [dict(packets)]
        return [dict(packet) for packet in packets.values() if isinstance(packet, Mapping)]
    return _coerce_packet_list(packets)


def _coerce_packet_list(packets: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if packets is None:
        return []
    if isinstance(packets, Mapping):
        return [dict(packets)] if packets else []
    return [dict(packet) for packet in packets if isinstance(packet, Mapping)]


def _looks_like_packet(value: Mapping[str, Any]) -> bool:
    return "contexts" in value or "evidence_rows" in value or "agent" in value


def _rename_synthesis_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        renamed: dict[str, Any] = {}
        for key, item in value.items():
            synthesis_key = _SYNTHESIS_KEY_NAMES.get(str(key), key)
            renamed[synthesis_key] = _rename_synthesis_keys(item)
        return renamed
    if isinstance(value, list):
        return [_rename_synthesis_keys(item) for item in value]
    return value


def _infer_database_completeness(packets: Sequence[Mapping[str, Any]]) -> DatabaseCompleteness:
    if not packets:
        return DatabaseCompleteness(status="unavailable", reasons=["database evidence unavailable"])

    rows_returned = 0
    source_rows_matched = 0
    row_count_before_limit: int | None = None
    dry_run_passed: bool | None = None
    reasons: list[str] = []
    result_limited = False

    for packet in packets:
        dry_run = packet.get("dry_run_passed")
        if isinstance(dry_run, bool):
            dry_run_passed = dry_run if dry_run_passed is None else dry_run_passed and dry_run
        packet_rows = _optional_int(packet.get("rows_returned")) or 0
        rows_returned += packet_rows
        source_rows_matched += packet_rows
        before_limit = _optional_int(packet.get("row_count_before_limit"))
        if before_limit is not None:
            row_count_before_limit = (
                before_limit
                if row_count_before_limit is None
                else row_count_before_limit + before_limit
            )
        reasons.extend(str(warning) for warning in packet.get("join_warnings", []) if str(warning).strip())

    if dry_run_passed is False:
        reasons.append("database dry run did not pass")
        return DatabaseCompleteness(
            status="unknown",
            dry_run_passed=dry_run_passed,
            rows_returned=rows_returned,
            row_count_before_limit=row_count_before_limit,
            result_limited=None,
            reasons=reasons,
        )

    if row_count_before_limit is not None and row_count_before_limit > rows_returned:
        result_limited = True
        reasons.append("database rows were limited before all matching rows were returned")
        status = "partial"
    elif source_rows_matched == 0:
        status = "empty"
    elif row_count_before_limit is None:
        reasons.append("database total row count before limit is unavailable")
        status = "unknown"
    else:
        status = "complete"

    return DatabaseCompleteness(
        status=status,
        dry_run_passed=dry_run_passed,
        rows_returned=rows_returned,
        row_count_before_limit=row_count_before_limit,
        result_limited=result_limited,
        reasons=reasons,
    )


def _document_evidence_id(
    scope: str | None,
    context: Mapping[str, Any],
    packet_index: int,
    context_index: int,
) -> str:
    stable_key = context.get("context_id") or context.get("source_hash") or context.get("document_id")
    if stable_key:
        return f"doc:{scope or 'unknown'}:{stable_key}"
    return f"doc:{scope or 'unknown'}:{packet_index}:{context_index}:{_stable_hash(context)}"


def _database_evidence_id(row: Mapping[str, Any], packet_index: int, row_index: int) -> str:
    row_key = row.get("Sample ID") or row.get("sample_id") or row.get("process_id")
    if row_key:
        return f"db:{row_key}"
    return f"db:{packet_index}:{row_index}:{_stable_hash(row)}"


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

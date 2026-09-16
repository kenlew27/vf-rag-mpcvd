"""Evaluate V4 claim-level verifier fixtures.

Usage:
    python -m agent.verifier_eval harness/verifier_v3_stress_subset.json harness/verifier_results.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anthropic import APIConnectionError, APIStatusError

from agent.nodes.verify_answer import (
    VERIFIER_BATCH_SIZE,
    _build_anthropic_client,
    run_verifier_payload,
)


_FIELDS = [
    "Fixture ID", "Verifier Model", "Base ID", "Semantic Case ID", "Variant", "Question",
    "Source Mix", "Batch Composition", "Majority Status", "Is Reordered",
    "Claim / Test", "Claim ID", "Batch Position", "Claim Text",
    "Evidence IDs", "Evidence Text", "Evidence Parcel Count",
    "Evidence Word Count", "Gold Status", "Gold Reason Code",
    "Gold Rationale", "Gold Decisive Evidence IDs", "System Status",
    "System Evidence IDs", "Gold Action", "System Action", "Status Correct",
    "Action Correct", "Mismatch Type", "False Intervention",
    "Missed Intervention", "Is Anchor", "Minority Anchor",
    "Majority Assimilation", "Batch Raw Output",
]
_STATUSES = frozenset({"supported", "unsupported", "contradicted"})
_STATUS_ORDER = ("supported", "unsupported", "contradicted")
_CHECKPOINT_VERSION = 1
_RETRY_DELAYS_SECONDS = (10, 20, 40, 80, 120)

logger = logging.getLogger(__name__)


def run_evaluation(
    input_path: Path,
    output_path: Path,
    *,
    summary_output_path: Path | None = None,
    verifier_model: str | None = None,
    deterministic_only: bool = False,
    request_interval_seconds: float = 1.5,
    resume: bool = True,
    client: Any = None,
    payload_verifier: Callable[..., Any] = run_verifier_payload,
    _client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Score a V4 corpus through the production payload-verifier interface."""
    if request_interval_seconds < 0:
        raise ValueError("request interval must be non-negative")
    fixtures = _load_fixtures(input_path)
    input_digest = _input_digest(input_path)
    checkpoint_path = _checkpoint_path(output_path)
    rows, completed_fixture_count = (
        ([], 0)
        if deterministic_only or not resume
        else _load_checkpoint(
            checkpoint_path,
            input_digest=input_digest,
            verifier_model=verifier_model,
            fixtures=fixtures,
        )
    )
    if not deterministic_only and completed_fixture_count:
        _write_rows(output_path, rows)
    request_started = False
    diagnostics: list[dict[str, Any]] = []
    owned_client = None
    if not deterministic_only and completed_fixture_count < len(fixtures):
        if client is None and _client_factory is not None:
            client = owned_client = _client_factory()
        elif client is None and payload_verifier is run_verifier_payload:
            client = owned_client = _build_anthropic_client()

    try:
        for fixture_index, fixture in enumerate(fixtures):
            if fixture_index < completed_fixture_count:
                continue
            if deterministic_only:
                rows.extend(
                    _not_evaluated_rows(fixture, verifier_model=verifier_model)
                )
                continue
            fixture_rows = []
            for batch_index, batch in enumerate(_fixture_batches(fixture), start=1):
                if request_started and request_interval_seconds:
                    time.sleep(request_interval_seconds)
                diagnostic = {
                    "diagnostic_id": str(uuid.uuid4()),
                    "fixture_id": str(fixture["id"]),
                    "batch_index": batch_index,
                    "input": batch["verifier_input"],
                    "attempts": [],
                }
                diagnostics.append(diagnostic)
                try:
                    raw_result = _run_payload_with_retries(
                        payload_verifier,
                        batch["verifier_input"],
                        client=client,
                        verifier_model=verifier_model,
                        fixture_id=str(fixture["id"]),
                        batch_index=batch_index,
                        diagnostic=diagnostic,
                    )
                    diagnostic["output"] = {
                        "assessments": _assessments(raw_result),
                        "raw_response": _raw_response(raw_result),
                    }
                    diagnostic["status"] = "succeeded"
                    request_started = True
                    fixture_rows.extend(
                        _scored_rows(
                            batch,
                            _assessments(raw_result),
                            raw_response=_raw_response(raw_result),
                            verifier_model=verifier_model,
                        )
                    )
                except RuntimeError as exc:
                    logger.warning(
                        "verifier batch failed for fixture %s batch %s; skipping: %s",
                        str(fixture["id"]), batch_index, exc,
                    )
            rows.extend(fixture_rows)
            _write_checkpoint(
                checkpoint_path,
                output_path,
                input_digest=input_digest,
                verifier_model=verifier_model,
                fixtures=fixtures,
                completed_fixture_count=fixture_index + 1,
                rows=rows,
            )
    finally:
        _write_diagnostics(output_path.with_suffix(output_path.suffix + ".jsonl"), diagnostics)
        close = getattr(owned_client, "close", None)
        if callable(close):
            close()

    if deterministic_only:
        _write_rows(output_path, rows)
    summary = _summary(rows) if not deterministic_only else {}
    if summary_output_path is not None:
        _write_json(summary_output_path, summary)
    return summary


def _run_payload_with_retries(
    payload_verifier: Callable[..., Any],
    payload: list[dict[str, Any]],
    *,
    client: Any,
    verifier_model: str | None,
    fixture_id: str,
    batch_index: int,
    diagnostic: dict[str, Any] | None = None,
) -> Any:
    for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
        attempt_started = time.monotonic()
        try:
            result = payload_verifier(
                payload,
                client=client,
                verifier_model=verifier_model,
            )
            if diagnostic is not None:
                diagnostic["attempts"].append({
                    "attempt": attempt + 1,
                    "status": "succeeded",
                    "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                })
            return result
        except Exception as exc:
            exception_traceback = _redact("".join(traceback.format_exception(exc)))
            if diagnostic is not None:
                diagnostic["attempts"].append({
                    "attempt": attempt + 1,
                    "status": "failed",
                    "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                    "error": _redact(f"{type(exc).__name__}: {exc}"),
                    "traceback": exception_traceback,
                })
            if not _is_transient_provider_error(exc) or attempt == len(_RETRY_DELAYS_SECONDS):
                if diagnostic is not None:
                    diagnostic["status"] = "failed"
                    diagnostic["error"] = _redact(f"{type(exc).__name__}: {exc}")
                    diagnostic["traceback"] = exception_traceback
                print(exception_traceback, file=sys.stderr)
                raise
            delay = _RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "transient verifier error for fixture %s batch %s; retry %s in %ss "
                "(status=%s request_id=%s)",
                fixture_id,
                batch_index,
                attempt + 1,
                delay,
                getattr(exc, "status_code", None),
                getattr(exc, "request_id", None),
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def _is_transient_provider_error(exc: Exception) -> bool:
    if isinstance(exc, APIConnectionError):
        return True
    if not isinstance(exc, APIStatusError):
        return False
    status_code = exc.status_code
    return status_code in {408, 409, 429} or status_code >= 500


def _load_fixtures(input_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("taxonomy_version") != "verifier-v4":
        raise ValueError("V4 evaluator requires a verifier-v4 fixture corpus")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("input must contain a non-empty fixtures array")
    for fixture in fixtures:
        _validate_fixture(fixture)
    return fixtures


def _validate_fixture(fixture: Any) -> None:
    if not isinstance(fixture, dict):
        raise ValueError("every fixture must be an object")
    required = {"id", "verifier_input", "gold_checks"}
    if missing := sorted(required - fixture.keys()):
        raise ValueError(f"fixture is missing required fields: {', '.join(missing)}")
    inputs, checks = fixture["verifier_input"], fixture["gold_checks"]
    if not isinstance(inputs, list) or not isinstance(checks, list) or not inputs:
        raise ValueError("V4 fixtures require non-empty verifier_input and gold_checks arrays")
    input_ids = []
    evidence_ids_by_claim: dict[str, set[str]] = {}
    for claim in inputs:
        if not isinstance(claim, dict) or set(claim) != {"claim_id", "claim_text", "evidence"}:
            raise ValueError("V4 verifier_input claims must have claim_id, claim_text, and evidence only")
        if not isinstance(claim["evidence"], list):
            raise ValueError("V4 verifier_input evidence must be an array")
        claim_id = str(claim["claim_id"])
        evidence_ids = []
        for parcel in claim["evidence"]:
            if not isinstance(parcel, dict) or set(parcel) != {"evidence_id", "content"}:
                raise ValueError(
                    "V4 evidence parcels must have evidence_id and content only"
                )
            if not isinstance(parcel["evidence_id"], str) or not parcel["evidence_id"]:
                raise ValueError("V4 evidence_id must be a non-empty string")
            if not isinstance(parcel["content"], str) or not parcel["content"]:
                raise ValueError("V4 evidence content must be a non-empty string")
            evidence_ids.append(parcel["evidence_id"])
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("V4 evidence IDs must be unique within each claim")
        input_ids.append(claim_id)
        evidence_ids_by_claim[claim_id] = set(evidence_ids)
    check_ids = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"claim_id", "status"}:
            raise ValueError("V4 gold_checks must have claim_id and status only")
        if check["status"] not in _STATUSES:
            raise ValueError("V4 gold status is invalid")
        check_ids.append(str(check["claim_id"]))
    if (
        len(input_ids) != len(set(input_ids))
        or len(check_ids) != len(set(check_ids))
        or set(input_ids) != set(check_ids)
    ):
        raise ValueError("V4 gold_checks must cover verifier_input claim IDs exactly once")

    rationales = fixture.get("gold_rationales")
    if rationales is None:
        return
    if not isinstance(rationales, list):
        raise ValueError("V4 gold_rationales must be an array")
    rationale_ids = []
    for rationale in rationales:
        if not isinstance(rationale, dict) or set(rationale) != {
            "claim_id", "reason_code", "rationale", "decisive_evidence_ids"
        }:
            raise ValueError(
                "V4 gold_rationales must have claim_id, reason_code, rationale, "
                "and decisive_evidence_ids only"
            )
        claim_id = str(rationale["claim_id"])
        decisive_ids = rationale["decisive_evidence_ids"]
        if not isinstance(decisive_ids, list):
            raise ValueError("V4 decisive_evidence_ids must be an array")
        if claim_id in evidence_ids_by_claim and not set(map(str, decisive_ids)) <= evidence_ids_by_claim[claim_id]:
            raise ValueError("V4 decisive evidence IDs must belong to the matching claim")
        rationale_ids.append(claim_id)
    if (
        len(rationale_ids) != len(set(rationale_ids))
        or set(rationale_ids) != set(input_ids)
    ):
        raise ValueError(
            "V4 gold_rationales must cover verifier_input claim IDs exactly once"
        )


def _fixture_batches(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    gold_by_id = {str(check["claim_id"]): check for check in fixture["gold_checks"]}
    return [
        {
            **fixture,
            "verifier_input": fixture["verifier_input"][start:start + VERIFIER_BATCH_SIZE],
            "gold_checks": [gold_by_id[str(claim["claim_id"])] for claim in fixture["verifier_input"][start:start + VERIFIER_BATCH_SIZE]],
        }
        for start in range(0, len(fixture["verifier_input"]), VERIFIER_BATCH_SIZE)
    ]


def _assessments(raw_result: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw_result, tuple):
        raw_result = raw_result[0]
    if isinstance(raw_result, dict):
        raw_result = raw_result.get("assessments", raw_result.get("checks"))
    if not isinstance(raw_result, list):
        raise ValueError("verifier result must contain an assessments list")
    result: dict[str, dict[str, Any]] = {}
    for assessment in raw_result:
        if hasattr(assessment, "model_dump"):
            assessment = assessment.model_dump(mode="json")
        if not isinstance(assessment, dict):
            raise ValueError("verifier assessment must be an object")
        claim_id, status = str(assessment.get("claim_id", "")), assessment.get("status")
        if not claim_id or status not in _STATUSES or claim_id in result:
            raise ValueError("verifier assessment has invalid claim_id or status")
        result[claim_id] = assessment
    return result


def _base_row(
    fixture: dict[str, Any],
    gold: dict[str, Any],
    *,
    batch_position: int,
    verifier_model: str | None,
) -> dict[str, str]:
    claim_id = str(gold["claim_id"])
    claim = next(
        item for item in fixture["verifier_input"]
        if str(item["claim_id"]) == claim_id
    )
    rationale = next(
        (
            item for item in fixture.get("gold_rationales", [])
            if str(item.get("claim_id")) == claim_id
        ),
        {},
    )
    evidence = claim["evidence"]
    evidence_ids = [str(item.get("evidence_id", "")) for item in evidence]
    evidence_texts = [str(item.get("content", "")) for item in evidence]
    majority = str(fixture.get("majority_status", ""))
    is_anchor = claim_id == str(fixture.get("anchor_claim_id", ""))
    gold_status = str(gold["status"])
    minority_anchor = is_anchor and majority in _STATUSES and majority != gold_status
    return {
        "Fixture ID": str(fixture["id"]),
        "Verifier Model": verifier_model or "",
        "Base ID": str(fixture.get("base_id", "")),
        "Semantic Case ID": str(fixture.get("semantic_case_id", "")),
        "Variant": str(fixture.get("variant", "")),
        "Question": str(fixture.get("question", "")),
        "Source Mix": str(fixture.get("source_mix", "")),
        "Batch Composition": str(fixture.get("batch_composition", "")),
        "Majority Status": majority,
        "Is Reordered": _bool(bool(fixture.get("is_reordered", False))),
        "Claim / Test": str(fixture.get("claim_test", "")),
        "Claim ID": claim_id,
        "Batch Position": str(batch_position),
        "Claim Text": str(claim["claim_text"]),
        "Evidence IDs": "; ".join(evidence_ids),
        "Evidence Text": "\n\n".join(evidence_texts),
        "Evidence Parcel Count": str(len(evidence)),
        "Evidence Word Count": str(sum(len(text.split()) for text in evidence_texts)),
        "Gold Status": gold_status,
        "Gold Reason Code": str(rationale.get("reason_code", "")),
        "Gold Rationale": str(rationale.get("rationale", "")),
        "Gold Decisive Evidence IDs": "; ".join(
            str(item) for item in rationale.get("decisive_evidence_ids", [])
        ),
        "System Status": "",
        "System Evidence IDs": "",
        "Gold Action": _action_for_status(gold_status),
        "System Action": "",
        "Status Correct": "FALSE", "Action Correct": "FALSE",
        "Mismatch Type": "",
        "False Intervention": "FALSE", "Missed Intervention": "FALSE",
        "Is Anchor": _bool(is_anchor),
        "Minority Anchor": _bool(minority_anchor),
        "Majority Assimilation": "FALSE",
        "Batch Raw Output": "",
    }


def _scored_rows(
    fixture: dict[str, Any],
    assessments: dict[str, dict[str, Any]],
    *,
    raw_response: str,
    verifier_model: str | None,
) -> list[dict[str, str]]:
    expected = {str(check["claim_id"]) for check in fixture["gold_checks"]}
    if set(assessments) != expected:
        raise ValueError("verifier assessments do not cover gold claim IDs exactly")
    rows = []
    for batch_position, gold in enumerate(fixture["gold_checks"], start=1):
        row = _base_row(
            fixture,
            gold,
            batch_position=batch_position,
            verifier_model=verifier_model,
        )
        assessment = assessments[str(gold["claim_id"])]
        status = str(assessment["status"])
        action = str(assessment.get("action") or _action_for_status(status))
        if action not in {"allow", "intervene"}:
            raise ValueError("verifier assessment has invalid action")
        row.update({
            "System Status": status,
            "System Evidence IDs": "; ".join(
                str(item) for item in assessment.get("evidence_ids", [])
            ),
            "System Action": action,
            "Status Correct": _bool(status == row["Gold Status"]),
            "Action Correct": _bool(action == row["Gold Action"]),
            "Mismatch Type": _mismatch_type(row["Gold Status"], status),
            "Batch Raw Output": raw_response,
        })
        row["False Intervention"] = _bool(row["Gold Action"] == "allow" and action == "intervene")
        row["Missed Intervention"] = _bool(row["Gold Action"] == "intervene" and action != "intervene")
        row["Majority Assimilation"] = _bool(
            row["Minority Anchor"] == "TRUE"
            and status == row["Majority Status"]
        )
        rows.append(row)
    return rows


def _not_evaluated_rows(
    fixture: dict[str, Any], *, verifier_model: str | None
) -> list[dict[str, str]]:
    rows = []
    for batch_position, gold in enumerate(fixture["gold_checks"], start=1):
        row = _base_row(
            fixture,
            gold,
            batch_position=batch_position,
            verifier_model=verifier_model,
        )
        for field in ("Status Correct", "Action Correct", "False Intervention", "Missed Intervention"):
            row[field] = "NOT_EVALUATED"
        rows.append(row)
    return rows


def _summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    count = len(rows)
    action_tp = sum(row["Gold Action"] == "intervene" and row["System Action"] == "intervene" for row in rows)
    false = sum(row["False Intervention"] == "TRUE" for row in rows)
    missed = sum(row["Missed Intervention"] == "TRUE" for row in rows)
    precision = 1.0 if action_tp + false == 0 else action_tp / (action_tp + false)
    recall = 1.0 if action_tp + missed == 0 else action_tp / (action_tp + missed)
    return {
        "status_accuracy": sum(row["Status Correct"] == "TRUE" for row in rows) / count if count else 1.0,
        "action_accuracy": sum(row["Action Correct"] == "TRUE" for row in rows) / count if count else 1.0,
        "false_interventions": false, "missed_interventions": missed,
        "intervention_precision": precision, "intervention_recall": recall,
        "intervention_f1": 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall),
        "confusion_matrix": _confusion_matrix(rows),
        "status_metrics": _status_metrics(rows),
        "slice_metrics": {
            "variant": _slice_metrics(rows, "Variant"),
            "source_mix": _slice_metrics(rows, "Source Mix"),
            "batch_composition": _slice_metrics(rows, "Batch Composition"),
            "gold_reason_code": _slice_metrics(rows, "Gold Reason Code"),
            "batch_position": _slice_metrics(rows, "Batch Position"),
        },
        "paired_metrics": _paired_metrics(rows),
    }


def _confusion_matrix(rows: list[dict[str, str]]) -> dict[str, dict[str, int]]:
    return {
        gold: {
            system: sum(
                row["Gold Status"] == gold and row["System Status"] == system
                for row in rows
            )
            for system in _STATUS_ORDER
        }
        for gold in _STATUS_ORDER
    }


def _status_metrics(rows: list[dict[str, str]]) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for status in _STATUS_ORDER:
        true_positive = sum(
            row["Gold Status"] == status and row["System Status"] == status
            for row in rows
        )
        false_positive = sum(
            row["Gold Status"] != status and row["System Status"] == status
            for row in rows
        )
        false_negative = sum(
            row["Gold Status"] == status and row["System Status"] != status
            for row in rows
        )
        support = true_positive + false_negative
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive else 0.0
        )
        recall = true_positive / support if support else 0.0
        metrics[status] = {
            "support": support,
            "precision": precision,
            "recall": recall,
            "f1": (
                0.0 if precision + recall == 0
                else 2 * precision * recall / (precision + recall)
            ),
        }
    return metrics


def _slice_metrics(
    rows: list[dict[str, str]], field: str
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row[field] or "(unspecified)"].append(row)
    return {
        value: {
            "count": len(group),
            "status_accuracy": _rate(group, "Status Correct"),
            "action_accuracy": _rate(group, "Action Correct"),
            "false_interventions": sum(
                row["False Intervention"] == "TRUE" for row in group
            ),
            "missed_interventions": sum(
                row["Missed Intervention"] == "TRUE" for row in group
            ),
        }
        for value, group in sorted(groups.items())
    }


def _paired_metrics(rows: list[dict[str, str]]) -> dict[str, float | int]:
    anchors = [row for row in rows if row["Is Anchor"] == "TRUE"]
    minority = [row for row in anchors if row["Minority Anchor"] == "TRUE"]
    homogeneous = [row for row in anchors if row["Minority Anchor"] == "FALSE"]
    anchor_groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in anchors:
        anchor_groups[(
            row["Semantic Case ID"], row["Variant"], row["Claim ID"]
        )].append(row)
    comparable_anchor_groups = [group for group in anchor_groups.values() if len(group) > 1]

    variant_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        variant_groups[(row["Base ID"], row["Claim ID"])].append(row)
    comparable_variants = [group for group in variant_groups.values() if len(group) > 1]

    order_pairs = []
    for group in comparable_variants:
        by_variant = {row["Variant"]: row for row in group}
        if {"long_noisy", "long_noisy_reordered"} <= set(by_variant):
            order_pairs.append((
                by_variant["long_noisy"], by_variant["long_noisy_reordered"]
            ))

    fixture_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        fixture_groups[row["Fixture ID"]].append(row)
    mixed_batches = [
        group for group in fixture_groups.values()
        if len({row["Gold Status"] for row in group}) > 1
    ]

    return {
        "anchor_context_groups": len(comparable_anchor_groups),
        "anchor_flip_rate": _fraction(
            sum(len({row["System Status"] for row in group}) > 1
                for group in comparable_anchor_groups),
            len(comparable_anchor_groups),
        ),
        "majority_assimilation_rate": _fraction(
            sum(row["Majority Assimilation"] == "TRUE" for row in minority),
            len(minority),
        ),
        "minority_penalty": _rate(homogeneous, "Status Correct") - _rate(
            minority, "Status Correct"
        ),
        "variant_groups": len(comparable_variants),
        "variant_flip_rate": _fraction(
            sum(len({row["System Status"] for row in group}) > 1
                for group in comparable_variants),
            len(comparable_variants),
        ),
        "order_pairs": len(order_pairs),
        "order_sensitivity": _fraction(
            sum(left["System Status"] != right["System Status"]
                for left, right in order_pairs),
            len(order_pairs),
        ),
        "mixed_batches": len(mixed_batches),
        "batch_homogenization_rate": _fraction(
            sum(len({row["System Status"] for row in group}) == 1
                for group in mixed_batches),
            len(mixed_batches),
        ),
    }


def _rate(rows: list[dict[str, str]], field: str) -> float:
    return _fraction(sum(row[field] == "TRUE" for row in rows), len(rows))


def _fraction(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _action_for_status(status: str) -> str:
    return "allow" if status == "supported" else "intervene"


def _raw_response(raw_result: Any) -> str:
    if isinstance(raw_result, tuple) and len(raw_result) >= 3:
        return str(raw_result[2])
    return ""


def _mismatch_type(gold: str, system: str) -> str:
    if gold == system:
        return ""
    return {
        ("supported", "unsupported"): "supported_missed_as_unsupported",
        ("supported", "contradicted"): "supported_misread_as_contradicted",
        ("unsupported", "supported"): "unsupported_overaccepted",
        ("unsupported", "contradicted"): "unsupported_misread_as_contradicted",
        ("contradicted", "supported"): "contradiction_missed_as_supported",
        ("contradicted", "unsupported"): "contradiction_weakened_to_unsupported",
    }[(gold, system)]


def _bool(value: bool) -> str:
    return str(value).upper()


def _checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".checkpoint.json")


def _input_digest(input_path: Path) -> str:
    return hashlib.sha256(input_path.read_bytes()).hexdigest()


def _load_checkpoint(
    checkpoint_path: Path,
    *,
    input_digest: str,
    verifier_model: str | None,
    fixtures: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], int]:
    if not checkpoint_path.exists():
        return [], 0
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        completed = checkpoint["completed_fixture_count"]
        rows = checkpoint["rows"]
        expected_ids = [str(fixture["id"]) for fixture in fixtures[:completed]]
        if (
            checkpoint["version"] != _CHECKPOINT_VERSION
            or checkpoint["input_sha256"] != input_digest
            or checkpoint["verifier_model"] != (verifier_model or "")
            or checkpoint["fields"] != _FIELDS
            or checkpoint["completed_fixture_ids"] != expected_ids
            or not isinstance(completed, int)
            or not 0 <= completed <= len(fixtures)
            or not isinstance(rows, list)
        ):
            raise ValueError
        _validate_checkpoint_rows(fixtures[:completed], rows, verifier_model)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "checkpoint does not match the current input, model, or CSV schema; "
            "rerun with --no-resume to start over"
        ) from exc
    return rows, completed


def _validate_checkpoint_rows(
    fixtures: list[dict[str, Any]],
    rows: list[Any],
    verifier_model: str | None,
) -> None:
    offset = 0
    for fixture in fixtures:
        for batch in _fixture_batches(fixture):
            row_count = len(batch["gold_checks"])
            batch_rows = rows[offset:offset + row_count]
            if len(batch_rows) != row_count or any(
                not isinstance(row, dict)
                or set(row) != set(_FIELDS)
                or any(not isinstance(value, str) for value in row.values())
                for row in batch_rows
            ):
                raise ValueError
            assessments = {
                row["Claim ID"]: {
                    "claim_id": row["Claim ID"],
                    "status": row["System Status"],
                    "evidence_ids": (
                        row["System Evidence IDs"].split("; ")
                        if row["System Evidence IDs"] else []
                    ),
                    "action": row["System Action"],
                }
                for row in batch_rows
            }
            expected = _scored_rows(
                batch,
                assessments,
                raw_response=batch_rows[0]["Batch Raw Output"],
                verifier_model=verifier_model,
            )
            if batch_rows != expected:
                raise ValueError
            offset += row_count
    if offset != len(rows):
        raise ValueError


def _write_checkpoint(
    checkpoint_path: Path,
    output_path: Path,
    *,
    input_digest: str,
    verifier_model: str | None,
    fixtures: list[dict[str, Any]],
    completed_fixture_count: int,
    rows: list[dict[str, str]],
) -> None:
    _write_json(checkpoint_path, {
        "version": _CHECKPOINT_VERSION,
        "input_sha256": input_digest,
        "verifier_model": verifier_model or "",
        "fields": _FIELDS,
        "completed_fixture_count": completed_fixture_count,
        "completed_fixture_ids": [
            str(fixture["id"])
            for fixture in fixtures[:completed_fixture_count]
        ],
        "rows": rows,
    })
    _write_rows(output_path, rows)


def _write_rows(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=output_path.parent,
            delete=False,
        ) as output_file:
            temp_path = Path(output_file.name)
            writer = csv.DictWriter(output_file, fieldnames=_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_json(output_path: Path, payload: Any) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            delete=False,
        ) as output_file:
            temp_path = Path(output_file.name)
            json.dump(payload, output_file, indent=2, sort_keys=True)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_diagnostics(output_path: Path, diagnostics: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        for diagnostic in diagnostics:
            output_file.write(json.dumps(_redact_value(diagnostic), ensure_ascii=False, default=str) + "\n")


def _redact(text: str) -> str:
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|authorization|bearer|token|password)\b\s*[:=]\s*)[^\s,]+",
        r"\1[REDACTED]",
        text,
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate V4 verifier fixtures and write a scorecard CSV.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--deterministic-only", action="store_true")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=1.5,
        help="seconds to wait between verifier API batches (default: 1.5)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore any existing fixture checkpoint and start over",
    )
    args = parser.parse_args()
    print(json.dumps(run_evaluation(
        args.input,
        args.output,
        summary_output_path=args.summary_output,
        verifier_model=args.model,
        deterministic_only=args.deterministic_only,
        request_interval_seconds=args.request_interval_seconds,
        resume=not args.no_resume,
    ), sort_keys=True))


if __name__ == "__main__":
    main()

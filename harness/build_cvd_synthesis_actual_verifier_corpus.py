"""Build a verifier-v4 corpus from every adjudicated synthesis claim.

The production verifier evaluator requires ``gold_checks`` to contain only
``claim_id`` and ``status``.  Each fixture therefore also carries explicit
``gold_label`` and ``gold_action`` metadata for auditing and downstream
answer-level analysis.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = (
    REPO_ROOT
    / "outputs"
    / "cvd_synthesis_claim_audit_20260814"
    / "claim_level_adjudication.jsonl"
)
CORPUS_PATH = REPO_ROOT / "harness" / "cvd_diamond_synthesis_actual_claims_verifier_v4.json"
MANIFEST_PATH = REPO_ROOT / "harness" / "cvd_diamond_synthesis_actual_claims_verifier_v4_manifest.csv"

EXPECTED_LABEL_COUNTS = {
    "supported": 1070,
    "unsupported": 132,
    "contradicted": 1,
}
VALID_LABELS = frozenset(EXPECTED_LABEL_COUNTS)


def _action_for_label(label: str) -> str:
    return "allow" if label == "supported" else "intervene"


def _read_rows() -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in SOURCE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("synthesis claim audit is empty")
    return rows


def _validate_source_rows(rows: list[dict[str, Any]]) -> None:
    claim_ids = [str(row.get("claim_audit_id", "")) for row in rows]
    if any(not claim_id for claim_id in claim_ids):
        raise ValueError("every source claim must have a claim_audit_id")
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("source claim_audit_id values must be globally unique")

    labels = Counter(str(row.get("strict_label", "")) for row in rows)
    if set(labels) - VALID_LABELS:
        raise ValueError(f"invalid strict labels: {sorted(set(labels) - VALID_LABELS)}")
    if dict(labels) != EXPECTED_LABEL_COUNTS:
        raise ValueError(
            f"source label counts changed: expected {EXPECTED_LABEL_COUNTS}, got {dict(labels)}"
        )

    source_rows = {int(row["source_row"]) for row in rows}
    if len(source_rows) != 43:
        raise ValueError(f"expected 43 substantive answer instances, got {len(source_rows)}")

    for row in rows:
        claim_id = str(row["claim_audit_id"])
        if not str(row.get("claim_text", "")).strip():
            raise ValueError(f"{claim_id} has empty claim_text")
        evidence_ids = row.get("evidence_ids")
        evidence = row.get("evidence")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError(f"{claim_id} has no evidence_ids")
        if len(evidence_ids) != len(set(map(str, evidence_ids))):
            raise ValueError(f"{claim_id} has duplicate evidence_ids")
        if not isinstance(evidence, dict):
            raise ValueError(f"{claim_id} evidence must be an object")
        missing = [
            evidence_id
            for evidence_id in evidence_ids
            if not str(evidence.get(evidence_id, "")).strip()
        ]
        if missing:
            raise ValueError(f"{claim_id} has missing evidence content for {missing}")


def _build_fixture(row: dict[str, Any], index: int) -> dict[str, Any]:
    claim_id = str(row["claim_audit_id"])
    label = str(row["strict_label"])
    action = _action_for_label(label)
    evidence_ids = [str(value) for value in row["evidence_ids"]]
    evidence = [
        {
            "evidence_id": evidence_id,
            "content": str(row["evidence"][evidence_id]),
        }
        for evidence_id in evidence_ids
    ]
    source_row = int(row["source_row"])
    eval_id = str(row["eval_id"])
    answer_id = f"SYN-A{source_row:03d}-{eval_id}"

    return {
        "id": f"CVD-SYN-ACT-FIX-{index:04d}",
        "verifier_input": [
            {
                "claim_id": claim_id,
                "claim_text": str(row["claim_text"]),
                "evidence": evidence,
            }
        ],
        "gold_checks": [{"claim_id": claim_id, "status": label}],
        "gold_rationales": [
            {
                "claim_id": claim_id,
                "reason_code": "manual_synthesis_adjudication",
                "rationale": str(row.get("adjudication_note", "")),
                "decisive_evidence_ids": evidence_ids,
            }
        ],
        "gold_label": label,
        "gold_action": action,
        "answer_id": answer_id,
        "question": answer_id,
        "source_row": source_row,
        "eval_id": eval_id,
        "original_query": str(row["question"]),
        "source_mix": "synthesis_actual",
        "batch_composition": label,
        "claim_test": "synthesizer_actual_output",
        "lenient_label": str(row.get("lenient_label", "")),
    }


def _build_corpus(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fixtures = [_build_fixture(row, index) for index, row in enumerate(rows, start=1)]
    label_counts = Counter(fixture["gold_label"] for fixture in fixtures)
    action_counts = Counter(fixture["gold_action"] for fixture in fixtures)
    return {
        "schema_version": "4.0",
        "taxonomy_version": "verifier-v4",
        "corpus_kind": "cvd_diamond_synthesis_actual_claims",
        "corpus_id": "cvd-diamond-synthesis-actual-claims-v1",
        "design": {
            "fixture_count": len(fixtures),
            "claim_count": len(fixtures),
            "claims_per_fixture": 1,
            "substantive_answer_instances": len({fixture["answer_id"] for fixture in fixtures}),
            "unique_eval_ids": len({fixture["eval_id"] for fixture in fixtures}),
            "answer_group_field": "answer_id",
            "gold_label_source": "strict_label",
            "gold_action_mapping": {
                "supported": "allow",
                "unsupported": "intervene",
                "contradicted": "intervene",
            },
            "label_counts": dict(sorted(label_counts.items())),
            "action_counts": dict(sorted(action_counts.items())),
            "gold_not_sent_to_verifier": True,
            "one_fixture_per_claim": True,
        },
        "provenance": {
            "source_path": str(SOURCE_PATH.relative_to(REPO_ROOT)),
            "source_sha256": hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest(),
        },
        "fixtures": fixtures,
    }


def _write_manifest(fixtures: list[dict[str, Any]]) -> None:
    fieldnames = [
        "Fixture ID",
        "Claim ID",
        "Answer ID",
        "Source Row",
        "Eval ID",
        "Original Question",
        "Claim Text",
        "Evidence IDs",
        "Evidence Parcel Count",
        "Gold Label",
        "Gold Action",
        "Lenient Label",
        "Gold Reason Code",
        "Gold Rationale",
    ]
    with MANIFEST_PATH.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        for fixture in fixtures:
            claim = fixture["verifier_input"][0]
            rationale = fixture["gold_rationales"][0]
            writer.writerow(
                {
                    "Fixture ID": fixture["id"],
                    "Claim ID": claim["claim_id"],
                    "Answer ID": fixture["answer_id"],
                    "Source Row": fixture["source_row"],
                    "Eval ID": fixture["eval_id"],
                    "Original Question": fixture["original_query"],
                    "Claim Text": claim["claim_text"],
                    "Evidence IDs": "; ".join(
                        item["evidence_id"] for item in claim["evidence"]
                    ),
                    "Evidence Parcel Count": len(claim["evidence"]),
                    "Gold Label": fixture["gold_label"],
                    "Gold Action": fixture["gold_action"],
                    "Lenient Label": fixture["lenient_label"],
                    "Gold Reason Code": rationale["reason_code"],
                    "Gold Rationale": rationale["rationale"],
                }
            )


def main() -> None:
    rows = _read_rows()
    _validate_source_rows(rows)
    corpus = _build_corpus(rows)
    CORPUS_PATH.write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_manifest(corpus["fixtures"])
    print(
        json.dumps(
            {
                "corpus": str(CORPUS_PATH),
                "manifest": str(MANIFEST_PATH),
                **corpus["design"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

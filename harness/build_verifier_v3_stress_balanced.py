"""Build a balanced, source-native verifier stress corpus from synthesize_3.

The existing V3 subsets were re-adjudicated after creation and consequently
became unsupported-heavy.  This corpus retains the five-claim, four-evidence-
profile, plus exact-order-permutation structure, but derives its supported
controls from verbatim source excerpts.  That makes the supported boundary
unambiguous in every evidence profile while preserving native evidence text.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from harness.build_verifier_v3_stress_corpora import (
    CORE_VARIANTS,
    _evidence_parcels,
    _json,
    _load_synthesis_seeds,
    _ranked_windows,
    _reordered_fixture,
    _scenario,
)


ALL_VARIANTS = CORE_VARIANTS + ("long_noisy_reordered",)
_SOURCE_MIXES = (
    ("document",) * 8
    + ("database",) * 8
    + ("mixed",) * 4
)
_BATCH_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    *(("5_supported", ("supported",) * 5),) * 4,
    *(("5_unsupported", ("unsupported",) * 5),) * 4,
    *(("5_contradicted", ("contradicted",) * 5),) * 4,
    *(("4_supported_1_unsupported", (
        "supported", "supported", "unsupported", "supported", "supported",
    )),) * 2,
    *(("4_unsupported_1_contradicted", (
        "unsupported", "unsupported", "contradicted", "unsupported", "unsupported",
    )),) * 2,
    *(("4_contradicted_1_supported", (
        "contradicted", "contradicted", "supported", "contradicted", "contradicted",
    )),) * 2,
    ("2_supported_2_unsupported_1_contradicted", (
        "supported", "unsupported", "contradicted", "supported", "unsupported",
    )),
    ("1_supported_2_unsupported_2_contradicted", (
        "unsupported", "contradicted", "supported", "unsupported", "contradicted",
    )),
)


def build_balanced_corpus(output_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the balanced corpus and its construction audit."""
    output_dir = output_dir or Path(__file__).resolve().parent
    seeds, source_sha256 = _load_synthesis_seeds()
    seed_offsets = {source_mix: 0 for source_mix in seeds}
    bases = []
    for number, ((composition, statuses), source_mix) in enumerate(
        zip(_BATCH_PATTERNS, _SOURCE_MIXES, strict=True), start=1
    ):
        scenario = _scenario(
            200 + number, source_mix, seeds, seed_offsets[source_mix]
        )
        seed_offsets[source_mix] += 1
        bases.append(_base(number, scenario, composition, statuses))

    fixtures = [
        _render_fixture(base, variant)
        for base in bases
        for variant in CORE_VARIANTS
    ]
    noisy = {
        fixture["base_id"]: fixture
        for fixture in fixtures
        if fixture["variant"] == "long_noisy"
    }
    fixtures.extend(_reordered_fixture(noisy[base["base_id"]]) for base in bases)
    fixtures.sort(key=lambda fixture: (fixture["base_id"], ALL_VARIANTS.index(fixture["variant"])))

    dataset = {
        "schema_version": "4.0",
        "taxonomy_version": "verifier-v4",
        "dataset_role": "development_stress_balanced",
        "holdout_eligible": False,
        "corpus_id": "verifier-v3-stress-balanced",
        "description": (
            "Twenty source-native synthesize_3 scenarios with five claims under four "
            "evidence profiles and an exact long-noisy order permutation. The 500 "
            "claim decisions are class-balanced by construction; supported controls "
            "are direct factual statements grounded in source-native text."
        ),
        "design": {
            "base_configuration_count": len(bases),
            "fixture_count": len(fixtures),
            "claims_per_fixture": 5,
            "claim_decision_count": sum(len(fixture["gold_checks"]) for fixture in fixtures),
            "core_variants": list(CORE_VARIANTS),
            "additional_variant": "long_noisy_reordered",
            "seed_corpus": "synthesize_3.csv",
            "seed_corpus_sha256": source_sha256,
            "source_native_evidence": True,
            "gold_labels_construction_verified": True,
            "gold_not_sent_to_verifier": True,
            "source_base_counts": dict(Counter(_SOURCE_MIXES)),
            "target_status_counts": {
                "supported": 165,
                "unsupported": 170,
                "contradicted": 165,
            },
        },
        "fixtures": fixtures,
    }
    _validate_dataset(dataset)

    corpus_path = output_dir / "verifier_v3_stress_balanced.json"
    audit_path = output_dir / "verifier_v3_stress_balanced_audit.json"
    corpus_path.write_text(_json(dataset), encoding="utf-8")
    audit_path.write_text(_json(_build_audit(dataset)), encoding="utf-8")
    return corpus_path, audit_path


def _base(
    number: int,
    scenario: dict[str, Any],
    composition: str,
    statuses: tuple[str, ...],
) -> dict[str, Any]:
    claims = [
        _claim_spec(scenario, status, index, f"CLM-{index + 1:03d}")
        for index, status in enumerate(statuses)
    ]
    counts = Counter(statuses)
    majority_status = next(
        (status for status, count in counts.items() if count == 4 or count == 5),
        "mixed",
    )
    return {
        "base_id": f"V3B-BAL{number:03d}",
        "scenario": scenario,
        "batch_composition": composition,
        "majority_status": majority_status,
        "anchor_claim_id": "CLM-003",
        "claims": claims,
    }


def _claim_spec(
    scenario: dict[str, Any],
    status: str,
    source_index: int,
    claim_id: str,
) -> dict[str, Any]:
    candidate = scenario["candidates"][source_index]
    source_item, fact, source_anchor = _select_fact_source(
        scenario, candidate, scenario["number"] + source_index
    )
    if status == "supported":
        claim_text = fact
        reason_code = "source_backed_statement"
        rationale = "The decisive evidence establishes the direct factual statement."
    elif status == "unsupported":
        claim_text = (
            f"{fact} This relationship held for every reactor, sample, and operating condition."
        )
        reason_code = "scope_or_causal_overreach"
        rationale = "The evidence establishes the bounded fact, not the added universal scope."
    else:
        claim_text = f"It is not true that {_lowercase_initial(fact.rstrip('.'))}."
        reason_code = "source_denial_conflict"
        rationale = "The decisive evidence establishes the factual statement that this claim denies."
    return {
        "claim_id": claim_id,
        "status": status,
        "claim_text": claim_text,
        "source_statement": source_anchor,
        "source_anchor": source_anchor,
        "reason_code": reason_code,
        "rationale": rationale,
        "source_items": [source_item],
        "all_seed_items": scenario["items"],
        "noise_offset": scenario["number"] + source_index,
    }


def _select_fact_source(
    scenario: dict[str, Any],
    candidate: dict[str, Any],
    offset: int,
) -> tuple[dict[str, str], str, str]:
    candidates = candidate["source_items"] + list(scenario["items"].values())
    seen = set()
    for source_item in candidates:
        source_id = source_item["evidence_id"]
        if source_id in seen:
            continue
        seen.add(source_id)
        fact = _factual_statement(source_item, offset)
        if fact is not None:
            return source_item, *fact
    raise ValueError("scenario has no source-native direct factual statement")


def _factual_statement(
    source_item: dict[str, str], offset: int
) -> tuple[str, str] | None:
    content = source_item["content"]
    windows = _ranked_windows(content, "")
    if source_item["source_type"] == "S":
        sample_id = re.search(r"(?:^|\n)Sample ID:\s*([^\n]+)", content)
        if sample_id is not None:
            value = sample_id.group(1).strip()
            return f"The sample ID is {value}.", f"Sample ID: {value}"

    field = re.search(r"(?:^|\n)field:\s*([^\n]+)", content)
    if field is not None:
        value = field.group(1).strip()
        return f"The profiled field is {value}.", f"field: {value}"

    sentences = [
        " ".join(sentence.split())
        for sentence in re.findall(r"[^.!?]+[.!?]", content)
        if 8 <= len(sentence.split()) <= 45
    ]
    sentences = [
        sentence for sentence in sentences
        if re.match(r"^[A-Z][A-Za-z]", sentence)
        and not sentence.startswith(("Figure", "Table"))
        and not sentence.endswith(("Fig.", "i.e.", "e.g."))
        and any(sentence in window for window in windows)
    ]
    if sentences:
        sentence = sentences[offset % len(sentences)].strip()
        return sentence, sentence

    return None


def _lowercase_initial(text: str) -> str:
    return text[:1].lower() + text[1:]


def _render_fixture(base: dict[str, Any], variant: str) -> dict[str, Any]:
    scenario = base["scenario"]
    verifier_input = []
    gold_checks = []
    gold_rationales = []
    for position, claim in enumerate(base["claims"], start=1):
        parcels, decisive_evidence_ids = _evidence_parcels(claim, position, variant)
        decisive_text = " ".join(
            parcel["content"]
            for parcel in parcels
            if parcel["evidence_id"] in decisive_evidence_ids
        )
        if claim["source_anchor"] not in " ".join(decisive_text.split()):
            raise ValueError("direct claim anchor is absent from decisive evidence")
        verifier_input.append({
            "claim_id": claim["claim_id"],
            "claim_text": claim["claim_text"],
            "evidence": parcels,
        })
        gold_checks.append({"claim_id": claim["claim_id"], "status": claim["status"]})
        gold_rationales.append({
            "claim_id": claim["claim_id"],
            "reason_code": claim["reason_code"],
            "rationale": claim["rationale"],
            "decisive_evidence_ids": decisive_evidence_ids,
        })
    return {
        "id": f"{base['base_id']}-{variant.replace('_', '-')}",
        "base_id": base["base_id"],
        "semantic_case_id": scenario["semantic_case_id"],
        "seed_id": scenario["seed_id"],
        "variant": variant,
        "question": scenario["question"],
        "source_mix": scenario["source_mix"],
        "batch_composition": base["batch_composition"],
        "majority_status": base["majority_status"],
        "anchor_claim_id": base["anchor_claim_id"],
        "is_reordered": False,
        "claim_test": f"{base['batch_composition']} batch under the {variant} evidence profile.",
        "tags": [
            "synthesize_3_seed",
            "balanced",
            "verbatim_control",
            scenario["source_mix"],
            base["batch_composition"],
            variant,
        ],
        "verifier_input": verifier_input,
        "gold_checks": gold_checks,
        "gold_rationales": gold_rationales,
    }


def _validate_dataset(dataset: dict[str, Any]) -> None:
    fixtures = dataset["fixtures"]
    statuses = Counter(
        check["status"]
        for fixture in fixtures
        for check in fixture["gold_checks"]
    )
    if len(fixtures) != 100 or statuses != dataset["design"]["target_status_counts"]:
        raise ValueError(f"balanced corpus has unexpected shape: {len(fixtures)} fixtures, {statuses}")


def _build_audit(dataset: dict[str, Any]) -> dict[str, Any]:
    fixtures = dataset["fixtures"]
    records = []
    for fixture in fixtures:
        rationales = {item["claim_id"]: item for item in fixture["gold_rationales"]}
        for claim, check in zip(fixture["verifier_input"], fixture["gold_checks"], strict=True):
            records.append({
                "fixture_id": fixture["id"],
                "claim_id": claim["claim_id"],
                "gold_status": check["status"],
                "reason_code": rationales[claim["claim_id"]]["reason_code"],
                "decisive_evidence_ids": rationales[claim["claim_id"]]["decisive_evidence_ids"],
            })
    return {
        "schema_version": "1.0",
        "corpus_id": dataset["corpus_id"],
        "adjudication_method": (
            "construction-verified direct claims: each claim is grounded in a source "
            "sentence or structured field, then adds either no scope, a universal scope, "
            "or a direct factual denial"
        ),
        "verifier_predictions_used_for_gold": False,
        "summary": {
            "fixture_count": len(fixtures),
            "decision_count": len(records),
            "status_counts": dataset["design"]["target_status_counts"],
            "human_review_group_count": 0,
        },
        "records": records,
    }


if __name__ == "__main__":
    print(*build_balanced_corpus(), sep="\n")

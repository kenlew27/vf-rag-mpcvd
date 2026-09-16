"""
demo_reasoning.py — barebones proof the reasoning pipeline works.

No API key, no BigQuery, no LangGraph. Runs the deterministic layers end-to-end:
  EvidencePacket → SynthesisOutput → FinalAnswer → CitationCheck → verifier input

Run with:
    python demo_reasoning.py
"""

from agent.schemas import (
    ConfidenceLabel,
    EvidenceIdMinter,
    EvidenceItem,
    EvidencePacket,
    FinalAnswer,
    Provenance,
    SourceType,
    SynthesisOutput,
    build_llm_check_payload,
    extract_claim_units,
    validate_citations,
)
from agent.request_plan import KnowledgeSource, RequestStatus, RequestTask

# 1. Build a fake evidence packet
minter = EvidenceIdMinter()
items = [
    EvidenceItem(
        evidence_id=minter.mint(SourceType.STRUCTURED),
        source_type=SourceType.STRUCTURED,
        source_label="Run EXP-001 (experiment_records)",
        content="growth_temp: 900°C\npressure: 300 torr\ngrowth_rate: 4.2 µm/hr",
        provenance=Provenance(origin_agent="table_agent", source_ref="bigquery", page_or_row="1"),
    ),
    EvidenceItem(
        evidence_id=minter.mint(SourceType.DOCUMENT),
        source_type=SourceType.DOCUMENT,
        source_label="Internal process note 2024",
        content="At 300 torr, diamond growth rate peaks near 900°C. Nitrogen content below 5 ppm improves birefringence.",
        provenance=Provenance(origin_agent="internal_document_agent", retrieval_query="CVD diamond growth rate temperature"),
    ),
]

packet = EvidencePacket(
    run_id="demo-run-001",
    query_raw="What growth rate does Run EXP-001 achieve and why?",
    tasks=[RequestTask.LOOKUP],
    knowledge_sources=[KnowledgeSource.STRUCTURED_DATA, KnowledgeSource.INTERNAL_DOCUMENTS],
    request_status=RequestStatus.READY,
    items=items,
    lanes_attempted=["structured", "internal"],
)

print(f"Packet: {len(packet.items)} items | hash: {packet.packet_hash()[:20]}...")

# 2. Fake synthesis output (what Claude would return)
synthesis = SynthesisOutput(
    answer=(
        "Run EXP-001 achieved a growth rate of 4.2 µm/hr at 900°C and 300 torr [EV-S-001]. "
        "Internal documentation confirms that 300 torr combined with 900°C represents the "
        "peak growth window for diamond CVD [EV-D-001]."
    ),
    citations=["EV-S-001", "EV-D-001"],
    confidence=ConfidenceLabel.MEDIUM,
    confidence_basis=[
        "Two items from different lanes with matching conditions",
        "Internal doc corroborates structured data",
    ],
)

# 3. Create FinalAnswer (code stamps metadata, model never writes it)
final_answer = FinalAnswer.from_synthesis(synthesis, packet, model="claude-sonnet-4-6", prompt_version="demo-v1")

print(f"\nFinalAnswer:")
print(f"  response_id : {final_answer.response_id}")
print(f"  confidence  : {final_answer.synthesis.confidence.value}")
print(f"  citations   : {final_answer.synthesis.citations}")

# 4. Citation check (deterministic)
check = final_answer.citation_check
print(f"\nCitationCheck:")
print(f"  all_cited_ids_exist     : {check.all_cited_ids_exist}")
print(f"  unknown_ids             : {check.unknown_ids}")
print(f"  inline_not_in_citations : {check.inline_not_in_citations}")
print(f"  citations_not_inline    : {check.citations_not_inline}")
print(f"  uncited_answer          : {check.uncited_answer}")

# 5. Claim extraction
units = extract_claim_units(synthesis)
print(f"\nClaims extracted: {len(units)}")
for u in units:
    print(f"  [{u.claim_id}] ({u.origin}) cited={u.cited_evidence_ids}")

# 6. Facet verifier payload (what Haiku would receive — no full answer, no query)
payload = build_llm_check_payload(units, packet)
print(f"\nLLM verifier payload: {len(payload)} claims to check")
for entry in payload:
    print(f"  {entry['claim_id']}: {entry['claim_text'][:60]}... ({len(entry['evidence'])} evidence items)")

print("\nDone.")

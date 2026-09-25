# Verification-First RAG for MPCVD Diamond Growth

**Code and evaluation materials** for the Digital Discovery paper:

> *Verification-first retrieval-augmented generation for trustworthy decision support in MPCVD diamond growth*

## Repository Structure

```
.
+-- agent/                      # Pipeline source code
|   +-- graph.py                # State-graph agent (LangGraph)
|   +-- state.py                # Pipeline state schema
|   +-- schemas.py              # Data contracts (EvidencePacket, FinalAnswer, etc.)
|   +-- request_plan.py         # Intent classification
|   +-- supervisor_routing.py   # Source routing
|   +-- database_query_planner.py  # Structured-data query planning
|   +-- document_query_planner.py  # Literature retrieval planning
|   +-- verifier_feedback.py    # Feedback-guided retry
|   +-- verifier_terminal.py    # Terminal safe synthesis
|   +-- _utils.py               # Shared internal utilities
|   +-- prompts/                # Prompt templates (Markdown)
|   +-- guidance/               # Database schema guides and safety rules
|   +-- nodes/                  # Graph node implementations
|   |   +-- plan_request.py     # Intent classification node
|   |   +-- supervisor.py       # Source routing node
|   |   +-- retrieve_bigquery.py   # Structured retrieval
|   |   +-- retrieve_document.py   # Literature retrieval
|   |   +-- synthesize_answer.py   # Synthesis node
|   |   +-- verify_answer.py       # Verification node
|   |   +-- compose_verified_answer.py  # Graded delivery
|   +-- synthesis/              # Multi-step evidence synthesis pipeline
+-- tools/                      # Infrastructure stubs (see Note below)
+-- storage/                    # Storage layer stubs (see Note below)
+-- app/                        # FastAPI application layer
+-- harness/                    # Evaluation harness
|   +-- cvd_diamond_question_set.json              # 50 evaluation questions
|   +-- claim_verification_gold_labels.csv         # 500 atomic claims with gold labels
|   +-- build_verifier_v3_stress_balanced.py       # Fixture construction script
|   +-- build_cvd_synthesis_actual_verifier_corpus.py  # Corpus builder
+-- evaluation/                 # Anonymised scientist evaluation scores
|   +-- scientist_scores_grader_A.csv
|   +-- scientist_scores_grader_B.csv
+-- figures/                    # Publication figures (600 DPI TIFF, B&W/greyscale)
|   +-- figure_1.tiff           # Six-stage pipeline architecture
|   +-- figure_2.tiff           # Verification and retry loop
|   +-- figure_3.tiff           # Stage-decomposed evaluation chart
|   +-- figure_4.tiff           # System ablation chart (verifier + retry)
|   +-- figure_5.tiff           # Atomic-claim accuracy by gold label
|   +-- figure_6.tiff           # Score distribution (scientist evaluation)
+-- compute_statistics.py       # Reproduce all reported statistics (Table 3)
+-- demo_reasoning.py           # Standalone demo (no API keys required)
+-- requirements.txt            # Python dependencies
+-- .env.example                # Required environment variables
+-- CITATION.cff                # Software citation metadata
+-- .zenodo.json                # Zenodo archive metadata
+-- LICENSE                     # MIT License
+-- README.md                   # This file
```

`tools/` and `storage/` provide the public interface for the production
infrastructure (LanceDB, BigQuery, GCS, Voyage embeddings, cross-encoder
reranker).  They are included so that all imports resolve and the full
pipeline can be read end-to-end; the deployed implementations require
access to the cloud environment.

## Released Materials

| Material | Description |
|:---------|:------------|
| Pipeline source code | Complete 6-stage architecture: intent classification, structured retrieval with deterministic guardrails, literature retrieval with reciprocal-rank fusion, evidence-grounded synthesis, proposition-level claim verification, and feedback-guided retry |
| CVD diamond question set | 50 questions across 3 complexity buckets (B1: database lookup, B2: database + literature, R: research hypothesis) |
| Claim verification fixtures | 500 atomic claims with gold labels (supported/unsupported/contradicted) derived from genuine model outputs |
| Scientist evaluation scores | 100 anonymised ratings from 2 independent domain scientists on groundedness, correctness, and scientific rigor (5-point Likert scales) |
| Scoring scripts | Reproduce all Wilson CIs, means, SDs, inter-rater agreement, and bucket breakdowns reported in the paper |
| Publication figures | All 6 paper figures at 600 DPI TIFF (B&W/greyscale) with SHA-256 integrity hashes |

## Not Included (Proprietary)

The structured experimental database (BigQuery table of MPCVD reactor runs) contains proprietary growth recipes, reactor configurations, and process parameters from an active industrial research programme and is not publicly available due to commercial sensitivity.

## Quick Start (for reviewers)

Everything below runs offline with no API keys or cloud access.

```bash
# 1. Set up environment
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Run the deterministic reasoning demo (no API keys needed)
python demo_reasoning.py
#    → builds an EvidencePacket, synthesises an answer, runs citation
#      checks, extracts claims, and formats a verifier payload

# 3. Run the test suite (122 unit tests)
python -m pytest agent/ app/ -q

# 4. Reproduce all paper statistics (Table 3, Section 4.4)
python compute_statistics.py
#    → Wilson CIs, means/SDs, inter-rater agreement, bucket breakdowns
```

`demo_reasoning.py` exercises the core data contracts end-to-end without
calling any LLM or database. It is the fastest way to verify that the
deterministic layers (evidence packaging, citation checking, claim
extraction, verifier payload construction) work as described in the paper.

## Domain Portability

The architecture is designed to be domain-portable. Adapting it to a different
materials system requires substituting three domain assets:

1. **Database schema** — a structured table of experimental records
2. **Document corpus** — internal reports and/or literature PDFs
3. **Verification action policy** — domain-specific rules for blocking violations

No changes to pipeline code are required.

## Dependencies

- Python 3.12+
- Anthropic API (Claude Sonnet 4, Claude Haiku 4.5)
- Voyage AI API (voyage-4 embeddings)
- LanceDB (vector store)
- LangGraph (agent orchestration)
- Google BigQuery (structured data — requires own database)

## Models Used

| Component | Model | Provider |
|:----------|:------|:---------|
| Intent classification | Claude Sonnet 4 (claude-sonnet-4-6) | Anthropic |
| Evidence synthesis | Claude Sonnet 4 (claude-sonnet-4-6) | Anthropic |
| Claim verification | Claude Haiku 4.5 (claude-haiku-4-5-20251001) | Anthropic |
| Dense embeddings | voyage-4 | Voyage AI |
| Reranker | bge-reranker-base | BAAI |

All commercial model queries were executed in September 2026.

## License

MIT License — see [LICENSE](LICENSE).

## Citation

If you use this code or data, please cite using the metadata in [CITATION.cff](CITATION.cff).
Citation details will be updated upon acceptance.

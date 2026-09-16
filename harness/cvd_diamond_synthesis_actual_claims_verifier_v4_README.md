# CVD synthesis actual-claim verifier corpus

This corpus converts every claim in the existing strict synthesis adjudication
into one independently runnable verifier-v4 fixture.

## Contents

- `cvd_diamond_synthesis_actual_claims_verifier_v4.json`: runnable verifier corpus.
- `cvd_diamond_synthesis_actual_claims_verifier_v4_manifest.csv`: flat audit view with explicit gold label and action.
- `build_cvd_synthesis_actual_verifier_corpus.py`: deterministic corpus builder and source-integrity checks.

## Gold standard

- 1,203 claims from 43 substantive synthesis answer instances.
- 1,070 `supported` / `allow`.
- 132 `unsupported` / `intervene`.
- 1 `contradicted` / `intervene`.
- Gold labels come from `strict_label` in the prior manual synthesis adjudication.

The evaluator requires `gold_checks` to contain only `claim_id` and `status`.
Every fixture therefore also includes `gold_label` and `gold_action` as explicit
metadata. Gold metadata is outside `verifier_input` and is never sent to the
verifier.

`eval_id` is reused across some synthesis reruns. Use the unique `answer_id`
(`source_row` + `eval_id`) when calculating answer-level leakage.

## Run

```bash
python -m agent.verifier_eval \
  harness/cvd_diamond_synthesis_actual_claims_verifier_v4.json \
  results/cvd_diamond_synthesis_actual_claims_verifier_results.csv \
  --summary-output results/cvd_diamond_synthesis_actual_claims_verifier_summary.json
```

The corpus has one fixture per claim, so a full non-deterministic run makes
1,203 verifier requests. Paired/batch-context metrics are not meaningful for
this corpus.

Rebuild with:

```bash
python harness/build_cvd_synthesis_actual_verifier_corpus.py
```

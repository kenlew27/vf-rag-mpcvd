# Database Agent Guidance

The database agent is a bounded retrieval component. It does not perform final
scientific reasoning, invent analyses, or infer causal claims.

## Runtime Contract

1. The supervisor's `structured_data_question` is the only executable semantic
   input.
2. One LLM call emits a strict `DatabaseQuerySpec`; it never emits SQL.
3. Runtime code validates exact live-schema columns and exact stored categorical
   values.
4. Runtime code compiles parameterized SQL for
   `projectdb.curated.experiment_records`, dry-runs it, executes it, and emits a
   lossless evidence packet.
5. The original question may add exact term-mapped context columns to row
   results. It cannot change filtering, grouping, calculations, ordering, or
   limits.

Supported operations are row retrieval/ranking, explicit descriptive
aggregates (`count`, `avg`, `min`, `max`, `stddev_pop`, `corr`), explicit
grouping, and two explicit disjoint numeric cohorts. Regression, causality,
hypothesis tests, clustering, forecasting, joins, and automatic “commonality”
analysis return `unsupported`.

## Active Files

| File | Purpose |
|---|---|
| `database_terms.json` | Small exact phrase-to-context-column map; never query logic |
| `request_planner_terms.json` | Compact request-planner-only aliases and bounded field bundles |
| `evidence_packet_schema.md` | Output packet contract |
| `approved_query_patterns.md` | Supported QuerySpec shapes |
| `query_validation_checklist.md` | Deterministic validation and execution gates |

The runtime schema is authoritative. Other schema and metric notes in this
directory are human reference material and are not injected into the planner.

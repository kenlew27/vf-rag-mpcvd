# Query Validation Checklist

The code path is fixed; no LLM judge, repair model, or post-query projector may
be inserted between these stages.

1. **Schema** — load top-level scalar columns from the one approved table.
2. **Plan once** — pass only the supervisor database question and runtime column
   catalog to the structured-output planner.
3. **Validate shape** — reject unknown fields, incompatible operators/types,
   malformed calculations, invented grouping, or invalid cohorts.
4. **Resolve values** — use normalized exact equality for string values; fail
   closed when zero or multiple fields/values match.
5. **Add context** — for row-shaped queries only, map exact phrases from the
   original question to optional columns. Never alter query semantics.
6. **Compile** — emit SELECT-only parameterized SQL with explicit projection and
   deterministic bounds/order.
7. **Dry-run** — require BigQuery validation and at most 10 GiB scanned.
8. **Execute once** — run the same compiled SQL and parameters.
9. **Conserve results** — preserve every required selected key and value,
   including nulls; keep optional context separate; validate `planner_table`.

Any failure becomes a controlled `clarification`, `unsupported`, or
`execution_failed` packet. The database agent never silently switches to a
different operation.

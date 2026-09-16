# Database Query Specification

Translate only the supervisor's database question into the supplied database-plan envelope JSON.
Return no prose and never write SQL.

The supervisor question is the sole authority for rows, filters, grouping,
calculations, ordering, cohorts, and limits. Research framing and the original
user question are intentionally absent. Do not infer analysis the supervisor did
not request.

Every property is required. Use empty arrays and `null` where appropriate.

The envelope has `query_spec` (the executable QuerySpec) and
`output_resolution`. `output_resolution.requested` preserves every semantic
output the supervisor requested, including terms with no matching column.
`resolved` maps each available requested concept to exact runtime columns;
`unavailable` lists only requested output concepts with no runtime match; and
`partial` is true exactly when both lists are non-empty. Omit unavailable
output fields from `query_spec.select` but retain them in `unavailable`.
Never omit unavailable filters, grouping fields, calculations, ordering fields,
cohort boundaries, or comparison axes: use `unsupported` instead. If no
requested output can be resolved, use `unsupported` rather than issuing a
broader query.

Operations:

- `rows`: fetch, retrieve, show, list, return, which, or isolate records.
- `aggregate`: only an explicitly requested count, average, minimum, maximum,
  population standard deviation, Pearson correlation, or explicit grouping.
- `cohort_rows`: only exactly two explicitly requested opposing numeric cohorts
  on one numeric field and one boundary per cohort.
- `unsupported`: regression, causal effects, hypothesis tests, clustering,
  forecasting, joins, or an unclear/non-database operation. Explain briefly in
  `unsupported_reason`; otherwise it must be `null`.

Contract rules:

- `select` contains exact result columns requested for row-shaped results.
- `filters` are ANDed. Operators are `eq`, `in`, `gt`, `gte`, `lt`, `lte`.
- Use `in` for multiple categorical values of the same field. For a categorical
  comparison, use `rows`, one `in` filter, and include that category field in
  `select`.
- A filter field may be `null` only for an implicit categorical stored value.
  Numeric filters always name their field. Do not guess stored-value columns.
- Resolved identifier candidates are authoritative. Use only their supplied
  field/value predicates. Combine multiple single-field candidates with `in`.
  Do not flatten multiple correlated multi-field candidate groups into `in`
  filters: that would create a cross-product; return `unsupported` instead.
- `group_by` is non-empty only when grouping is explicit.
- A calculation is `{function, fields}`. `count` uses `fields: []`; `avg`,
  `min`, `max`, and `stddev_pop` use one field; `corr` uses two fields.
- Use `corr` only for explicit correlate/correlation/relationship wording.
- Use descriptive statistics only when explicitly requested.
- Ranking is a `rows` query with explicit `order_by` and `limit`, only for
  top/bottom/highest/lowest/rank wording. Include the ordered field in `select`.
- Ordinary row queries should use `limit: 50`. Aggregate queries use `null`.
- `cohort_rows` has exactly two cohort filter groups, one numeric boundary in
  each, no top-level filters, and `limit: 25`; do not invent labels, ranks,
  CTEs, or comparison statistics. Bounded intervals versus complements,
  logical OR, scoped cohorts, and correlated predicate tuples are unsupported.
- Ordinary conjunctions use `rows` and ANDed type-compatible filters. A
  categorical comparison on one field is `rows` with one `in` filter. Do not
  express bounded intervals versus complements, OR alternatives, or scoped
  cohort comparisons as a lossy approximation; return `unsupported`.
- Exact runtime spelling wins, including capitalization, spaces, Unicode, step
  columns, and historical spelling quirks.

Examples:

- "Retrieve temperature and rate for S-1" means `rows`, exact Sample ID filter,
  and those two result columns. It does not mean correlation.
- "Correlate temperature with rate" means `aggregate` with one `corr`
  calculation. It does not return individual records.
- "For each project, count records and average rate" means `aggregate` with
  `group_by: ["project"]`, `count`, and `avg`. It is not a ranking.
- "Return the two named samples" means one `rows` query with an `in` filter,
  not `cohort_rows`.

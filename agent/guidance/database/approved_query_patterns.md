# Approved QuerySpec Patterns

The LLM chooses one relational shape. Deterministic code supplies SQL syntax,
parameters, aliases, ordering defaults, and caps.

| Request | Operation | Shape |
|---|---|---|
| Fetch/list/show records | `rows` | explicit `select` plus exact or numeric `filters` |
| Return named samples | `rows` | one `Sample ID in [...]` filter |
| Top/bottom/highest/lowest | `rows` | selected rank field, explicit `order_by`, bounded `limit` |
| Count/average/min/max/stddev | `aggregate` | explicit calculations only |
| Pearson correlation | `aggregate` | one explicit `corr` calculation with two numeric fields |
| Per project/recipe/category | `aggregate` | explicit `group_by` plus calculations |
| Two numeric ranges | `cohort_rows` | exactly two disjoint one-boundary cohorts |
| Recipe step fields | `rows` | exact runtime step-column names |

Rules:

- Row filters are ANDed. Multiple values for one field use `in`.
- Categorical values resolve by normalized exact equality against stored values;
  missing or ambiguous values request clarification.
- Row results cap at 50; cohort results cap at 25 rows per side.
- `count` aliases to `sample_count`; other aggregate aliases are stable and
  field-derived.
- “Provide the records/data” stays a row query even when the original research
  framing discusses relationships or analysis.
- Regression, causality, hypothesis testing, clustering, forecasting, joins,
  and vague automatic analysis are unsupported rather than approximated.

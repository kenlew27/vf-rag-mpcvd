# Database Evidence Packet

Successful packets preserve every compiled result column, including explicit
`null` values. Required evidence and optional original-question context remain
separate.

```json
{
  "schema_version": "2.1.0",
  "agent": "database_agent",
  "question": "Original user question",
  "database_question": "Supervisor database handoff",
  "query_type": "sample_list | aggregate | comparison",
  "tables_or_views_used": ["projectdb.curated.experiment_records"],
  "sql": "Exact parameterized Standard SQL executed",
  "dry_run_passed": true,
  "rows_returned": 1,
  "row_count_before_limit": 1,
  "required_columns": ["Sample ID", "growth_temp"],
  "context_columns": ["birefringence"],
  "evidence_rows": [{"Sample ID": "S-1", "growth_temp": 910.0}],
  "context_rows": [{"birefringence": null}],
  "output_resolution": {
    "requested": ["growth temperature", "morphology"],
    "resolved": [{"requested": "growth temperature", "columns": ["growth_temp"]}],
    "unavailable": ["morphology"],
    "partial": true
  },
  "limitations": [],
  "clarification_needed": false,
  "execution_failed": false,
  "requires_synthesis": true,
  "planner_table": {
    "columns": [],
    "rows": [],
    "rows_returned": 1,
    "result_limit": 50,
    "truncated": false,
    "schema_caveats": []
  }
}
```

Controlled outcome packets use `query_type: clarification`, `unsupported`, or
`execution_failed`, return no rows or SQL, and put one sanitized reason in
`limitations`. Comparison packets also include a `cohort_results` entry for each
cohort so downstream code can detect an empty side.

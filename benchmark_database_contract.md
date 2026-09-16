# Benchmark database contract (code-only reference)

## Scope and authority

This reference is deliberately limited to version-controlled runtime code and repository Markdown. No BigQuery table contents, metadata, schema API, query results, or environment files were read. Consequently, any fact requiring a table read is marked **Not verified (table access prohibited)**.

Effective runtime source: `tools/bigquery/diamond_search.py`. Supporting repository documentation: `agent/guidance/database/{README,bq_schema_guide,safety_rules,query_validation_checklist,scientist_phrase_mapping,join_paths}.md`.

## Table contract

| Item | Effective runtime behavior |
|---|---|
| Project | `projectdb` |
| Dataset | `curated` |
| Table | `experiment_records` |
| Fully qualified table | `projectdb.curated.experiment_records` |
| Snapshot/frozen-table identifier | Not verified (table access prohibited); runtime references the table, not a snapshot decorator or snapshot table. |
| Total row count | Not verified (table access prohibited). |
| Last-modified metadata | Not verified (table access prohibited). |
| Current runtime limit | `50` (`_RESULT_LIMIT`). |
| Predicate conjunction | Every non-`None` recognized filter is appended in input-dictionary iteration order and joined with `AND`. |
| Ordering | No `ORDER BY` is emitted. Returned row order is therefore not deterministic or contractually defined. The evidence packet preserves the order returned by BigQuery. |
| Projection | Fixed explicit projection below; no `SELECT *` in the current builder. |
| Joins | Unsupported by the current builder. Repository guidance documents joins for other database work, but they are not expressible by its flat filters. |
| Grouping / aggregation | Unsupported by the current builder. |
| Pagination | Unsupported. `LIMIT 50` is fixed; no offset or page token. |
| User-selected columns | Unsupported. |

Repository documentation says approved general database guidance allows `LIMIT` up to 200, but the effective `experiment_records` runtime builder fixes the limit at 50.

### Documentation/runtime discrepancies

- The guidance documents `growth_mean_pressue` as the pressure-mean source spelling, while the current fixed projection selects `growth_mean_pressure`. Neither name was checked against the table schema; effective runtime behavior is the projection spelling.
- Guidance refers to `start_date` / `process_start_date`; the current projection selects `dat_start_time`.
- General safety guidance permits a limit as high as 200, but the runtime query builder always emits 50.
- The runtime dry-run code also applies a configurable `maximum_bytes_billed` value (default one GiB) and separately rejects an estimate above 10 GiB. The documentation records only the latter threshold.

## Complete fixed projection

The exact production select-list order is preserved below. BigQuery type, mode, observed nullability, and null count were intentionally not obtained.

| Position | Column | BigQuery type | Mode | Nullable in observed data | Null count | Notes |
| -------: | ------ | ------------- | ---- | ------------------------: | ---------: | ----- |
| 1 | process_id | Not verified | Not verified | Not verified | Not verified | Repository docs describe `process_id` as STRING. |
| 2 | Sample ID | Not verified | Not verified | Not verified | Not verified | Space and capitalization preserved. |
| 3 | sample_id | Not verified | Not verified | Not verified | Not verified |  |
| 4 | project | Not verified | Not verified | Not verified | Not verified |  |
| 5 | sub_project | Not verified | Not verified | Not verified | Not verified |  |
| 6 | sample_source | Not verified | Not verified | Not verified | Not verified |  |
| 7 | supplier_id | Not verified | Not verified | Not verified | Not verified |  |
| 8 | reactor | Not verified | Not verified | Not verified | Not verified |  |
| 9 | holder_id | Not verified | Not verified | Not verified | Not verified |  |
| 10 | dat_start_time | Not verified | Not verified | Not verified | Not verified | Name preserved exactly; repository documentation elsewhere refers to `start_date`, so this is a documentation/runtime naming discrepancy. |
| 11 | post_characterization_date | Not verified | Not verified | Not verified | Not verified |  |
| 12 | chosen_recipe | Not verified | Not verified | Not verified | Not verified |  |
| 13 | growth_recipe | Not verified | Not verified | Not verified | Not verified |  |
| 14 | growth_phase | Not verified | Not verified | Not verified | Not verified |  |
| 15 | growth_temp | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 16 | growth_duration_hours | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 17 | growth_std_pressue | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified | Existing misspelling preserved. |
| 18 | ramp_temp | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 19 | ramp_duration | Not verified | Not verified | Not verified | Not verified |  |
| 20 | growth_length | Not verified | Not verified | Not verified | Not verified |  |
| 21 | CO2_flag | STRING (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 22 | Gas Status | STRING (runtime filter declaration) | Not verified | Not verified | Not verified | Space and capitalization preserved. |
| 23 | gas_flagged | Not verified | Not verified | Not verified | Not verified |  |
| 24 | ch4_percent | Not verified | Not verified | Not verified | Not verified |  |
| 25 | co2_percent | Not verified | Not verified | Not verified | Not verified |  |
| 26 | h2_percent | Not verified | Not verified | Not verified | Not verified |  |
| 27 | h2n2_percent | Not verified | Not verified | Not verified | Not verified |  |
| 28 | CH4_recipe_proportion | Not verified | Not verified | Not verified | Not verified |  |
| 29 | CO2_recipe_proportion | Not verified | Not verified | Not verified | Not verified |  |
| 30 | H2_recipe_proportion | Not verified | Not verified | Not verified | Not verified |  |
| 31 | H2N2_recipe_proportion | Not verified | Not verified | Not verified | Not verified |  |
| 32 | miscut | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 33 | miscut_angle | Not verified | Not verified | Not verified | Not verified |  |
| 34 | miscut_direction | Not verified | Not verified | Not verified | Not verified |  |
| 35 | center_thickness_mm | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 36 | width_mm | Not verified | Not verified | Not verified | Not verified |  |
| 37 | length_mm | Not verified | Not verified | Not verified | Not verified |  |
| 38 | weight_ct | Not verified | Not verified | Not verified | Not verified |  |
| 39 | grown_thickness_mm | Not verified | Not verified | Not verified | Not verified |  |
| 40 | growth_rate_µm_hr | Not verified | Not verified | Not verified | Not verified | Unicode micro sign `µ` (U+00B5), not ASCII `u`. |
| 41 | growth_mean_reflected | FLOAT64 (runtime filter declaration) | Not verified | Not verified | Not verified |  |
| 42 | growth_mean_pressure | Not verified | Not verified | Not verified | Not verified |  |
| 43 | growth_mean_power | Not verified | Not verified | Not verified | Not verified |  |
| 44 | ramp_mean_reflected | Not verified | Not verified | Not verified | Not verified |  |
| 45 | ramp_mean_pressue | Not verified | Not verified | Not verified | Not verified | Existing misspelling preserved. |
| 46 | birefringence | Not verified | Not verified | Not verified | Not verified |  |
| 47 | birefringence_difference | Not verified | Not verified | Not verified | Not verified |  |
| 48 | root_birefringence | Not verified | Not verified | Not verified | Not verified |  |
| 49 | root_birefringence_difference | Not verified | Not verified | Not verified | Not verified |  |
| 50 | cluster_score | Not verified | Not verified | Not verified | Not verified |  |
| 51 | new_cluster_score | Not verified | Not verified | Not verified | Not verified |  |
| 52 | Weighted PPM | Not verified | Not verified | Not verified | Not verified | Space and capitalization preserved. |
| 53 | hm_main_thickness | Not verified | Not verified | Not verified | Not verified |  |
| 54 | is_corrupt | Not verified | Not verified | Not verified | Not verified |  |
| 55 | TempTrip | Not verified | Not verified | Not verified | Not verified | Capitalization preserved. |
| 56 | WaterTrip | Not verified | Not verified | Not verified | Not verified | Capitalization preserved. |
| 57 | PowerTrip | Not verified | Not verified | Not verified | Not verified | Capitalization preserved. |

## Complete effective filter contract

| Filter key | Logical field | Physical column | Operator | Value type | BigQuery parameter type | Runtime normalization |
| ---------- | ------------- | --------------- | -------- | ---------- | ----------------------- | --------------------- |
| sample_id_exact | sample identifier | `Sample ID` | exact | string | STRING | None. |
| project_exact | project | project | exact | string | STRING | None. |
| project_contains | project | project | contains | string | STRING | `value.lower()`, then parameter is wrapped as `%` + value + `%`. |
| sample_source_exact | sample source | sample_source | exact | string | STRING | None. |
| supplier_id_exact | supplier identifier | supplier_id | exact | string | STRING | None. |
| growth_temp_min | growth temperature | growth_temp | minimum | number | FLOAT64 | None. |
| growth_temp_max | growth temperature | growth_temp | maximum | number | FLOAT64 | None. |
| growth_duration_hours_min | growth duration | growth_duration_hours | minimum | number | FLOAT64 | None. |
| growth_duration_hours_max | growth duration | growth_duration_hours | maximum | number | FLOAT64 | None. |
| growth_std_pressue_min | growth pressure standard deviation | growth_std_pressue | minimum | number | FLOAT64 | None. |
| growth_std_pressue_max | growth pressure standard deviation | growth_std_pressue | maximum | number | FLOAT64 | None. |
| ramp_temp_min | ramp temperature | ramp_temp | minimum | number | FLOAT64 | None. |
| ramp_temp_max | ramp temperature | ramp_temp | maximum | number | FLOAT64 | None. |
| center_thickness_mm_min | center thickness | center_thickness_mm | minimum | number | FLOAT64 | None. |
| center_thickness_mm_max | center thickness | center_thickness_mm | maximum | number | FLOAT64 | None. |
| miscut_min | substrate miscut | miscut | minimum | number | FLOAT64 | None. |
| miscut_max | substrate miscut | miscut | maximum | number | FLOAT64 | None. |
| growth_mean_reflected_min | mean reflected power | growth_mean_reflected | minimum | number | FLOAT64 | None. |
| growth_mean_reflected_max | mean reflected power | growth_mean_reflected | maximum | number | FLOAT64 | None. |
| CO2_flag_exact | CO2 flag | CO2_flag | exact | string | STRING | None. |
| gas_status_exact | gas status | `Gas Status` | exact | string | STRING | None. |

### Filter semantics

- **Unknown key:** Any non-`None` key not in the derived allowlist raises `UnknownFilterFieldError`; nested input is therefore rejected as an unknown key.
- **Null (`None`) value:** skipped before allowlist validation; it contributes no predicate or parameter.
- **Wrong type:** `build_query()` performs no Python type validation. `FILTER_SCHEMA` describes string/number types for LLM structured output, while the BigQuery client receives declared STRING or FLOAT64 scalar parameters. Exact BigQuery coercion/rejection was not exercised.
- **Duplicate JSON keys:** `retrieve_bigquery()` parses LLM output with Python `json.loads`; standard parsing retains the last occurrence. The JSON Schema itself cannot detect duplicates. This behavior is inferred from code, not integration-tested here.
- **Contains wildcard behavior:** `%` and `_` in `project_contains` are not escaped. Because the value is placed in a `LIKE` pattern, they retain BigQuery wildcard semantics.
- **Case behavior:** exact predicates use `=` with no normalization; case sensitivity follows the BigQuery string comparison. Contains uses `LOWER(project) LIKE @p_project_contains`; the user value is lowercased, yielding case-insensitive substring matching for ordinary case mappings.
- **Numeric boundaries:** minimum is inclusive (`>=`); maximum is inclusive (`<=`). Supplying both yields an inclusive bounded range joined with `AND`.

## SQL actually used for this reference

None. No table SQL was executed by design.

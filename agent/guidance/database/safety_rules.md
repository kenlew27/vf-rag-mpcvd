# BigQuery Database Agent Safety Rules

Strict safety constraints and operational rules for the database agent. Any deviation triggers an immediate execution abort.

---

## Rules

### 1. SELECT-Only Query Policy
- Only `SELECT` statements are permitted.
- Forbidden: `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `RENAME`, `GRANT`, `REVOKE`, `EXPORT`.

### 2. Approved Tables Only
1. `projectdb.warehouse.process_display`
2. `projectdb.warehouse.process_metrics`
3. `projectdb.warehouse.growth_metrics`
4. `projectdb.recipe.recipe_mapping`
5. `projectdb.analysis.cluster_scores`
6. `projectdb.curated.experiment_records`

Direct queries to logging or raw staging tables are forbidden.

### 3. Schema Exactness
- Growth rate: **`growth_rate_µm_hr`** (Unicode µ, U+00B5). ASCII `u` will fail.
- Pressure: **`growth_mean_pressue`** / **`growth_std_pressue`** (spelled without 'r'). `pressure` will fail.
- No Raman columns: `raman_fwhm_cm1` and `raman_quality_label` do not exist. Cast as `NULL` or omit.

### 4. No `SELECT *`
- Every query must explicitly specify required columns.

### 5. Mandatory `LIMIT` Clauses
- Default: 50 rows. Maximum: 200 rows.
- Exception: pure aggregates (`SELECT COUNT(*)`, `SELECT AVG(...) GROUP BY`).

### 6. Mandatory Dry-Runs
- Before executing any SQL, perform a BigQuery dry-run to validate syntax and estimate scan size.
- If scan estimate exceeds **10 GB**, abort and refine the query (add date filters or restrict columns).

### 7. SQL Injection Mitigation
- Never construct queries using direct string interpolation of user parameters.
- Use parameterized query parameters (`ScalarQueryParameter`) for all values.

### 8. Publication-Safe Mode
- When `publication_safe_mode` is active, queries retrieving `CH4%`, `CO2%`, `H2N2%`, exact temperatures, pyrometer readings, or microwave power settings must transform values into ranges or relative descriptions.

### 9. No Causal Language
- The database agent is an evidence collector, not a scientist.
- Never use phrases like "This shows that pressure instability caused the defect."

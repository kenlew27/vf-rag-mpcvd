# BigQuery Join Paths

Authorized join paths between physical tables in `projectdb`. The database agent must strictly adhere to these paths.

---

## Table Join Reference

| Left Table | Right Table | Join Keys | Join Type | Purpose |
|---|---|---|---|---|
| `warehouse.process_display` | `warehouse.process_metrics` | `process_id` | `INNER JOIN` | Combine growth/birefringence metrics with recipe telemetry |
| `warehouse.process_display` | `recipe.recipe_mapping` | `recipe` = `recipe` | `LEFT JOIN` | Compare actual outcomes with planned recipe setpoints |
| `warehouse.process_display` | `analysis.cluster_scores` | `sample_id` | `LEFT JOIN` | Correlate birefringence stress profiles with image URIs |
| `warehouse.process_display` | `curated.experiment_records` | `process_id` | `INNER JOIN` | Filter display metrics to clean, validated curated runs |

---

## Join Caveats

### 1. Process ID Type Mismatches
- `process_id` is `STRING` (primary key).
- `new_process_id` is `FLOAT` (numerical representation).
- When joining on numerical identifier to string, apply explicit type casting:
  ```sql
  ON CAST(d.new_process_id AS STRING) = m.process_id
  ```
  Prefer joining on `process_id` directly since it is `STRING` in both `display_metrics` and `process_metrics`.

### 2. Standardize Recipe Joins
- Use `TRIM(recipe)` to avoid matching failures from trailing whitespace:
  ```sql
  ON TRIM(d.recipe) = TRIM(r.recipe)
  ```

### 3. Prevent Cartesian Products
- A single `sample_id` can appear in multiple runs. Joining on `sample_id` alone will duplicate rows.
- Always join on `process_id` to maintain a strict 1-to-1 relationship.
- Only join on `sample_id` when performing sample-history tracing or connecting to `cluster_scores`.

### 4. Birefringence and Defect Joining
- Birefringence data is in `display_metrics`; `hm_notes` and `morphology_label` are in `process_metrics`.
- To correlate surface morphology with strain:
  ```sql
  SELECT d.process_id, d.birefringence, m.morphology_label
  FROM `projectdb.warehouse.process_display` AS d
  INNER JOIN `projectdb.warehouse.process_metrics` AS m
    ON d.process_id = m.process_id
  WHERE d.birefringence IS NOT NULL;
  ```

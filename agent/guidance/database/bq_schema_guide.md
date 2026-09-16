# BigQuery Schema Guide

Describes the analytical schemas for the CVD diamond growth database. Maps logical views to the actual physical tables in the `projectdb` project.

> **Crucial Schema Mismatches**:
> 1. **Micro Symbol (`µ`)**: The growth rate column is named **`growth_rate_µm_hr`** (Greek micro `µ`, U+00B5), NOT `growth_rate_um_hr`.
> 2. **Pressure Spelling Error**: Pressure columns are spelled **`pressue`** (missing the 'r'): `growth_mean_pressue`, `growth_std_pressue`.
> 3. **Raman Quality Data**: `raman_fwhm_cm1` is **not** in BigQuery. Fall back to the LlamaParse vector store.

---

## Physical Table Index

| Logical View | Physical Source | Purpose |
|---|---|---|
| `vw_process_summary` | `warehouse.process_display` or `curated.experiment_records` | Administrative/process metadata |
| `vw_growth_metrics` | `warehouse.process_display` | Thickness, growth rates, dimensions |
| `vw_recipe_features` | `recipe.recipe_mapping` or `warehouse.process_metrics` | Gas flow setpoints and recipe configs |
| `vw_process_trace_features` | `warehouse.process_metrics` | Telemetry stability and sensor variance |
| `vw_characterization_summary` | `warehouse.process_display` + `warehouse.process_metrics` | Post-growth birefringence, morphology |
| `vw_process_recipe_join` | `warehouse.process_metrics` | Run outcomes linked to recipe gas settings |
| `vw_run_comparison_features` | `warehouse.process_display` or `warehouse.process_metrics` | Side-by-side run comparisons |

---

## `vw_process_summary` — `warehouse.process_display` / `curated.experiment_records`

**Use when**: Identifying runs by reactor, date window, project, sub-project, or holder ID.

| Logical Column | Physical Column | Type |
|---|---|---|
| `process_id` | `process_id` (or `new_process_id` as FLOAT) | STRING |
| `sample_id` | `sample_id` | STRING |
| `reactor` | `reactor` | STRING |
| `recipe_name` | `recipe` | STRING |
| `holder_id` | `holder_id` | STRING |
| `process_start_date` | `start_date` | TIMESTAMP |
| `process_end_date` | `end_date` | TIMESTAMP |
| `project` | `project` | STRING |
| `sub_project` | `sub_project` | STRING |
| `growth_duration_hr` | `growth_duration_hours` | FLOAT |

---

## `vw_growth_metrics` — `warehouse.process_display`

**Use when**: Querying growth speed, thickness yield, and dimensional parameters.

| Logical Column | Physical Column | Type |
|---|---|---|
| `growth_rate_um_hr` | **`growth_rate_µm_hr`** *(Unicode µ)* | FLOAT |
| `grown_thickness_mm` | `grown_thickness_mm` | FLOAT |
| `center_thickness_mm` | `center_thickness_mm` | FLOAT |
| `growth_duration_hr` | `growth_duration_hours` | FLOAT |

---

## `vw_recipe_features` — `warehouse.process_metrics`

**Use when**: Reviewing CH4, CO2, N2, H2 recipe concentrations.

| Logical Column | Physical Column | Type |
|---|---|---|
| `recipe_id` | `recipe` | STRING |
| `recipe_family` | `recipe_rm` | STRING |
| `growth_ch4_percent` | `` `CH4%` `` | FLOAT |
| `growth_co2_percent` | `` `CO2%` `` | FLOAT |
| `growth_n2_percent` | `` `H2N2%` `` | FLOAT |

---

## `vw_process_trace_features` — `warehouse.process_metrics`

**Use when**: Assessing plasma stability, pressure drift, or microwave power stability.

| Logical Column | Physical Column | Type |
|---|---|---|
| `growth_pressure_mean_torr` | **`growth_mean_pressue`** *(spelled pressue)* | FLOAT |
| `growth_pressure_std_torr` | **`growth_std_pressue`** *(spelled pressue)* | FLOAT |
| `growth_power_mean_w` | `growth_mean_power` | FLOAT |
| `growth_power_std_w` | `growth_std_power` | FLOAT |
| `growth_reflected_power_mean_w` | `growth_mean_reflected` | FLOAT |
| `growth_reflected_power_std_w` | `growth_std_reflected` | FLOAT |
| `growth_temp_mean_c` | `growth_temp` | FLOAT |
| `anomaly_flag` | `gas_flagged` / `Temp_flag` | BOOL / STRING |

---

## `vw_characterization_summary` — `warehouse.process_display` + `warehouse.process_metrics`

**Use when**: Reviewing birefringence, retardance, morphology, or defect notes.

| Logical Column | Physical Column | Source Table |
|---|---|---|
| `birefringence_score` | `birefringence` | `display_metrics` |
| `root_birefringence` | `root_birefringence` | `display_metrics` |
| `morphology_label` | `morphology_label` | `process_metrics` |
| `defect_notes` | `hm_notes` | `process_metrics` |
| `raman_fwhm_cm1` | **N/A — not in BigQuery** | vector store |

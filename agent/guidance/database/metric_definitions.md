# CVD Diamond Growth Metric Definitions

Definitions, optimization notes, and warnings for physical database metrics. The database agent must use the exact physical column names defined here.

---

## CRITICAL AGENT WARNINGS

1. **Growth Rate Column (`µ`)**: The growth rate column is **`growth_rate_µm_hr`** (Greek micro character `µ`, U+00B5). Querying `growth_rate_um_hr` with a standard `u` will fail.
2. **Pressure Spelling Error (`pressue`)**: Pressure columns are spelled **`pressue`** (missing the 'r'): `growth_mean_pressue`, `growth_std_pressue`. Specifying `pressure` will fail.
3. **Missing Raman Columns**: `raman_fwhm_cm1` and `raman_quality_label` do not exist in BigQuery. Return `NULL` for these and redirect to the LlamaParse vector store.

---

## 1. Physical Growth Metrics

### `growth_rate_µm_hr`
- **Definition**: Average rate of diamond thickness growth, calculated as `grown_thickness_mm * 1000 / growth_duration_hours`.
- **Scientist Says**: "growth speed", "growth rate", "fast growth"
- **Optimization**: Higher is generally better for throughput, but only if crystal quality is maintained.
- **Caveat**: Fast growth frequently results in polycrystalline growth or crack defects if plasma conditions are unstable.
- **Tables**: `warehouse.process_display`, `warehouse.process_metrics`

### `grown_thickness_mm`
- **Definition**: Physical height added to the diamond crystal during the process (mm).
- **Scientist Says**: "grown thickness", "thickness added"
- **Tables**: `warehouse.process_display`

### `growth_duration_hours`
- **Definition**: Total elapsed hours in active growth stages.
- **Scientist Says**: "growth duration", "growth hours"
- **Tables**: `warehouse.process_display`

---

## 2. Reactor Telemetry & Stability Metrics

### `growth_mean_pressue` *(note: spelled `pressue`)*
- **Definition**: Average gas pressure inside the CVD reactor chamber during active growth (Torr).
- **Scientist Says**: "growth pressure", "average pressure"
- **Tables**: `warehouse.process_metrics`

### `growth_std_pressue` *(note: spelled `pressue`)*
- **Definition**: Standard deviation of chamber pressure during active growth; indicates gas flow or throttle valve instability.
- **Scientist Says**: "pressure stability", "pressure variance"
- **Optimization**: Lower is better (closer to 0 = high stability).
- **Tables**: `warehouse.process_metrics`

### `growth_mean_power`
- **Definition**: Average microwave generator power delivered to the plasma during growth (Watts).
- **Scientist Says**: "microwave power", "average forward power"
- **Tables**: `warehouse.process_metrics`

### `growth_std_power`
- **Definition**: Standard deviation of microwave generator power during the growth phase.
- **Scientist Says**: "power stability", "microwave fluctuations"
- **Optimization**: Lower is better.
- **Tables**: `warehouse.process_metrics`

### `growth_mean_reflected`
- **Definition**: Average microwave power reflected back from the cavity into the generator (tuning mismatch), in Watts.
- **Scientist Says**: "reflected power", "tuning mismatch"
- **Optimization**: Lower is better.
- **Tables**: `warehouse.process_metrics`

### `growth_std_reflected`
- **Definition**: Standard deviation of reflected microwave power over the growth phase.
- **Scientist Says**: "reflected power instability", "coupling stability"
- **Optimization**: Lower is better.
- **Tables**: `warehouse.process_metrics`

### `growth_temp` / `top_temp_mean`
- **Definition**: Average temperature of the substrate holder or top face of the diamond during growth (°C).
- **Scientist Says**: "growth temperature", "pyrometer reading"
- **Tables**: `warehouse.process_metrics`, `warehouse.process_display`

### `gas_flagged` / `Temp_flag`
- **Definition**: Telemetry flags indicating flow or temperature deviations exceeding standard threshold tolerances.
- **Scientist Says**: "anomalous run", "did anything go wrong"
- **Optimization**: `False` / `NORMAL` is desired.
- **Tables**: `warehouse.process_metrics`

---

## 3. Post-Growth Characterization Metrics

### `birefringence` / `root_birefringence`
- **Definition**: Quantitative measure of optical stress/strain birefringence (refractive index anisotropy).
- **Scientist Says**: "birefringence score", "optical strain"
- **Optimization**: Lower is better (critical for optical-grade diamond).
- **Tables**: `warehouse.process_display`, curated tables

### `morphology_label`
- **Definition**: Categorical classification of the diamond surface structure after growth (e.g., "monocrystalline", "polycrystalline", "rough").
- **Scientist Says**: "morphology", "surface defects", "edge cracking"
- **Optimization**: Target morphology is typically "monocrystalline".
- **Caveat**: Visual morphology is a qualitative operator assessment — not a quantitative physical measurement.
- **Tables**: `warehouse.process_metrics`

### `raman_fwhm_cm1` / `raman_peak_position_cm1`
- **Definition**: Raman spectral quality metrics (FWHM and peak location).
- **Scientist Says**: "Raman quality", "diamond purity"
- **IMPORTANT**: Not in BigQuery. Return `NULL` and redirect to the LlamaParse vector store.

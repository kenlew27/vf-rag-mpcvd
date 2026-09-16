# Scientist Phrase Mapping

Translation dictionary mapping common informal scientist terminology to physical BigQuery tables and columns, and to the flat filter keys used by the database agent.

---

## Semantic Translation Dictionary

| Scientist Phrase | Database Intent | Target Table | Target Column(s) | Flat Filter Keys | Caveats |
|---|---|---|---|---|---|
| **"fast growth"** / **"high speed"** | High deposition rate | `warehouse.process_display` | `growth_rate_µm_hr` | (no direct key — use as sort in SQL) | Uses Unicode µ. Does not verify crystal quality. |
| **"best recipe"** | Recipe with high yield or quality | `warehouse.process_display` | `recipe`, `growth_rate_µm_hr`, `birefringence` | — | Requires synthesis to define "best" (grade dependent). |
| **"good run"** | High growth rate, low stress | `warehouse.process_display` | `growth_rate_µm_hr`, `birefringence` | — | Defined by application (Quantum vs. Optical). |
| **"bad run"** | Low rate, high stress, or anomaly | `warehouse.process_metrics` | `gas_flagged`, `Temp_flag`, `morphology_label` | `CO2_flag_exact`, `gas_status_exact` | Verify if run was aborted early. |
| **"stable plasma"** | Low telemetry variance | `warehouse.process_metrics` | `growth_std_pressue`, `growth_std_power` | `growth_std_pressue::lt` or `growth_std_pressue::lte` | Telemetry stability doesn't guarantee recipe optimization. |
| **"pressure drift"** | Chamber pressure deviation | `warehouse.process_metrics` | `growth_std_pressue` | `growth_std_pressue::gt` or `growth_std_pressue::gte` (high) | Spelled `pressue` (no 'r'). |
| **"power instability"** | Microwave power fluctuation | `warehouse.process_metrics` | `growth_std_power` | — | Can indicate generator noise or coupling swings. |
| **"reflected power issue"** | Microwave cavity mismatch | `warehouse.process_metrics` | `growth_mean_reflected`, `growth_std_reflected` | `growth_mean_reflected::gt` or `growth_mean_reflected::gte` | Often leads to automatic safety trips. |
| **"rough surface"** / **"roughness"** | Non-planar growth morphology | `warehouse.process_metrics` | `morphology_label`, `hm_notes` | — | Qualitative field determined by operator. |
| **"Raman quality"** | Spectral purity indicator | N/A | N/A | N/A | Not in BigQuery — redirect to vector store. |
| **"birefringence"** | Internal crystal stress | `warehouse.process_display` | `birefringence`, `root_birefringence` | — | Localized strain fields may be missed by average. |
| **"same holder"** | Runs on the same carrier | `warehouse.process_display` | `holder_id` | — | Pockets on carrier have thermal variations. |
| **"recent runs"** | Runs from last X days | `warehouse.process_display` | `start_date` | — | Ensure timezone matches UTC storage. |
| **"recipe family"** | Group of similar recipes | `warehouse.process_metrics` | `recipe_rm` | — | Recipe families must be exact strings. |
| **"methane"** / **"CH4"** | Carbon source gas flow | `warehouse.process_metrics` | `` `CH4%` `` | — | Methane flow ratios dictate growth rate. |
| **"nitrogen"** / **"N2"** | Doping gas flow | `warehouse.process_metrics` | `` `H2N2%` `` | — | Nitrogen accelerates growth but causes yellowing. |
| **"growth temperature"** / **"pyrometer"** | Substrate surface temperature | `curated.experiment_records` | `growth_temp` | `growth_temp::gt`, `growth_temp::gte`, `growth_temp::lt`, `growth_temp::lte` | In experiment_records, the column is `growth_temp`. |
| **"thick sample"** / **"center thickness"** | Substrate thickness | `curated.experiment_records` | `center_thickness_mm` | `center_thickness_mm::gt`, `center_thickness_mm::gte`, `center_thickness_mm::lt`, `center_thickness_mm::lte` | Pre-growth substrate thickness, not grown thickness. |
| **"long run"** / **"short run"** | Growth duration | `curated.experiment_records` | `growth_duration_hours` | `growth_duration_hours::gt`, `growth_duration_hours::gte`, `growth_duration_hours::lt`, `growth_duration_hours::lte` | Total active growth time in hours. |
| **"miscut angle"** | Substrate crystallographic miscut | `curated.experiment_records` | `miscut` | `miscut::gt`, `miscut::gte`, `miscut::lt`, `miscut::lte` | Affects growth mode and surface morphology. |
| **"project"** | Internal project label | `curated.experiment_records` | `project` | `project_exact`, `project_contains` | Case-sensitive exact match or substring. |
| **"sample source"** / **"supplier"** | Origin of substrate | `curated.experiment_records` | `sample_source`, `supplier_id` | `sample_source_exact`, `supplier_id_exact` | Use for filtering by substrate vendor. |

---

## Complex Causal Queries

When a scientist asks:
- *"Why did this run fail?"*
- *"What should we try next?"*
- *"How do we prevent this defect?"*
- *"Compare these runs and tell me what changed."*

**Boundary Rule**: The database agent must **not** construct causal explanations, propose recipes, or recommend parameters. Retrieve the relevant data, compile it into an evidence packet, and pass to the Scientific Synthesis Agent.

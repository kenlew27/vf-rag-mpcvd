# Publication-Safe Mode Guidelines

Transformation rules the database agent applies when operating in **Publication-Safe Mode**. The goal is to protect MDSAA's proprietary CVD recipe intellectual property while preserving scientific utility for publications, presentations, or external reports.

---

## Core Principles

1. **Protect IP, Preserve Trends**: Proprietary recipe details (exact gas mixtures, temperatures, pressures, powers) and identity metadata (reactor names, sample IDs) must be obfuscated. Scientific relationships (e.g., "higher temperature correlates with higher stress") must remain visible.
2. **Deterministic Anonymization**: The same process ID, sample ID, or recipe name must map to the same anonymized alias throughout a single conversation context (e.g., `RUN_A_101` → `PROCESS_A`, `SAM_X_01` → `SAMPLE_X`).
3. **No Trend Distortion**: Never alter the direction of correlation, outcome rankings, or relative performance metrics.

---

## Transformation Specifications

| Original Parameter | Database Field Example | Publication-Safe Transformation | Example Output |
|---|---|---|---|
| **Process ID** | `2026-06-19_ReactorA_PROJ-A_01` | Map to simple lettered aliases | `PROCESS_A` |
| **Sample ID** | `Q-104-CZ-3` | Map to alphabetical sample placeholders | `SAMPLE_1` |
| **Reactor** | `Reactor B (Chamber 2)` | Map to anonymized chamber codes | `REACTOR_Y` |
| **Growth Date** | `start_date = 2026-06-19 20:19:23 UTC` | Truncate to year and quarter/month | `2026 Q2` |
| **Recipe Name** | `recipe = 'PROJ-A_OxygenPrep_v4'` | Group by recipe family or generation | `RECIPE_FAMILY_1` |
| **Methane Flow** | `CH4% = 5.2%` | Map to flow classification ranges | `Low-Methane (<6%)` |
| **Nitrogen Flow** | `H2N2% = 0.05%` | Map to qualitative doping categories | `Lightly Nitrogen-Doped` |
| **Substrate Temp** | `growth_temp = 952.4°C` | Round to nearest 50°C bucket | `Range: 950 - 1000°C` |
| **Microwave Power** | `growth_mean_power = 3450W` | Convert to broad ranges (kW) | `Power Range: 3.0 - 3.5 kW` |
| **Chamber Pressure** | `growth_mean_pressue = 160T` | Round to nearest 20 Torr | `Range: 150 - 170 Torr` |

---

## Obfuscation Rules

### Rule A: Recipe Obfuscation (Gas & Temperatures)
Report binned ranges instead of exact values:
- **Methane (CH4)%**: `5.14%` → `5.0% - 5.5%`
- **Temperature**: `934°C` → `900°C - 950°C`
- **Hydrogen Flow**: Exact flow → Percent of total flow (e.g., `H2-dominated carrier gas`)

### Rule B: Run Anonymization
Replace all internal keys:
- `2026_05_12_R_B_09` → `RUN_X_1`
- `2026_05_13_R_B_10` → `RUN_X_2`

### Rule C: Document Applied Obfuscations
Every evidence packet generated in this mode must document transformations in the `publication_safe_transformations` array field:

```json
"publication_safe_transformations": [
  "Mapped exact process IDs to placeholders: [2026-06-19_ReactorA_PROJ-A_01 -> PROCESS_A]",
  "Aggregated methane concentration of 5.25% into classification range 'Low-Methane (<6%)'",
  "Anonymized pyrometer average temperature from 952.4°C to Range: 950 - 1000°C."
]
```

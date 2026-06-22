# AI Powered Simulation Stack

A structured guidance and validation system for AI-assisted simulations of
resonators, couplers, TWPAs, and qubit chip elements.

Lab mates type natural language. Claude drives the entire pipeline.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Layer 1 · Design Intent                            │
│  device_templates/  +  prompts/design_intake.md     │
│  User fills target values via AI conversation       │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  Layer 2 · CAD                                      │
│  prompts/cad_conversation.md                        │
│  gdstk scripts → .gds file → cad_verify_gds.py     │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  Layer 3 · Materials                                │
│  prompts/material_selection.md                       │
│  Human confirms substrate, metal, loss model        │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  Layer 4 · COMSOL Engine                            │
│  COMSOL Simulation Suite MCP server (19 tools)      │
│  build_comsol_model / run_custom_comsol_build       │
│  run_eigenfrequency_study  ← NEW                    │
│  run_stub_length_sweep                              │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  Layer 5 · Automated Analysis + Tuning              │
│  scripts/analysis/plot_results.py                   │
│  prompts/result_interpretation.md                   │
│  prompts/automated_grid_search.md                   │
│  Grid search: simulate → score → update params      │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  Layer 6 · Final Design                             │
│  sessions/<device>_<date>/session.yaml              │
│  AI records best candidate and accepted outputs     │
│  User reviews converged design                      │
└─────────────────────────────────────────────────────┘
```

---

## Supported devices

| Device    | Template                          | Primary study         |
|-----------|-----------------------------------|-----------------------|
| Resonator | `device_templates/resonator.yaml` | eigenfrequency → freq sweep |
| Coupler   | `device_templates/coupler.yaml`   | eigenfrequency → freq sweep |
| TWPA      | `device_templates/twpa.yaml`      | stub-length sweep + ABCD fit |
| Transmon  | `device_templates/transmon.yaml`  | eigenfrequency (multi-mode) |

---

## Supported studies

| Study              | MCP tool                   | Script                           | Time    |
|--------------------|----------------------------|----------------------------------|---------|
| Eigenfrequency     | `run_eigenfrequency_study` | `eigenfrequency_analysis.py`     | ~5 min  |
| Frequency sweep    | `run_stub_length_sweep` or custom | `sweep_stub_length.py`    | ~30 min |
| Stub-length sweep  | `run_stub_length_sweep`    | `sweep_stub_length.py`           | ~1–3 h  |
| Automated tuning   | `prompts/automated_grid_search.md` | selected study + scoring loop | budget-dependent |

---

## Output organization

Each device session produces:

```
stack/sessions/<device>_<YYYYMMDD>/
  design_params.yaml          ← filled by AI intake conversation
  session.yaml                ← iteration log (written by AI after each result)
  grid_search.yaml            ← optional machine-readable trial plan/results
  iter_001_initial/
    eigenfrequencies.csv
    eigenfrequency_result.mph
    eigenfreqs.png
  iter_002_length_adjusted/
    eigenfrequencies.csv
    ...
```

All `.mph`, `.csv`, and `.png` data files are gitignored.

---

## Material properties (quick reference)

| Material  | εr    | tan δ          | Metal σ       | Model at mK |
|-----------|-------|----------------|---------------|-------------|
| Si        | 11.7  | ~1e-5 to 1e-4  | —             | PEC         |
| AlN       | 8.9   | ~1e-4 to 1e-3  | —             | PEC         |
| Sapphire  | 9.39  | <1e-7          | —             | PEC         |
| Al        | —     | —              | 5.88e7 S/m    | PEC         |
| Nb        | —     | —              | 6.74e6 S/m    | PEC         |
| NbTiN     | —     | —              | ~2.5e6 S/m    | PEC         |

Full details and confirmation step: `prompts/material_selection.md`

---

## Files in this directory

```
stack/
  README.md                 ← this file
  WALKTHROUGH.md            ← 6-step golden path walkthrough
  device_templates/
    resonator.yaml
    coupler.yaml
    twpa.yaml
    transmon.yaml
  prompts/
    design_intake.md        ← Step 1: collect targets and geometry
    cad_conversation.md     ← Step 2: generate GDS
    material_selection.md   ← Step 3: post-CAD substrate/metal confirmation
    study_selection.md      ← Step 4: which study to run
    automated_grid_search.md← Step 5: automated parameter tuning loop
    result_interpretation.md← Step 5: read CSV and score trial results
  sessions/                 ← created at runtime (gitignored data inside)
```

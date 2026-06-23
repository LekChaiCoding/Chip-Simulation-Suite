# Pipeline data flow & formats

This documents what flows between stages, so you can see where each tool plugs in
and what the COMSOL→fitting handoff looks like.

```
 converter_group_recreation.py              [tool: generate_cad]
        │  gdstk, absolute chip coords
        ▼
 converter_group_recreation.gds  ───────────[tool: verify_cad → PASS/FAIL]
        │  material_selection.md: human confirms substrate, metal, loss model
        │  ECAD import + confirmed material_params
        ▼
 recreate_and_solve.py                       [tool: build_comsol_model]
        │  COMSOL: geometry + physics + mesh
        ▼
 *_built.mph  ──────────────────────────────[tool: validate_geometry]
        │  solve per excited port
        ▼
 sweep_stub_length.py                        [tool: run_stub_length_sweep]
        │  complex S11/S21 vs (stub, freq)
        ▼
 stub_length_sweep.dat  ────────────────────[tool: export_touchstone → .s2p]
        │  fed to fitting
        ▼
 abcd_fit.py / fit_stub_sweep.jl             [tools: run_abcd_fit / fit_stub_sweep]
        │  fit lumped Cg, implied Z0
        ▼
 abcd_fit_results.csv  ─────────────────────[tool: analyze_dispersion]
        │  Bloch k(ω), Δk = 2k_p − k_s − k_i
        ▼
 dispersion_analysis.csv, delta_k.csv
```

For general resonator, coupler, transmon, and custom-device work, the same
CAD -> material confirmation -> COMSOL boundary is followed, but the analysis
stage is usually an automated tuning loop:

```
 design_params.yaml + verified GDS + confirmed material_params
        │
        ▼
 automated_grid_search.md
        │  choose approved parameter ranges and tolerances once
        │
        ├─► generate/update CAD if geometry changed
        ├─► build/update COMSOL model with confirmed materials
        ├─► run eigenfrequency, sweep, or fitting study
        ├─► read the actual CSV output
        ├─► score the trial against design targets
        └─► refine the grid until accepted or budget exhausted
        │
        ▼
 accepted design_params.yaml + final .mph/.csv/.png outputs
```

## Key file formats

### GDS layers (`converter_group_recreation.gds`)
21-junction fishbone ladder, 17 µm unit-cell pitch, absolute coords (CPW
centreline at y = 2500 µm). Layers: `0` metal etch, `1` junction bars,
`3` bandage plate, `4` pillar pads, `11` end blocks, `51` markers.

### Stub-length sweep (`stub_length_sweep.dat`)
CSV; one row per (stub length, frequency). **Complex** S-parameters (Re/Im — not
dB), because phase carries the L·C information the fit needs:

```
# stub_length_sweep.dat
stub_length_um, freq_hz, S11_re, S11_im, S21_re, S21_im
300.0, 1e9, 0.017, 0.021, 0.770, -0.637
...
```

### Touchstone (`.s2p`, optional)
Standard `# Hz S RI R 50` full 2×2 matrix; produced by `export_touchstone`.

### ABCD fit results (`abcd_fit_results.csv`)
One row per (stub, topology, fit objective):

```
stub_um, topology, fit_method, Cg_fit_fF, Leff_fit_pH,
s11_complex_rmse, s21_complex_rmse, s11_phase_rmse_deg, s21_phase_rmse_deg,
Z0_implied_ohm, note
```

The **canonical** row to read is `topology=topoA, fit_method=fitA` (the complex
residual matching the Julia reference). For bridge/003 that gives:

| stub µm | Cg fF | Z0 Ω |
|--------:|------:|-----:|
| 300 | 109.9 | 51.2 |
| 340 | 124.7 | 48.1 |
| 400 | 147.5 | 44.2 |

Other (topology, objective) rows are an *intentional* sweep of alternatives;
some collapse in the 300–400 µm breakdown region — that is the point of the
comparison, not a bug.

### Automated grid-search log (`session.yaml`)
For AI-driven tuning, each candidate records the tested parameters, output files,
target errors, normalized score, status, and the current best design. The loop is
based on the tuning pattern in `Z:\users\ishida\backup\python_script`: update
COMSOL parameters, regenerate geometry/mesh, run the study, extract numerical
results, score the trial, and keep the best candidate.

---

## AlNtransmon pipeline walkthrough (D0–D6)

This shows how the generic tools map onto a multi-stage transmon qubit design
pipeline. The stage labels (D0–D6) are arbitrary — any pipeline can define its
own stage names in the `stage_map` passed to `get_pipeline_session_plan`.

### Start of every session

```python
get_pipeline_session_plan(
    yaml_path = "design_Q0/design_params.yaml",
    stage_map = {
        "D0_capacitance":    ["design_Q0.qubit.d_q"],
        "D1_qr_coupling":    ["design_Q0.qr_coupler.delta_angle_coupler"],
        "D1_1_drive_port":   ["design_Q0.drive_spokes.n"],
        "D2_readout_freq":   ["design_Q0.readout_resonator.l_slider_single"],
        "D3_notch_position": ["design_Q0.filter.l_end"],
        "D4_filter_freq":    ["design_Q0.filter.l_slider_single"],
        "D5_unit_cell":      ["design_Q0.readout_port.spiral_turns"],
    })
# → next_stage: "D1_qr_coupling"  (if D0 is already done)
```

Then enter plan mode, confirm the sweep parameters, get approval.

### D0 — Transmon pad capacitance

Set the qubit pad diameter `d_q` to hit target `EC_Hz` (charging energy).

```python
# 1. Run eigenfrequency to get mode frequencies
run_eigenfrequency_study(mph_path="qubit_built.mph", n_modes=3,
    extract_fields=True, path_selections=["qubit_path"], dry_run=False)

# 2. Extract coupling / EC from the CSV
compute_circuit_params(f0_Hz=5.12e9, L_H=12e-9)
# → EC_Hz, C_F, EJ_Hz, fq_Hz, anh_Hz

# 3. Write back
design_params_write("design_params.yaml", "design_Q0.qubit.d_q", 120.5)
```

### D1 — Qubit–resonator coupling angle

Sweep `delta_angle_coupler` to hit target coupling `g_qr`.

```python
run_geometry_param_sweep(
    mph_path = "qubit_res_built.mph",
    param_name = "delta_angle_coupler",
    param_values = [15, 20, 25, 30, 35],
    param_unit = "deg",
    study_type = "eigenfrequency",
    extract_fields = True,
    path_selections = ["resonator_path", "qubit_path"],
    dry_run = False)

run_coupling_extraction(
    eigenfreq_csv = "runs/.../delta_angle_coupler_sweep.csv",
    mode1_path_col = "path_resonator_path",
    mode2_path_col = "path_qubit_path",
    lumped_inductance_H = 12e-9)
# → g_Hz, chi_Hz per sweep point → invert to find angle for target g

design_params_write("design_params.yaml",
                    "design_Q0.qr_coupler.delta_angle_coupler", 22.5)
```

### D2 — Readout resonator frequency

Sweep `l_slider_single` (resonator length slider) to hit target `fr_Hz`.

```python
run_geometry_param_sweep(
    mph_path = "readout_built.mph",
    param_name = "l_slider_single",
    param_values = [3800, 4000, 4200, 4400, 4600],
    param_unit = "um",
    study_type = "eigenfrequency",
    n_modes = 2,
    dry_run = False)
# → CSV with freq_ghz per slider value → fit a line, invert to target fr
design_params_write("design_params.yaml",
                    "design_Q0.readout_resonator.l_slider_single", 4150.0)
```

### D3–D4 — Purcell filter

Sweep filter geometry parameters to position the notch and set filter frequency.
Same pattern: `run_geometry_param_sweep` → invert calibration curve →
`design_params_write`.

### D5 — Purcell decay rate (T₁)

```python
run_decay_rate_sweep(
    mph_path = "full_chip_built.mph",
    sweep_param = "L_jj",
    sweep_values = [8e-9, 10e-9, 12e-9, 14e-9, 16e-9],
    sweep_unit = "H",
    junction_selection = "jj_node",
    port_selection = "readout_port_node",
    shunt_capacitance_F = 85e-15,
    freq_ghz = 5.12,
    dry_run = False)
# → kappa_MHz, T1_us per LJJ → find LJJ giving T1 > 100 µs
design_params_write("design_params.yaml", "design_Q0.decay.T1_us", 142.3)
```

### D6 — Final assembly

```python
assemble_geometry(
    components=[
        {"gds_path": "qubit.gds",   "cell_name": "qubit_top",  "x_um": 0,   "y_um": 0},
        {"gds_path": "readout.gds", "cell_name": "readout_top","x_um": 600, "y_um": 0},
        {"gds_path": "filter.gds",  "cell_name": "filter_top", "x_um": 1200,"y_um": 0},
    ],
    output_path = "chip_Q0_final.gds",
    top_cell_name = "Q0_final")
design_params_write("design_params.yaml",
                    "design_Q0.final.assembled_gds", "chip_Q0_final.gds")
```

All tool calls above use the **device-agnostic** tools — replace the script
paths and parameter names to adapt this walkthrough to any qubit architecture.

---

## Physics constants (this device)

| Symbol | Value | Meaning |
|--------|-------|---------|
| Lj | 280 pH | Josephson inductance / junction (`juncL`) |
| Ls | 8.33 pH | geometric series inductance / cell |
| Leff | 288.33 pH | Lj + Ls |
| Cg_design | 115.33 fF | from the 50 Ω constraint Leff/50² |
| Z0 | 50 Ω | port / target characteristic impedance |
| pitch | 17 µm | unit-cell pitch |
| N | 21 junctions / 20 shunts | chain size |

# Pipeline data flow & formats

This documents what flows between stages, so you can see where each tool plugs in
and what the COMSOL→fitting handoff looks like.

```
 converter_group_recreation.py              [tool: generate_cad]
        │  gdstk, absolute chip coords
        ▼
 converter_group_recreation.gds  ───────────[tool: verify_cad → PASS/FAIL]
        │  ECAD import
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

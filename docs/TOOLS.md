# MCP Tools Reference

All tools return JSON-serialisable dicts. Long-running tools return a `job_id`
immediately; poll with `get_job_status` / `get_job_result`.

---

## CAD stage

### `generate_cad(cad_script=None, out_gds_var="OUT_GDS", out_png_var="OUT_PNG", output_dir=None, gds_filename=None, debug=False)`
Generate a chip GDS from **any** GDS generation script. Device-agnostic — works
for JTWPA, transmon qubit, resonator, waveguide, or any gdstk-based layout.

- **`cad_script`:** absolute path to a Python GDS generation script.
  `None` → uses the project default (config key `cad_generator`; currently the JTWPA script).
- **`out_gds_var`:** variable name the script uses for the GDS output path (default `OUT_GDS`).
- **`out_png_var`:** variable name for the PNG preview (`None` to skip preview).
- **`gds_filename`:** override the output `.gds` filename inside `output_dir`.
- **Returns:** `{ok, gds_path, preview_png, duration_s, log_path, log_tail}`
- **Runs:** synchronously (~5 s).

```text
> generate_cad()        # JTWPA default (backward-compatible)
> generate_cad(cad_script="/path/to/transmon_cad.py", out_gds_var="OUT_GDS")
{ "ok": true, "gds_path": ".../runs/cad-.../transmon.gds", ... }
```

### `verify_cad(gds_path=None, checker_script=None, gds_var="RECR", debug=False)`
Verify a GDS against **any** geometry checker script. Device-agnostic — supply
a checker for any device, or use `None` for the project default (JTWPA).

- **`checker_script`:** absolute path to any Python checker that defines
  `main() -> int` (0 = pass) and a module-level string constant.
  `None` → uses the project default JTWPA checker (backward-compatible).
- **`gds_var`:** name of the GDS path constant in the checker script
  (default `"RECR"` — the JTWPA checker's constant). Override for other devices.
- **`gds_path`:** override the GDS to validate. Defaults to the project reference GDS.
- **Returns:** `{passed, gds_path, n_failures, report, checker_script}`

Copy `scripts/checker_template.py` as a starting point for any device checker.

```text
> verify_cad()                                           # JTWPA default
> verify_cad(checker_script="/path/to/my_checker.py",
             gds_var="GDS_PATH",
             gds_path="/path/to/my_device.gds")
{ "passed": true, "n_failures": 0, "report": "ALL PASS" }
```

### `assemble_geometry(components, output_path, top_cell_name="assembly", merge_refs=True)`
Assemble multiple GDS components into one top-level layout. Use for 4-qubit
unit cells, resonator arrays, multi-chip assemblies, or any combination of
sub-GDS parts.

- **`components`:** list of placement dicts:
  `{gds_path, cell_name, x_um=0, y_um=0, rotation_deg=0, magnification=1, x_reflection=False}`
- **`output_path`:** where to write the assembled GDS.
- **`merge_refs`:** `True` = gdstk References (smaller file); `False` = flatten.
- **Returns:** `{ok, output_path, n_components, bbox, error}`

```text
> assemble_geometry(
    components=[
      {"gds_path": "qubit.gds", "cell_name": "qubit_top", "x_um": 0,   "y_um": 0},
      {"gds_path": "res.gds",   "cell_name": "resonator",  "x_um": 500, "y_um": 0, "rotation_deg": 90},
    ],
    output_path="chip_assembly.gds")
{ "ok": true, "n_components": 2, "bbox": [[0,0],[1200,800]] }
```

### `run_custom_cad(cad_script, output_dir=None, out_gds_var="OUT_GDS", out_png_var="OUT_PNG", gds_filename="output.gds", debug=False)`
Run any user-supplied GDS generation script with automatic path redirection.
Use this for custom chip geometries (different junction count, CPW params, etc.)
without modifying this package. Set `out_gds_var` / `out_png_var` to the variable
names used in the script (defaults match `converter_group_recreation.py`).

- **Returns:** `{ok, gds_path, preview_png, duration_s, log_path, log_tail, error}`
- **Runs:** synchronously (~5 s per script).

```text
> run_custom_cad("/path/to/my_device.py", out_gds_var="OUT_GDS", gds_filename="my_device.gds")
{ "ok": true, "gds_path": ".../my_device.gds", "preview_png": ".../my_device.png" }
```

---

## COMSOL stage  *(default `dry_run=True` — real solves need a COMSOL connection)*

### `comsol_health_check(comsol_host=None, comsol_port=None)`
Probe COMSOL reachability without solving: checks `mph` import and (if a host is
set) a TCP connect.
- **Returns:** `{ok, mph_available, host, port, host_reachable, detail}`

---

### `build_comsol_model(gds_path, ...)` ⚠️ *Deprecated — JTWPA-specific*

> **Deprecated.** This tool is hardcoded to the JTWPA geometry. For any other device
> (transmon, resonator, fluxonium, etc.) use `run_custom_comsol_build` instead.
> This tool continues to function for existing JTWPA workflows.

Build the **JTWPA** EM model from a GDS (build → geometry validation → coarse solve).
For any other device, use `run_custom_comsol_build`.

**Geometry and material adjustments:**

| Parameter | What it does | Example |
|-----------|-------------|---------|
| `geom_params` | COMSOL parameter table overrides — applied via `m.param().set()` | `{"add_stub_length": "350[um]", "metal_t": "200[nm]"}` |
| `material_params` | Material property overrides | `{"sub_eps_r": "11.7", "sub_loss_tan": "1e-6"}` |
| `comsol_cores` | Solver thread count | `8` |
| `build_only` | Save `model_built.mph` and stop — inspect before a long solve | `True` |

**MPH output** (returned in `result["mph_paths"]` when job completes):
- `model_built.mph` — geometry + mesh. Open in COMSOL GUI to inspect before solving.
- `model_solved.mph` — solved S-parameter data. Open to verify field distributions.

- **dry-run:** `{dry_run, would_run, patches_applied, mph_files_would_save, comsol_health, ready}`
- **real:** `{job_id, status}` — poll `get_job_result` for `{mph_paths, output_files}`

```text
> build_comsol_model("runs/cad-.../device.gds",
    geom_params={"add_stub_length": "350[um]"},
    material_params={"sub_eps_r": "11.9"},
    comsol_cores=8, dry_run=True)
{ "dry_run": true, "ready": false,
  "patches_applied": {"^ROOT\\s*=.*$": "ROOT = r\"...\"",
                      "GEOM_PARAM_OVERRIDES (injected)": "{'add_stub_length': '350[um]'}", ...},
  "mph_files_would_save": ["runs/comsol_build/model_built.mph",
                            "runs/comsol_build/model_solved.mph"] }
```

---

### `run_custom_comsol_build(build_script, output_dir=None, out_dir_var="OUT_DIR", geom_params=None, material_params=None, param_overrides_var="PARAM_OVERRIDES", material_overrides_var="MATERIAL_OVERRIDES", comsol_host=None, comsol_cores=4, dry_run=True, debug=False)`

**Device-agnostic** COMSOL build — run *any* user-supplied script for any chip
(transmon qubit, resonator grid, fluxonium, quantum memory, etc.).

The script follows a lightweight convention (three patchable variables):

```python
# my_transmon_build.py
OUT_DIR            = "/default/output"   # → redirected by MCP tool
PARAM_OVERRIDES    = {}                  # → your geom_params dict
MATERIAL_OVERRIDES = {}                  # → your material_params dict

# script reads and applies them:
for name, val in PARAM_OVERRIDES.items():
    m.param().set(name, val)
pymodel.save(os.path.join(OUT_DIR, "model_built.mph"))
```

```text
> run_custom_comsol_build(
    build_script="/path/to/transmon_build.py",
    geom_params={"pad_width": "200[um]", "res_length": "5000[um]", "sub_t": "525[um]"},
    material_params={"sub_eps_r": "11.7", "sub_loss_tan": "1e-6", "metal_sigma": "5.88e7"},
    comsol_cores=8, dry_run=True)
{ "dry_run": true, "ready": false,
  "patches_applied": {"OUT_DIR": "r\"runs/comsol_custom_transmon_build\"",
                      "PARAM_OVERRIDES": "{'pad_width': '200[um]', ...}",
                      "MATERIAL_OVERRIDES": "{'sub_eps_r': '11.7', ...}"},
  "mph_files_would_save": ["runs/.../model_built.mph", "runs/.../model_solved.mph"] }
```

**MPH output** — same as `build_comsol_model`: paths appear in `result["mph_paths"]`
so you can open them in the COMSOL GUI to verify geometry, mesh, and solutions.

---

### `validate_geometry(mph_path, reference_vertices_csv=None, comsol_host=None, dry_run=True)`
Validate a built model (face counts + full vertex multiset) — the mandatory gate
before trusting a solve. Pass the `mph_path` from `build_comsol_model` result.

### `run_eigenfrequency_study(mph_path, n_modes=5, freq_start_ghz=1.0, freq_stop_ghz=20.0, extract_fields=False, path_selections=None, node_groups=None, comsol_host=None, output_dir=None, comsol_cores=4, dry_run=True, debug=False)`
Find resonance frequencies and Q-factors via the COMSOL eigenvalue solver (~5 min).
Run this **first** for any new device to locate resonances before a full sweep.

- **`extract_fields`:** also extract `emw.intWe`, `emw.intWm`, and |E| path integrals
  per mode. Feed the output CSV into `run_coupling_extraction` for g, χ, participation ratio.
- **`path_selections`:** list of COMSOL named edge selections for |E| integrals, e.g.
  `["resonator_path", "qubit_path"]`. Supply names defined in your COMSOL model.
- **`node_groups`:** list of COMSOL node selections for complex voltage extraction.
- **Returns (real-run):** `{job_id, status}` — poll `get_job_result` for `{eigenfrequencies_csv}`.

### `run_stub_length_sweep(mph_path, stub_lengths_um, freq_ghz, ...)` ⚠️ *Deprecated*

> **Deprecated.** Use `run_geometry_param_sweep(param_name="stub_length",
> study_type="frequency_domain", ...)` for equivalent behavior with any COMSOL
> geometry parameter. This tool continues to function for existing JTWPA workflows.

### `run_geometry_param_sweep(mph_path, param_name, param_values, param_unit="um", study_type="eigenfrequency", n_modes=5, freq_start_ghz=1.0, freq_stop_ghz=20.0, extract_fields=False, path_selections=None, node_groups=None, freq_points_ghz=None, port="both", resume=False, comsol_host=None, output_dir=None, comsol_cores=4, dry_run=True, debug=False)`
**Device-agnostic** parametric sweep over **any** COMSOL geometry parameter. Works for
stub length (TWPA), slider length (resonator), coupler angle (transmon), gap width
(waveguide), junction radius, or any named COMSOL parameter.

| Parameter | What it does |
|-----------|-------------|
| `param_name` | Any COMSOL parameter name — no hardcoding |
| `param_unit` | COMSOL unit string: `"um"`, `"deg"`, `"H"`, etc. |
| `study_type` | `"eigenfrequency"` or `"frequency_domain"` |
| `extract_fields` | Extract We/Wm/path integrals per mode (eigenfreq only) |
| `resume` | Skip values already in the output CSV (crash-safe) |

- **dry-run:** `{dry_run, would_run, patches_applied, comsol_health}`
- **completed result:** `{job_id}` — one `.mph` per sweep value + output CSV

```text
> run_geometry_param_sweep(
    mph_path="model_built.mph",
    param_name="l_slider_single",
    param_values=[4000, 4200, 4400, 4600],
    param_unit="um",
    study_type="eigenfrequency",
    n_modes=3,
    dry_run=True)
```

### `run_decay_rate_sweep(mph_path, sweep_param, sweep_values, sweep_unit, junction_selection, port_selection, shunt_capacitance_F, freq_ghz=None, Z0_Ohm=50.0, resume=False, comsol_host=None, output_dir=None, comsol_cores=4, dry_run=True, debug=False)`
Sweep a lumped-element parameter and extract decay rate κ and T₁ at each point.
Works for qubit Purcell decay (sweep L_JJ), resonator external Q (sweep coupling gap),
or any admittance-dominated decay channel.

- Computes `κ = |V_port/V_junction|² / (Z0·C)` [rad/s] and `T1 = 1/κ` [s].
- **`junction_selection` / `port_selection`:** COMSOL named selections for voltage nodes.
- **Returns:** one CSV row per sweep value: `{sweep_param, freq_ghz, V_junc_mag, V_port_mag, kappa_rad_s, kappa_MHz, T1_us, T1_ns}`.

### `run_coupling_extraction(eigenfreq_csv, mode1_path_col, mode2_path_col, lumped_inductance_H, mode1_label="mode1", mode2_label="mode2")`
Pure-Python post-processing — no COMSOL connection needed. Reads a CSV from
`run_eigenfrequency_study` with `extract_fields=True` and applies
Jaynes-Cummings energy-partition analysis.

- **`mode1_path_col` / `mode2_path_col`:** CSV column names for |E| path integrals.
- **`lumped_inductance_H`:** inductance of the lumped element (JJ, coupling inductor).
- **Returns:** `{g_Hz, g_MHz, chi_Hz, chi_MHz, anharmonicity_Hz, f_mode1_Hz, f_mode2_Hz, mode_labels, participation_ratio, error}`

### `export_touchstone(csv_path, output_path=None, dry_run=True, debug=False)`
Convert an extracted S-parameter CSV to a Touchstone `.s2p` file. Safe to run
offline with `dry_run=False`.

---

## Superconducting circuit physics

### `compute_circuit_params(**kwargs)`
Compute superconducting circuit parameters from any input combination.
Device-agnostic: works for transmon, fluxonium, charge qubit, or any
lumped-element SC circuit.

Pass any subset of these keyword arguments:

| Key | Meaning | Unit |
|-----|---------|------|
| `L_H` | Josephson/coupling inductance | H |
| `f0_Hz` | Bare oscillator frequency | Hz |
| `EJ_Hz` / `EC_Hz` | Josephson / charging energy | Hz |
| `C_F` | Capacitance | F |
| `Ic_A` | Critical current | A |
| `g_Hz` | Mode–mode coupling rate | Hz |
| `fq_Hz` / `fr_Hz` | Qubit / resonator frequency | Hz |
| `anh_Hz` | Anharmonicity | Hz |
| `V_junction` / `V_port` | Voltage phasors for κ | V |
| `Z0_Ohm` | Port impedance (default 50) | Ω |

Returns all derivable quantities with `_Hz`, `_fF`, `_nH` suffixes.

---

## Design parameter management + session planning

### `design_params_read(yaml_path, key_path)`
Read a value from `design_params.yaml` by dot-separated key path.
`key_path` example: `"design_Q0.readout_resonator.l_slider_single"`.
Creates the file if it does not exist.

### `design_params_write(yaml_path, key_path, value)`
Atomically write a value by dot-separated key path (creates parent keys if missing).
Uses `os.replace` for atomic writes — safe for concurrent MCP calls.

### `get_pipeline_session_plan(yaml_path, stage_map)`
Determine which pipeline stages are complete and what comes next. Call this
at the **start of every session** before launching any COMSOL job.

- **`stage_map`:** maps stage name → list of YAML key paths that must be non-null
  for the stage to count as complete.
- **Returns:** `{completed_stages, next_stage, missing_params, all_values, session_scope}`

```text
> get_pipeline_session_plan(
    yaml_path = "design_params.yaml",
    stage_map = {
      "D0": ["design_Q0.qubit.d_q"],
      "D1": ["design_Q0.qr_coupler.delta_angle_coupler"],
      "D2": ["design_Q0.readout_resonator.l_slider_single"],
    })
{ "next_stage": "D1", "completed_stages": ["D0"],
  "missing_params": ["design_Q0.qr_coupler.delta_angle_coupler", ...] }
```

---

## Fitting stage

### `run_abcd_fit(data_path=None, output_dir=None, stub_filter_um=None, debug=False)`
Python ABCD/Bloch fit: 3 topologies × 5 objectives per stub. Writes
`abcd_fit_results.csv` (fitted Cg, implied Z0) + per-stub comparison CSVs and
figures.
- **Returns:** `{job_id, status}`  (poll for completion)
- **Default data:** bundled `bridge/003/stub_length_sweep.dat`.
- **`stub_filter_um`:** restrict to a single stub length (µm) — useful for
  quick re-fits or manual parallelisation.

```text
> run_abcd_fit()                       -> { "job_id": "abcd_fit-22563f68", ... }
> get_job_result("abcd_fit-22563f68")  -> { "status": "completed",
      "result": { "summary": "7 CSV(s) written", "output_files": [ ... ] } }
```

Canonical objective (topoA/fitA) over bridge/003 yields Z0 ≈ 44–51 Ω across the
300–400 µm stubs (near the 50 Ω target).

### `run_abcd_fit_parallel(data_path=None, output_dir=None, max_workers=None, debug=False)`
**Parallel** ABCD fit: one subprocess per stub, run concurrently via
`ThreadPoolExecutor`. Each stub lands in `stub_<N>um/` subdirectory. After all
stubs finish, individual `abcd_fit_results.csv` files are merged into
`abcd_fit_results_merged.csv`. Typically **4-6× faster** than `run_abcd_fit` on
multi-core machines.

- **Returns:** `{job_id, status, n_stubs}` immediately. Poll `get_job_result` for
  `{stubs_ok, merged_csv, output_files, figures}`.
- **`max_workers`:** cap on concurrent processes (defaults to `len(stubs)`).

```text
> run_abcd_fit_parallel()              -> { "job_id": "abcd_fit_parallel-3a9f1b2c", "n_stubs": 6 }
> get_job_result("abcd_fit_parallel-...")
  -> { "status": "completed",
       "result": { "stubs_ok": {"300": true, "320": true, ...},
                   "merged_csv": ".../abcd_fit_results_merged.csv",
                   "summary": "Parallel fit: 6/6 stubs OK; 102 result rows merged" } }
```

### `run_generic_fit(fit_script, data_path, output_dir=None, dat_path_var="DAT_PATH", out_base_var="OUT_BASE", extra_patches=None, debug=False)`
Run **any user-supplied Python fitting script** with path redirection. Patches
`dat_path_var` and `out_base_var` to point at the user's data and output
directory, then runs as a background job. Pass `extra_patches` (a
`{regex_pattern: replacement_line}` dict) to override additional variables —
e.g. junction count, port impedance, topology constants.

- **Returns:** `{job_id, status}` — poll `get_job_result` for `{output_files, figures}`.

```text
> run_generic_fit(
    fit_script="/path/to/my_circuit_fit.py",
    data_path="/path/to/my_sweep.dat",
    extra_patches={r"^N_JCT\s*=.*$": "N_JCT = 10",
                   r"^Z0_PORT\s*=.*$": "Z0_PORT = 75.0"}
  )
```

### `fit_stub_sweep(debug=False)`
Julia single-Cg fitter (`fit_stub_sweep.jl`). Returns a `job_id`. **Requires**
Julia + the project's `JosephsonCircuits.jl` environment.

### `analyze_dispersion(debug=False)`
Julia Bloch dispersion / Δk analysis. Returns a `job_id`. Requires the
fit-results CSV from `fit_stub_sweep` first.

---

## Jobs & introspection

| Tool | Returns |
|------|---------|
| `get_job_status(job_id)` | `{status, elapsed_s, log_tail, error}` |
| `get_job_result(job_id)` | `{status, result, error}` |
| `list_jobs()`            | `[{job_id, tool, status, created_at, elapsed_s}]` |
| `describe_config()`      | resolved paths, COMSOL host, interpreters |

> Tip: call `describe_config()` first to confirm the suite located your pipeline
> scripts and data.

---

## qleap chip simulations (tile pipelines in `<repo>/simulations/`)

Wrappers for the qleap_circuit_design per-tile pipelines. The qleap repo root
is resolved as `chip_sim_root.parent` (the suite is vendored at
`<repo>/resources/COMSOL Simulation Suite`). Solver tools default to
`dry_run=True` and launch background jobs; extraction/status tools run
in the foreground (seconds).

### `qleap_notch_status(unit=None, row=None)`
Read-only progress + results of the NDS001 notch-decay pipeline per tile:
prepared/gated model presence, sweep CSVs done, metal-verification verdict,
and the per-qubit notch summary (f_q, f_notch, kappa(f_q), T1, depth).

### `qleap_run_notch_pipeline(unit, row, letters="ABCD", dry_run=True)`
Full NDS001 tile pipeline (resume-safe): copy -> inspect (coax gate) ->
prepare (selection-verification gate) -> metal raster gate -> renders ->
coarse sweeps -> fine windows -> fine sweeps -> extraction. ~5-6 h/tile.

### `qleap_run_notch_sweep(unit, row, letter, pass_name="coarse", flist=None, others="inductor", reciprocity=False, cores=8, dry_run=True)`
One JJ-port S-parameter sweep on a prepared tile. `flist` accepts a COMSOL
range/list expression for targeted scans (e.g. a notch window). ~45 s/point.

### `qleap_extract_notch(unit, row, stage="final")`
NDS001 post-processing: `window` writes fine flists; `final` writes the
notch summary + kappa(omega) figure and returns them.

### `qleap_run_eigen_gqr(unit, row, run, neigs=None, shift=None, cores=8, dry_run=True)`
RCS001 eigenfrequency solve: run 1 = qubit frequencies (JJ inductors),
run 2 = g_QR (JJ current ports).

### `qleap_extract_gqr(unit, row, stage="final")`
RCS001 post-processing: `qubit-freqs` after run 1, `final` after run 2.

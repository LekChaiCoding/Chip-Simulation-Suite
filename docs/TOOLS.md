# MCP Tools Reference

All tools return JSON-serialisable dicts. Long-running tools return a `job_id`
immediately; poll with `get_job_status` / `get_job_result`.

---

## CAD stage

### `generate_cad(output_dir=None, debug=False)`
Generate the 21-junction JTWPA chip GDS (the exact device imported into COMSOL).

- **Returns:** `{ok, gds_path, preview_png, duration_s, log_path, log_tail}`
- **Runs:** synchronously (~5 s).

```text
> generate_cad()
{ "ok": true,
  "gds_path": ".../runs/cad-1781675442/converter_group_recreation.gds",
  "preview_png": ".../converter_group_recreation.png", "duration_s": 5.7 }
```

### `verify_cad(gds_path=None, debug=False)`
Verify a GDS against the vertex-validated reference geometry pins (layer bboxes,
21 junction bars, tine edges, pads, ports, CPW centreline).

- **Returns:** `{passed, gds_path, n_failures, report}`
- **Default:** the repo's reference GDS if `gds_path` omitted.

```text
> verify_cad(".../runs/cad-.../converter_group_recreation.gds")
{ "passed": true, "n_failures": 0,
  "report": "RESULT: ALL PASS — recreation matches the built reference geometry pins" }
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

### `build_comsol_model(gds_path, junction_inductance_ph=280.0, comsol_host=None, output_dir=None, geom_params=None, material_params=None, comsol_cores=4, build_only=False, dry_run=True, debug=False)`

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

### `run_stub_length_sweep(mph_path, stub_lengths_um, freq_ghz, comsol_host=None, output_dir=None, comsol_cores=4, port="both", resume=False, dry_run=True, debug=False)`
Parametric stub-length sweep extracting complex S-parameters (S11/S21/S12/S22).
Produces the `.dat` the fitting tools consume. Each stub saved as an interim
`stub_<N>um.mph` that can be opened in the COMSOL GUI.

| Parameter | What it does |
|-----------|-------------|
| `port` | `"1"`, `"2"`, or `"both"` — `"both"` extracts the full 2×2 S-matrix |
| `resume` | Skip stubs already in the output CSV (safe crash-resume for long sweeps) |
| `comsol_cores` | Solver threads per stub |

- **dry-run:** `{dry_run, would_run, patches_applied, mph_files_would_save, comsol_health}`
- **completed result:** `{mph_paths, output_files}` — one `.mph` per stub

### `export_touchstone(csv_path, output_path=None, dry_run=True, debug=False)`
Convert an extracted S-parameter CSV to a Touchstone `.s2p` file. Safe to run
offline with `dry_run=False`.

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

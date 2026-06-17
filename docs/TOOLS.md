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

---

## COMSOL stage  *(default `dry_run=True` — real solves need a COMSOL connection)*

### `comsol_health_check(comsol_host=None, comsol_port=None)`
Probe COMSOL reachability without solving: checks `mph` import and (if a host is
set) a TCP connect.
- **Returns:** `{ok, mph_available, host, port, host_reachable, detail}`

### `build_comsol_model(gds_path, junction_inductance_ph=280.0, comsol_host=None, output_dir=None, dry_run=True, debug=False)`
Build the EM model from a GDS (build → geometry validation → coarse solve).
- **dry-run:** `{dry_run, would_run, comsol_health, ready}`
- **real:** `{job_id, status}`

### `validate_geometry(mph_path, reference_vertices_csv=None, comsol_host=None, dry_run=True)`
Validate a built model (face counts + full vertex multiset) — the mandatory gate
before trusting a solve.

### `run_stub_length_sweep(mph_path, stub_lengths_um, freq_ghz, comsol_host=None, output_dir=None, dry_run=True, debug=False)`
Solve a parametric stub-length sweep, extracting complex S-parameters. Produces
the `.dat` the fitting tools consume — the COMSOL→fitting handoff.

### `export_touchstone(csv_path, output_path=None, dry_run=True, debug=False)`
Convert an extracted S-parameter CSV to a Touchstone `.s2p` file. (Safe to run
offline with `dry_run=False`.)

---

## Fitting stage

### `run_abcd_fit(data_path=None, output_dir=None, debug=False)`
Python ABCD/Bloch fit: 3 topologies × 5 objectives per stub. Writes
`abcd_fit_results.csv` (fitted Cg, implied Z0) + per-stub comparison CSVs and
figures.
- **Returns:** `{job_id, status}`  (poll for completion)
- **Default data:** bundled `bridge/003/stub_length_sweep.dat`.

```text
> run_abcd_fit()                       -> { "job_id": "abcd_fit-22563f68", ... }
> get_job_result("abcd_fit-22563f68")  -> { "status": "completed",
      "result": { "summary": "7 CSV(s) written", "output_files": [ ... ] } }
```

Canonical objective (topoA/fitA) over bridge/003 yields Z0 ≈ 44–51 Ω across the
300–400 µm stubs (near the 50 Ω target).

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

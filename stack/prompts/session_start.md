# Pipeline Session Start

Use this prompt at the beginning of any device pipeline session to ensure a clear,
approved plan exists before any COMSOL simulation is launched.

---

## Step 1 — Read Current State

Call `get_pipeline_session_plan` with your `design_params.yaml` path and a `stage_map`
that defines which YAML keys signal completion of each stage.

Example for an AlNtransmon pipeline (adapt for other devices):

```python
get_pipeline_session_plan(
    yaml_path = "/path/to/design_params.yaml",
    stage_map = {
        "D0_capacitance":    ["design_Q0.qubit.d_q"],
        "D1_qr_coupling":    ["design_Q0.qr_coupler.delta_angle_coupler"],
        "D1_1_drive_port":   ["design_Q0.drive_spokes.n"],
        "D2_readout_freq":   ["design_Q0.readout_resonator.l_slider_single"],
        "D3_notch_position": ["design_Q0.filter.l_end"],
        "D4_filter_freq":    ["design_Q0.filter.l_slider_single"],
        "D5_unit_cell":      ["design_Q0.readout_port.spiral_turns"],
    }
)
```

This returns: completed stages, next stage, missing params, and a session scope summary.

---

## Step 2 — Enter Plan Mode

Before launching any COMSOL job, enter plan mode and document:

1. **Device scope** — which device(s) / indices are being designed this session?
2. **Stage** — which pipeline stage is being run? What is its goal?
3. **Sweep** — what is the sweep parameter name (as it appears in COMSOL), range, and unit?
4. **CAD script** — which GDS generation script will `generate_cad` use?
5. **COMSOL build script** — which build script will `run_custom_comsol_build` use?
6. **COMSOL selections** — what are the `path_selections` and `node_groups` defined in the `.mph`?
7. **Target values** — what are the target parameter values to hit?
8. **Decision criterion** — which calibration curve is inverted, and how?
9. **Write-back** — which `design_params_write` calls will record the result?

---

## Step 3 — Get Approval, Then Execute

Only after plan mode is approved:

1. Run `generate_cad` (with your device's CAD script) to produce the GDS
2. Run `verify_cad` with a device-specific checker script
3. Run `run_custom_comsol_build` to build the `.mph`
4. Run `run_eigenfrequency_study` or `run_geometry_param_sweep` to extract data
5. Run `run_coupling_extraction` or `compute_circuit_params` to post-process
6. Call `design_params_write` to record results
7. Proceed to the next stage

---

## Session Anti-Patterns (do not do these)

- **Do NOT** launch any COMSOL job before the session plan is approved
- **Do NOT** hardcode device geometry into tool calls — all geometry lives in CAD/build scripts
- **Do NOT** skip `verify_cad` — a broken GDS wastes hours of compute
- **Do NOT** forget to call `design_params_write` after each stage — results must be persisted
- **Do NOT** use `build_comsol_model` or `run_stub_length_sweep` for new work — these are
  deprecated JTWPA-specific tools; use `run_custom_comsol_build` and `run_geometry_param_sweep`

---

## Tool Quick Reference

| Goal | Tool |
|---|---|
| Check pipeline state | `get_pipeline_session_plan` |
| Read a design param | `design_params_read` |
| Write a design param | `design_params_write` |
| Generate GDS (any device) | `generate_cad(cad_script=...)` |
| Check GDS geometry | `verify_cad(checker_script=...)` |
| Assemble multi-component GDS | `assemble_geometry` |
| Build COMSOL model (any device) | `run_custom_comsol_build` |
| Find resonances quickly | `run_eigenfrequency_study` |
| Sweep any geometry parameter | `run_geometry_param_sweep` |
| Extract mode coupling g | `run_coupling_extraction` |
| Compute circuit parameters | `compute_circuit_params` |
| Sweep decay / Purcell rate | `run_decay_rate_sweep` |
| Check COMSOL connection | `comsol_health_check` |
| Export S-params to Touchstone | `export_touchstone` |
| Fit ABCD matrix | `run_abcd_fit_parallel` |
| Monitor background job | `get_job_status` / `get_job_result` |

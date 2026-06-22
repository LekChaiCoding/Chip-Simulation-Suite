# Automated Grid Search - Simulation Tuning Controller

Use this prompt after CAD is verified and materials are confirmed. The goal is
to let the AI run analysis, parameter updates, and COMSOL simulations without
asking the human to approve every iteration.

The human approves the search space once. After that, the AI iterates until the
model matches the design targets, a validation gate fails, or the run budget is
exhausted.

---

## Reference pattern

The tuning scripts in `Z:\users\ishida\backup\python_script` follow this loop:

1. Set COMSOL parameters on the loaded model.
2. Regenerate geometry and mesh.
3. Run the selected study.
4. Extract numerical results.
5. Score the result against target frequencies, linewidths, coupling, or Q.
6. Store trial parameters, result values, errors, and the best candidate.

Useful references:
- `comsol_sjr_tune_bare_filters_light.py`: simple Optuna loop over filter-end
  offsets with frequency and linewidth score terms.
- `comsol_sjr_tune_bare_filters_onebyone_roundrobin.py`: alternating optimizer
  that tunes one frequency parameter at a time, then linewidth parameters, with
  early-stop thresholds.
- `comsol_sjr_Jsearch.py`: single-parameter search that stores all trial data
  for later inspection.

The stack should expose the same behavior at the conversation level even when
the underlying MCP tools are generic: propose the parameter grid, run a trial,
read the real CSV, score it, update parameters, and repeat.

---

## Step 1 - Select tunable parameters

Choose tunable parameters from the filled `design_params.yaml`. Prefer a small
search space with physically meaningful knobs.

Common choices:

| Device | Primary target | Tunable parameters |
|--------|----------------|--------------------|
| Resonator | resonance frequency | `length_um`, coupling stub length, coupling gap |
| Coupler | coupling strength, mode placement | coupler length, gap, overlap, pad spacing |
| Transmon | qubit/readout modes | pad dimensions, junction inductance, readout length |
| TWPA | Cg, Z0, gain, bandwidth | stub length, cell pitch, junction inductance |

Ask the human to approve:
- parameter names
- min/max range
- step size or candidate count
- target tolerances
- maximum trials or wall-clock budget

After this approval, do not ask before each iteration.

---

## Step 2 - Build the search grid

Start with a coarse grid, then refine around the best candidate.

Recommended defaults:
- Single parameter: 7 to 11 candidates across the approved range.
- Two parameters: 5 x 5 coarse grid, then 5 x 5 refined around the best point.
- Three or more parameters: use coordinate/round-robin search instead of the
  full Cartesian product.

For coupled targets, use a weighted score:

```text
score =
  w_freq * rms_frequency_error_normalized
  + w_q * rms_q_error_normalized
  + w_linewidth * rms_linewidth_error_normalized
  + penalty_for_failed_gates
```

Normalize each term by the accepted tolerance, so `score <= 1` means the design
is within tolerance overall.

---

## Step 3 - Run each trial

For every candidate:

1. Create an iteration directory:
   `stack/sessions/<device>_<YYYYMMDD>/iter_NNN_grid_<short_params>/`
2. Update `design_params.yaml` or pass `geom_params` with the candidate values.
3. Regenerate and verify CAD when the candidate changes GDS geometry.
4. Run the material-confirmed COMSOL build with `dry_run=True` first.
5. If the dry-run is ready, launch the real build/study with `dry_run=False`.
6. Poll `get_job_status` until completion.
7. Read the actual result CSV before scoring.

Never report a trial result from logs alone. The score must come from the CSV or
structured job result.

---

## Step 4 - Stop conditions

Stop automatically when any condition is met:

- Best score is within the approved tolerance.
- Every target is within its individual tolerance.
- The maximum trial count or wall-clock budget is reached.
- The same validation gate fails twice for adjacent candidates.
- COMSOL connectivity fails or a required output file is missing.

If a gate fails, record the failed candidate and reason in `session.yaml`, then
continue only when the next candidate is still meaningful.

---

## Step 5 - Session log

Append every trial to `session.yaml`:

```yaml
grid_search:
  approved_by_user: true
  search_space:
    length_um: {min: 9000, max: 9800, step: 100}
  targets:
    freq_ghz: 5.5
    q_internal: 50000
  tolerances:
    freq_pct: 1.0
    q_pct: 5.0
  trials:
    - iter: 1
      params: {length_um: 9500}
      result: {mode1_freq_ghz: 5.31, mode1_Q: 48200}
      errors: {freq_pct: -3.45, q_pct: -3.6}
      score: 3.47
      status: completed
  best:
    iter: 7
    params: {length_um: 9180}
    score: 0.42
    status: accepted
```

---

## Step 6 - Final report

When the search stops, report:

- best parameter set
- final target comparison
- final `.mph`, `.csv`, and plot paths
- whether the model converged within tolerance
- what stopped the search

If the best candidate is accepted, the final `design_params.yaml` becomes the
source of truth for fabrication or the next simulation stage.

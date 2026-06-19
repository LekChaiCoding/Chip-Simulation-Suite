# Result Interpretation — Fill-in Template from Actual CSV Data

After COMSOL finishes (job status = "done"), use this template to read the
results and suggest design adjustments. Always fill from the ACTUAL CSV —
never report values without reading the file first.

---

## Step 1 — Check job status

```
get_job_result(job_id="<id from run_* call>")
```

If status is "running": wait and check again.
If status is "failed": read the log_tail for [GATE-N FAIL] messages.
If status is "done": proceed to Step 2.

---

## Step 2 — Read the results CSV

### Eigenfrequency results (`eigenfrequencies.csv`):

| Column         | Meaning                            |
|----------------|------------------------------------|
| mode           | Mode index (1-based)               |
| freq_ghz       | Resonance frequency [GHz]          |
| Q_factor       | Quality factor                     |
| loss_rate_mhz  | Energy loss rate [MHz] = fr / Q    |

Report to the user:
> "Mode 1: f = [freq_ghz] GHz, Q = [Q_factor], loss = [loss_rate_mhz] MHz"
> (repeat for each mode)

### S-parameter results (`sparams.csv` or `stub_length_sweep.dat`):

Read the CSV and identify:
- S21 minimum (transmission dip) → resonance frequency
- S21 linewidth (3-dB BW) → Q_loaded = f_res / BW
- S11 minimum → confirmation of resonance

---

## Step 3 — Compare vs. target

Compute the fractional error for each target parameter:

```
freq_error_pct = (result_freq - target_freq) / target_freq * 100
Q_error_pct    = (result_Q - target_Q) / target_Q * 100
```

Fill this template:

```
Result summary (iter_NNN):
  freq: [result] GHz  (target: [target] GHz, error: [±pct]%)
  Q:    [result]      (target: [target],      error: [±pct]%)
  loss: [result] MHz
  Status: [ON TARGET / NEEDS ADJUSTMENT]
```

---

## Step 4 — Suggest adjustment

Use these physics-informed rules for resonator tuning:

| Problem               | Adjustment                                          |
|-----------------------|-----------------------------------------------------|
| freq too LOW          | Shorten resonator length by ~|freq_error_pct|%     |
| freq too HIGH         | Lengthen resonator by ~|freq_error_pct|%           |
| Q_coupling too HIGH   | Decrease coupling gap or coupling capacitor length  |
| Q_coupling too LOW    | Increase coupling gap or coupling capacitor length  |
| Q_internal too LOW    | Check substrate loss tangent; verify PEC boundaries |
| Multiple spurious modes | Narrow eigenfrequency search window             |

For transmon:
- freq too LOW → increase junction inductance (smaller junction area)
- freq too HIGH → decrease junction inductance (larger junction area)

State the adjustment as a specific change to geometry parameters:
> "I suggest decreasing length_um from [old] to [new] (−[pct]%) to raise the
>  resonance from [result] to [target] GHz."

---

## Step 5 — Log to session.yaml

Update `stack/sessions/<device>_<YYYYMMDD>/session.yaml` with:

```yaml
iterations:
  - iter: N
    dir: iter_NNN_<description>
    timestamp: "<ISO datetime>"
    design_params_delta: "<what changed from previous iteration>"
    result:
      mode1_freq_ghz: <value>
      mode1_Q: <value>
    vs_target:
      freq_pct: <signed float>
      Q_pct: <signed float>
    ai_suggestion: "<specific geometry change suggestion>"
    action: ADJUST   # ADJUST | ACCEPT | ABORT
```

Once `action: ACCEPT` (both freq and Q within ±2% of target), report:
> "Design converged. Final parameters saved to design_params.yaml."

---
name: comsol-eigenfrequency
description: Run a COMSOL eigenfrequency study to find resonance frequencies and Q-factors without a frequency sweep. Use this FIRST for any new device (~5 min) before committing to a long frequency sweep.
---

# Eigenfrequency study: find resonances and Q-factors via COMSOL

Use this skill when you have a built `.mph` model and need to quickly locate
resonance frequencies and extract Q-factors — without running a full frequency
sweep. A typical eigenfrequency solve takes ~5 min vs. ~30 min for a sweep.

See `comsol-frequency-sweep` for the follow-up full S-parameter sweep.
See `comsol-python-setup` for client startup and JPype pitfalls.

---

## 1. Call the MCP tool (dry-run first)

```python
# Always dry-run first to confirm the plan.
run_eigenfrequency_study(
    mph_path="/path/to/model_built.mph",
    n_modes=5,
    freq_start_ghz=1.0,
    freq_stop_ghz=20.0,
    comsol_cores=4,
    dry_run=True,   # default
)
```

Inspect the returned `would_run` and `patches_applied` to confirm paths are correct,
then set `dry_run=False` to launch.

---

## 2. COMSOL study setup (inside eigenfrequency_analysis.py)

```python
m = pymodel.java

m.study().create("stdEig")
m.study("stdEig").label("stdEig")   # REQUIRED: mph.solve() resolves by LABEL

m.study("stdEig").create("eig", "Eigenfrequency")
feat = m.study("stdEig").feature("eig")
feat.set("neigsactive", "on")
feat.set("neigs", "5")
feat.set("eigunit", "GHz")          # CRITICAL: avoids rad/s unit confusion
feat.set("shift", "10[GHz]")        # search near the midpoint
```

**`eigunit = "GHz"`** is mandatory. Without it, COMSOL returns eigenvalues in
rad/s (angular frequency), and the Q-factor formula gives nonsense results.

---

## 3. Solve

```python
import time
t0 = time.time()
pymodel.solve("stdEig")             # resolves study by LABEL, not tag
log(f"eigenfrequency solve done in {time.time() - t0:.0f} s")
```

Timing reference (EMW eigenfrequency, n_modes=5, 4 cores): ~5–10 min.

---

## 4. Extract eigenvalues

COMSOL returns complex eigenvalues. With `eigunit=GHz`:
  - `real(freq)` = resonance frequency f_r  [GHz]
  - `imag(freq)` = half-linewidth f_i       [GHz]
  - Q = f_r / (2 · |f_i|)

```python
all_ds = [d.name() for d in pymodel / "datasets"]
ds = all_ds[-1]   # eigenfrequency solve creates a new dataset

eig_re = [float(v) for v in pymodel.evaluate("real(freq)", "GHz", dataset=ds)]
eig_im = [float(v) for v in pymodel.evaluate("imag(freq)", "GHz", dataset=ds)]

for i, (fr, fi) in enumerate(zip(eig_re, eig_im)):
    q = fr / (2 * abs(fi)) if abs(fi) > 1e-20 else float("inf")
    loss_mhz = abs(fi) * 2 * math.pi * 1e3
    print(f"Mode {i+1}: f={fr:.4f} GHz  Q={q:.0f}  loss={loss_mhz:.3f} MHz")
```

**Do NOT use `emw.S11dB` or `emw.S21dB` for eigenfrequency datasets** — those
expressions require a frequency-sweep solution. They will return NaN or fail.

---

## 5. Write CSV immediately

```python
with open(CSV_OUT, "w", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=["mode", "freq_ghz", "Q_factor",
                                             "loss_rate_mhz"])
    writer.writeheader()
    for i, (fr, fi) in enumerate(zip(eig_re, eig_im)):
        q = fr / (2 * abs(fi))
        writer.writerow({
            "mode": i + 1,
            "freq_ghz": round(fr, 6),
            "Q_factor": round(q, 1),
            "loss_rate_mhz": round(abs(fi) * 2 * math.pi * 1e3, 4),
        })
```

---

## 6. Validation gates

These gate messages appear in the script log and cause non-zero exit:

| Gate message                        | Cause                                     |
|-------------------------------------|-------------------------------------------|
| `[GATE-2 FAIL] BASE_MPH not found`  | .mph file doesn't exist at the given path |
| `[GATE-2 FAIL] Mode N: NaN eigenfrequency` | Solver diverged; widen window or increase shift |
| `[GATE-2 FAIL] Mode N: Q ≤ 0`      | Imaginary part has wrong sign (check eigunit) |
| `[GATE-2 FAIL] Expected N modes, got M` | Fewer physical modes than requested in window |
| `[GATE-2 FAIL] CSV is empty`        | Write failed silently |

If `Expected N modes, got M`:
1. Widen `freq_start_ghz` / `freq_stop_ghz`
2. Reduce `n_modes`
3. Check that the model has the expected EMW physics

---

## 7. Output files

| File                                   | Contents                          |
|----------------------------------------|-----------------------------------|
| `<out_dir>/eigenfrequencies.csv`       | mode, freq_ghz, Q_factor, loss_rate_mhz |
| `<out_dir>/eigenfrequency_result.mph`  | Solved model for COMSOL GUI inspection |

Both are gitignored by default (see .gitignore).

---

## Hard rules

- Always call `pymodel.solve(LABEL)`, not `.solve(TAG)`.
- Set `eigunit = "GHz"` — not "Hz", not "rad/s" (default), not "MHz".
- Never list Java iterables — SIGSEGV via JPype. Use comprehensions.
- Write CSV before saving the .mph (in case .mph save is slow/fails).
- gitignore all output files — never commit .mph or .csv data.

## Reference scripts

- `COMSOL Simulation/001/Scripts/eigenfrequency_analysis.py` — full pipeline
- `COMSOL Simulation Suite/comsol_suite/tools/comsol.py` — `run_eigenfrequency_study()`
- `COMSOL Simulation Suite/comsol_suite/server.py` — MCP tool wrapper

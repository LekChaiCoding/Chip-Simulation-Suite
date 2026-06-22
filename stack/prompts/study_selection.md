# Study Selection — Which COMSOL Study to Run

Use this guide when the user asks what study to run, or when filling
`studies_to_run` in the design template.

---

## Decision tree

```
New device (no prior simulation)?
  └─► Eigenfrequency first (~5 min, no sweep needed)
      Then: automated grid search until targets pass
      Then: frequency sweep around identified peak (~30 min)

Optimizing a known resonance?
  └─► Frequency sweep only (narrow window around target)

TWPA / stub-length parametric?
  └─► Stub-length sweep (existing JTWPA pipeline)
      Optional: eigenfrequency pre-check to confirm initial resonance location

Dispersive coupling / chi extraction?
  └─► Eigenfrequency (multi-mode) → identifies qubit + readout simultaneously
```

---

## Study options

### 1. Eigenfrequency (`"eigenfrequency"`)

**MCP tool:** `run_eigenfrequency_study`
**Script:** `COMSOL Simulation/001/Scripts/eigenfrequency_analysis.py`
**Time:** ~5 min
**Output:** `eigenfrequencies.csv` — mode, freq_ghz, Q_factor, loss_rate_mhz

**When to use:**
- First simulation of any new device — fastest way to locate resonances.
- Multi-mode extraction (qubit + readout simultaneously).
- Screening geometry sweeps before committing to long frequency sweeps.

**Parameters to set:**
- `n_modes`: number of modes to find (default 5; use 2–3 for a single resonator)
- `freq_start_ghz`, `freq_stop_ghz`: search window (set wide first, ~1–20 GHz)

**Output interpretation:** see `result_interpretation.md`

---

### 2. Frequency sweep (`"frequency_sweep"`)

**MCP tool:** `run_stub_length_sweep` (for JTWPA) or `run_custom_comsol_build`
  with a sweep study in the build script (for other devices)
**Time:** ~30 min (5-point) to ~2 h (dense sweep)
**Output:** `sparams.csv` — freq_Hz, S11_dB, S21_dB (and S12, S22 for 2-port)

**When to use:**
- After eigenfrequency to measure Q from the S21 lineshape.
- When you need the full S-parameter picture (reflection, transmission).

**Parameters to set:**
- Start near eigenfrequency − 500 MHz
- Stop near eigenfrequency + 500 MHz
- 101 points is typically sufficient for Q extraction

---

### 3. Stub-length sweep (`"stub_length_sweep"`)

**MCP tool:** `run_stub_length_sweep`
**Time:** ~1–3 h (full sweep over multiple stubs)
**Output:** `stub_length_sweep.dat` — consumed by ABCD fit

**When to use:**
- TWPA design only — extracts the coupling capacitance Cg per stub length.
- Feeds into Python ABCD fit (`run_abcd_fit` or `run_abcd_fit_parallel`).

---

## Fitting options (post-COMSOL)

### Python ABCD fit (default)

**MCP tool:** `run_abcd_fit` or `run_abcd_fit_parallel`
**Input:** `stub_length_sweep.dat`
**Time:** ~2–10 min per stub; parallelized version runs all stubs concurrently
**Output:** `abcd_fit_results.csv` — topology, stub_length_um, Cg_fF, Z0_ohm

This is the default. No Julia installation required.

### Julia fit (optional)

**MCP tool:** `fit_stub_sweep`
**Input:** `stub_length_sweep.dat`
**Requires:** Julia environment + JosephsonCircuits.jl installed on the machine
**When to prefer:** If the lab has Julia configured AND you want the
  Josephson-circuit Hamiltonian parameters (not just Cg).

To check if Julia is available: call `describe_config()` and look at `julia_bin`.
If it points to a real binary (not just "julia"), the environment is likely set up.

---

## Automated tuning mode

After CAD verification and material confirmation, prefer automated tuning over
manual one-off adjustments. Use `automated_grid_search.md` to select approved
parameter ranges, run the chosen study repeatedly, score each result from the
actual CSV output, and stop when the model matches the design targets.

Recommended study inside the loop:
- Resonator/coupler/transmon: eigenfrequency for fast frequency/mode screening.
- TWPA: stub-length sweep plus ABCD fit when Cg/Z0/gain are the target metrics.
- Final accepted design: optional dense frequency sweep for Q extraction.

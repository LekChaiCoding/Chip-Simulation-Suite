# AI Simulation Stack — 6-Step Golden Path

Walk through this guide with a lab mate to run their first simulation.
Every step is a conversation with Claude — no manual YAML editing required.

---

## Step 1 — Tell Claude what you're building

Just describe your device in plain English:

> "I want to simulate a half-wave CPW resonator on silicon, targeting 5.5 GHz
>  with an internal Q of 50,000."

Claude will:
1. Load `stack/device_templates/resonator.yaml`
2. Ask a few clarifying questions (gap width, line width, metal choice)
3. Show you the material properties for your chosen substrate + metal
4. Ask for confirmation before using those values

At the end of this step, a populated `design_params.yaml` is saved to
`stack/sessions/resonator_<date>/`.

---

## Step 2 — Generate the CAD

After confirming the design parameters, Claude generates a parameterized GDS
using gdstk and verifies it:

> "Generating CPW resonator GDS: length=9512 µm, width=10 µm, gap=6 µm..."
> "GDS verification passed: all expected layers found."

The GDS goes to `tmp_cad_data/` (gitignored).

If verification fails, Claude adjusts and retries automatically.

---

## Step 3 — Build the COMSOL model

Claude calls `build_comsol_model` (for JTWPA) or `run_custom_comsol_build`
(for any other device). Always dry-run first:

> "Dry run: would patch BASE_MPH → dummy.mph, OUT_DIR → runs/comsol_build.
>  COMSOL health: mph import OK.
>  Ready to launch? (yes / no)"

Say yes, and the build runs as a background job (~10–30 min).
Claude polls `get_job_status` and tells you when it's done.

---

## Step 4 — Run the eigenfrequency study

Once the model is built, run eigenfrequency analysis to locate your resonances:

> "Running eigenfrequency study: 5 modes in [1, 20] GHz..."

After ~5 min:
> "Mode 1: f = 5.32 GHz, Q = 48,200, loss = 0.069 MHz
>  Mode 2: f = 10.64 GHz, Q = 52,100 (spurious harmonic)"

Claude plots the results with `scripts/analysis/plot_results.py` and saves a
PNG to `stack/sessions/resonator_<date>/iter_001_initial/eigenfreqs.png`.

---

## Step 5 — Interpret results and iterate

Claude compares the result against your target:

> "Resonance at 5.32 GHz, target 5.50 GHz — 3.3% below target.
>  Suggestion: shorten the resonator length by 3.3%
>  (from 9512 µm to 9197 µm)."

Say "yes, adjust" and Claude:
1. Updates `design_params.yaml` with the new length
2. Regenerates the GDS
3. Rebuilds the COMSOL model
4. Runs the eigenfrequency study again

After 2–3 iterations you'll typically converge to within ±1% of target.
All iterations are logged in `session.yaml`.

---

## Step 6 — Frequency sweep for Q extraction (optional)

Once the resonance frequency is locked in, run a narrow frequency sweep to
extract Q from the S21 transmission lineshape:

> "Running frequency sweep: 5.0–6.0 GHz, 201 points..."

After ~30 min:
> "S21 lineshape: f_res = 5.498 GHz, Q_loaded = 49,800, Q_coupling = 980.
>  Both within 0.5% of target. Design converged."

---

## That's it

The final `design_params.yaml` is your verified design.
Hand it to the fab team or use it as the starting point for a coupled-system
simulation (resonator + qubit).

---

## Common questions

**"Do I need to know YAML?"**
No. Claude writes and updates the YAML for you based on your natural-language
answers. You can look at it if you're curious, but you never need to edit it.

**"Do I need Julia installed?"**
No. The default ABCD fitter is pure Python. Julia is optional for Josephson
circuit Hamiltonian extraction (TWPA only).

**"What if COMSOL isn't connected?"**
All MCP tools default to `dry_run=True`. You can see the full plan — which
script will run, what paths will be patched — without COMSOL. When you're
on the COMSOL network, set `dry_run=False` to launch.

**"How do I know if the simulation is physically reasonable?"**
Claude checks for [GATE-N FAIL] messages at every layer boundary. If any gate
fires (NaN eigenvalue, empty CSV, geometry mismatch), the pipeline stops and
Claude explains what went wrong before suggesting a fix.

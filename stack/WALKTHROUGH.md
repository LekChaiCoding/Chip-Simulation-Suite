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
2. Ask a few clarifying questions (gap width, line width, starting length)
3. Record any material preference as a draft value, without final confirmation

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

## Step 3 — Confirm materials

After the CAD is created and verified, Claude runs the material-selection
conversation:

> "The GDS verification passed. Before I build the COMSOL model, confirm the
>  materials: substrate = Si, metal = Al, first-pass loss tangent = 0."

Claude shows the material table, asks for confirmation or corrections, then
writes the confirmed `sub_eps_r`, `sub_loss_tan`, and metal model into
`design_params.yaml` and `session.yaml`.

---

## Step 4 — Build the COMSOL model

Claude calls `build_comsol_model` (for JTWPA) or `run_custom_comsol_build`
(for any other device). Always dry-run first:

> "Dry run: would patch BASE_MPH → dummy.mph, OUT_DIR → runs/comsol_build.
>  COMSOL health: mph import OK.
>  Ready to launch? (yes / no)"

Say yes, and the build runs as a background job (~10–30 min).
Claude polls `get_job_status` and tells you when it's done.

---

## Step 5 — Automated grid-search tuning

Once materials are confirmed, Claude asks for one approval of the automated
tuning plan:

> "I'll tune `length_um` from 9000 to 9800 um in 100 um steps, then refine
>  around the best result. Target: 5.5 GHz within 1%, Q within 5%.
>  Maximum budget: 20 COMSOL trials. Approve this search space?"

After approval, Claude runs the loop automatically:

1. Generate the candidate parameter set.
2. Regenerate and verify CAD if geometry changed.
3. Build or update the COMSOL model with the confirmed materials.
4. Run eigenfrequency or the selected study.
5. Read the actual CSV output.
6. Score frequency, Q, linewidth, coupling, or fitted parameters against target.
7. Keep the best candidate and continue until tolerance or budget is reached.

The loop follows the same pattern as the reference tuning scripts in
`Z:\users\ishida\backup\python_script`: set parameters, regenerate
geometry/mesh, run the study, extract numerical results, score the trial, and
store the best candidate.

---

## Step 6 — Final sweep for Q extraction (optional)

Once the automated tuning loop finds an accepted model, run a narrow frequency
sweep to extract Q from the S21 transmission lineshape:

> "Running frequency sweep: 5.0–6.0 GHz, 201 points..."

After ~30 min:
> "S21 lineshape: f_res = 5.498 GHz, Q_loaded = 49,800, Q_coupling = 980.
>  Both within 0.5% of target. Design converged."

All grid-search trials and the accepted final model are logged in `session.yaml`.

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

# Material Selection — Properties and Confirmation

Show this table to the user when they select substrate and metal.
Get explicit confirmation before the first COMSOL build for each session.

This conversation happens after CAD generation and GDS verification, not during
initial design intake. At this point the geometry is fixed enough to choose the
COMSOL material model deliberately before the automated simulation loop starts.

---

## Substrate properties

| Material  | εr    | tan δ (loss tangent)   | `sub_eps_r` param | Notes                          |
|-----------|-------|------------------------|-------------------|---------------------------------|
| Si        | 11.7  | ~1×10⁻⁴ to 1×10⁻⁵    | `"11.7"`          | Most common; easy to source    |
| AlN       | 8.9   | ~1×10⁻³ to 1×10⁻⁴    | `"8.9"`           | Piezoelectric; used in TWPAs   |
| Sapphire  | 9.39  | <1×10⁻⁷               | `"9.39"`          | Ultra-low loss; anisotropic    |

Source: COMSOL material library + `comsol_suite/tools/comsol.py:556-562`

---

## Metal properties

| Material  | σ (S/m)   | Tc (K)  | `metal_sigma` param | Model approach |
|-----------|-----------|---------|---------------------|----------------|
| Al        | 5.88×10⁷  | 1.2     | `"5.88e7"`          | PEC (T << Tc)  |
| Nb        | 6.74×10⁶  | 9.2     | `"6.74e6"`          | PEC (T << Tc)  |
| NbTiN     | ~2.5×10⁶  | ~15     | `"2.5e6"`           | PEC (T << Tc)  |

**Modeling approach for superconductors:**
At dilution refrigerator temperatures (10–100 mK), all metals listed above are
well below Tc. They are modeled as **PEC (Perfect Electric Conductor)** in COMSOL.
The σ values above are the room-temperature conductivities, included for reference
and for any classical (warm) simulations. PEC is exact at T << Tc and gives
cleaner eigenfrequency convergence than finite-conductivity boundaries.

Loss is captured via the substrate loss tangent (`sub_loss_tan`), not the metal σ.

---

## Confirmation prompt

After displaying the table, always ask:

> "I'll use **[substrate]** (εr=[value], tan δ=[value]) and **[metal]** (PEC, Tc=[K]).
>  These will be set as:
>    sub_eps_r = "[value]"
>    sub_loss_tan = "[value]"    ← set to 0 for lossless first-pass
>    metal modeled as PEC
>
>  Confirm? (yes / change something)"

Only proceed to COMSOL build after explicit confirmation.
Only show this confirmation once per session (not once per iteration).

Write the confirmed values into `design_params.yaml` and `session.yaml`, then
pass them as `material_params` to `build_comsol_model` or
`run_custom_comsol_build`.

---

## Sub-loss guidance

- First simulation: set `sub_loss_tan = "0"` (lossless) to establish the
  resonance frequency without loss-broadening the peak.
- Second pass (Q extraction): set `sub_loss_tan` to the real value for the
  chosen substrate.
- This is automatically tracked in `session.yaml` under `design_params_delta`.

## Hand off to automated tuning

After materials are confirmed, proceed to `automated_grid_search.md`. The AI
should ask the human to approve the tunable parameter ranges, tolerances, and
trial budget once, then run analysis, parameter changes, and simulations
automatically until a candidate satisfies the design targets or a stop condition
is reached.

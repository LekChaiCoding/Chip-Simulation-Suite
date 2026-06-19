# CAD Conversation — Device-Specific GDS Generation Guide

Use this prompt after the design intake is complete (design_params.yaml is filled).
The goal is to generate or verify a GDS file before passing it to COMSOL.

---

## General rules

1. **Parameterized scripts only.** Never hard-code geometry values in a script.
   All dimensions come from the design_params.yaml or are passed as argparse flags.
2. **gdstk required.** Set `gdstk.tolerance = 0.001` at the top of every script.
3. **GDS goes to a gitignored folder.** Write to `tmp_cad_data/` or `python_outputs/`.
4. **Verify the GDS** with `cad_verify_gds.py` before handing to COMSOL.
5. **Never modify a reference GDS.** Always generate a new file.

---

## Resonator (CPW half-wave or quarter-wave)

**Script to use:** `COMSOL Simulation/001/Scripts/converter_group_recreation.py`
(or a new parameterized version — see below)

**Key geometry parameters:**
- `line_width_um`: CPW center conductor width (from design_params.yaml)
- `gap_um`: CPW gap width
- `length_um`: resonator length (compute from freq if null:
  `length = c / (2 * n_eff * freq)` for half-wave, halve for quarter-wave)
- `n_eff`: effective index ≈ `sqrt((1 + eps_r) / 2)` for CPW on substrate

**Minimum GDS structure:**
- Resonator path (center conductor + two gap polygons)
- Ground plane (surrounding metal, with holes for the CPW)
- Optional: coupling capacitor stub at the feed end

**AI instruction to give the user:**
> "I'll generate a CPW resonator GDS. Length = [computed from freq]. Does that
>  look right? (I can adjust line_width, gap, or length manually if needed.)"

---

## Transmon qubit

**Key geometry parameters:**
- `pad_width_um`, `pad_height_um`: qubit island dimensions
- `junction_area_um2`: Josephson junction cross-section (for Lj = φ₀² / Ej)
- Readout resonator: `line_width_um`, `gap_um`, `length_um`

**GDS structure:**
- Two qubit pads (cross or rectangular) separated by junction gap
- Readout resonator coupled to one pad
- Ground plane

**Note:** For transmon, use `run_custom_comsol_build` with
`geom_params = {"pad_width": "Xum", ...}` — this injects geometry into the
COMSOL build script directly without needing to touch the GDS pipeline.

---

## TWPA

**Script to use:** `converter_group_recreation.py` (JTWPA-specific)

**TWPA GDS is unit-cell based:**
- `n_cells` unit cells, each of length `cell_length_um`
- Each cell has a stub of length `stub_length_um` (swept parametrically)

The existing `build_comsol_model` MCP tool handles the JTWPA GDS → COMSOL pipeline.
Pass `junction_inductance_ph` and `geom_params` to it directly.

---

## Verification step

After generating the GDS, always call:
```
run_cad_verify(gds_path="<path>")
```
This runs `cad_verify_gds.py` which checks:
- All expected layers exist
- No degenerate polygons
- Bounding box is within expected bounds

If verification fails, adjust the generation script parameters and retry.
Do NOT pass a failing GDS to COMSOL.

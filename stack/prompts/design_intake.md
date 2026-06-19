# Design Intake — AI Conversation Guide

Use this script when a lab mate wants to simulate a new device.
Walk through each step in order. Do not move to the next step until the current
one is complete. The goal is to populate a device template YAML with real numbers.

---

## Step 1 — Identify the device

Ask:
> "What are you building? (resonator / coupler / TWPA / transmon / something else)"

- If "something else": use `transmon.yaml` as the closest generic template and
  note what is different.
- Load the appropriate template from `stack/device_templates/`.

---

## Step 2 — Target specification

For each `null` field in the `target:` section of the chosen template, ask the
user to supply a value. Use the device-specific prompts below.

**Resonator:**
> "What resonance frequency are you targeting? (GHz)"
> "What internal Q factor? (typical: 10,000–100,000)"
> "What coupling Q? (must be less than Q_internal; typical: 500–5,000)"

**Transmon:**
> "What qubit frequency? (typical: 4–7 GHz)"
> "What anharmonicity are you expecting? (typical: −200 to −300 MHz)"
> "What T1 are you aiming for? (µs)"

**TWPA:**
> "What signal frequency? (GHz)"
> "What target gain? (dB)"
> "What bandwidth? (GHz)"

**Coupler:**
> "What bare coupler frequency? (GHz)"
> "What maximum coupling strength g? (MHz)"

If the user does not know a value, suggest a reasonable default and note it as
an estimate. Estimates are marked with `# estimate` in the YAML.

---

## Step 3 — Geometry specification

Ask about each `null` in the `geometry:` section.

**Substrate choice** (required for all devices):
> "What substrate? Common choices: Si (most common), AlN (piezoelectric, used in
>  some TWPAs), Sapphire (ultra-low loss). Which one?"

→ After the user picks, display the material properties (see `material_selection.md`)
  and ask for confirmation before continuing.

**Metal choice** (required for all devices):
> "What metal layer? Al (Tc=1.2K, easy to fab), Nb (Tc=9.2K, harder but higher Q),
>  NbTiN (Tc~15K, used in NbTiN TWPAs). Which one?"

→ Same: display properties, ask for confirmation.

**Device-specific geometry:**
- For resonators: ask line_width_um, gap_um. Offer to compute length_um from freq.
- For transmon: ask pad_width_um, pad_height_um, junction_area_um2.
- For TWPA: ask stub_length_range, n_cells, junction_inductance_ph.

---

## Step 4 — Study selection

Ask:
> "What study do you want to run? I recommend starting with eigenfrequency analysis
>  (~5 min) to quickly locate your resonances, then doing a frequency sweep
>  (~30 min) to extract Q factors from S-parameter lineshapes.
>  You can also do a stub-length sweep if you're designing a TWPA."

Set `studies_to_run` accordingly. See `study_selection.md` for details.

---

## Step 5 — Write the populated YAML

Save the filled template to `stack/sessions/<device>_<YYYYMMDD>/design_params.yaml`.
Print the full YAML to the user and ask:
> "Does this look right? Any corrections before I start generating the CAD?"

---

## Step 6 — Hand off to CAD

Once the user confirms, proceed to `cad_conversation.md`.

"""
COMSOL build script — AlNtransmon D0 capacitance extraction.

Mirrors the COMSOL model constructed in COPY - AlNtransmon/D0_capacitance_extraction.ipynb,
re-implemented using the raw mph/JPype API so the Suite can drive it without any
dependency on the external cores.core_circuit_design library.

Patchable interface (run_custom_comsol_build injects these)
-----------------------------------------------------------
  OUT_DIR            → redirected to runs/<job_id>/
  PARAM_OVERRIDES    → dict; must contain "GDS_PATH"; optionally "LJJ_nH"
  MATERIAL_OVERRIDES → dict; optionally "sub_eps_r" (default 11.45 for Si at 4 K)

GDS layer conventions in the *_sim.gds files
---------------------------------------------
  Layer 200  (sc_bottom_sim) — bottom SC ground plane, 2-D face at z = 0
  Layer 201  (sc_top_sim)    — top SC film (qubit disk + ground ring), 2-D at z = H
  Layer  50  (ln_tsv)        — TSV cross-sections, extruded 0 → H
  Layer 100  (ln_jj)         — JJ lumped-element face, 2-D at z = H
  Layer 300  (ln_sim_ref)    — 1500×1500 µm bounding rectangle (used for Si & air)

3-D geometry
------------
  Si wafer:   1500×1500×300 µm block (z = 0 → H)
              minus TSV cylinders (TSV interior is air/ground)
  Air box:    1500×1500×1000 µm block (z = H → H+1000)

Physics (emw)
-------------
  PEC  — layer 200 boundaries (bottom ground)
  PEC  — layer 201 boundaries (top SC film, excluding JJ face)
  PEC  — TSV sidewall boundaries
  Lumped element inductor  — layer 100 (JJ), value = LJJ_nH
  Scattering BC — all outer box faces (default; PEC overrides at higher priority)

Study
-----
  stdEig — eigenfrequency, neigs=8, shift=5 GHz (eigunit GHz)
  The highest real eigenfrequency is the LC qubit mode.

Debug flags
-----------
  DEBUG=True prints extra COMSOL selection info before solving.
"""

import os
import sys
import time
import math
from pathlib import Path

# ── Patchable interface ────────────────────────────────────────────────────────
OUT_DIR            = "/default"
PARAM_OVERRIDES    = {
    "GDS_PATH": "PLACEHOLDER_GDS",
    "LJJ_nH":  "11.2",
}
MATERIAL_OVERRIDES = {
    "sub_eps_r":    "11.45",   # Si at 4 K (slightly lower than RT value 11.9)
    "sub_loss_tan": "0",
}

# ── Device / geometry constants ────────────────────────────────────────────────
H_UM      = 300.0    # wafer thickness (µm)
SIM_L_UM  = 1500.0   # simulation window (µm) — matches D0 notebook Lsim/W_sim
AIR_H_UM  = 1000.0   # air box height above wafer (µm)
N_EIGEN   = 8        # number of eigenmode solutions
SHIFT_GHZ = 5.0      # eigenfrequency shift point (GHz)

# GDS layer numbers in *_sim.gds (after sim_layer_offset=200 is applied)
LYR_SC_BOT = 200
LYR_SC_TOP = 201
LYR_TSV    = 50
LYR_JJ     = 100

DEBUG = False


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gate(condition, tag, msg):
    if not condition:
        print(f"[{tag} FAIL] {msg}", flush=True)
        sys.exit(1)


def JDA(vals):
    from jpype import JArray, JDouble
    return JArray(JDouble)([float(v) for v in vals])


def JI(x):
    from jpype import JInt
    return JInt(int(x))


def layer_name(n: int) -> str:
    """COMSOL ECAD layer name for GDS layer number n, datatype 0."""
    return f"LAYER{n}_0"


# ── Geometry ───────────────────────────────────────────────────────────────────

def _gds_layer_combos(gds_path: str) -> list:
    """Return sorted list of (layer, datatype) pairs present in the GDS.

    COMSOL ECAD import requires elevation/height/importlayer arrays whose
    length matches the total number of (layer, datatype) pairs in the file —
    even for layers we don't explicitly name.  This helper enumerates them so
    we can build arrays of the correct length.
    """
    import gdstk
    lib  = gdstk.read_gds(gds_path)
    cell = lib.cells[0]
    return sorted({(p.layer, p.datatype) for p in cell.polygons})


def build_geometry(m, gds_path: str) -> dict:
    """Import GDS layers and create 3-D wafer + air geometry.

    Returns a dict of COMSOL selection name suffixes for use in physics/materials.

    COMSOL ECAD import quirk: the elevation/height/importlayer arrays must be
    the same length as the total (layer, datatype) count in the GDS file, not
    just the subset we care about.  We enumerate all combos with gdstk and
    build full-length arrays, enabling only the layers we need.
    """
    log("Building geometry ...")
    g = m.component("comp1").geom("geom1")

    H  = f"{H_UM}[um]"
    L  = f"{SIM_L_UM}[um]"
    AH = f"{AIR_H_UM}[um]"

    # Enumerate all (layer, datatype) pairs present in the GDS file
    all_combos = _gds_layer_combos(gds_path)
    log(f"  GDS layer/datatype combos ({len(all_combos)}): {all_combos}")

    # Layers we want to activate in the 2D-faces import (all planar)
    FACE_LAYERS = {
        LYR_SC_BOT: 0.0,      # elevation (µm)
        LYR_SC_TOP: H_UM,
        LYR_JJ:     H_UM,
    }

    # Build full-length arrays for the 2D faces import node
    face_names  = [layer_name(lyr) for lyr, _ in all_combos]
    face_elev   = [FACE_LAYERS.get(lyr, 0.0) for lyr, _ in all_combos]
    face_height = [0.0] * len(all_combos)   # all 2-D (height=0)
    face_on     = ["on" if lyr in FACE_LAYERS else "off"
                   for lyr, _ in all_combos]

    # ── 2-D faces: sc_bottom (z=0), sc_top + JJ (z=H) ──────────────────────
    imp2d = g.create("imp_faces", "Import")
    imp2d.label("ECAD_2D_Faces")
    imp2d.set("type", "ecad")
    imp2d.set("filename", gds_path)
    imp2d.set("updategeomunit", False)
    imp2d.set("manualelevation", True)
    imp2d.set("splitbydatatype", True)
    imp2d.set("layername",   face_names)
    imp2d.set("elevation",   JDA(face_elev))
    imp2d.set("height",      JDA(face_height))
    imp2d.set("importlayer", face_on)
    imp2d.importData()
    log(f"  2-D faces imported (layers {LYR_SC_BOT}, {LYR_SC_TOP}, {LYR_JJ})")

    # Build full-length arrays for the TSV extrusion import node
    tsv_height = [H_UM if lyr == LYR_TSV else 0.0 for lyr, _ in all_combos]
    tsv_on     = ["on" if lyr == LYR_TSV else "off" for lyr, _ in all_combos]

    # ── TSV: extruded from z=0 to z=H ────────────────────────────────────────
    imp_tsv = g.create("imp_tsv", "Import")
    imp_tsv.label("ECAD_TSV")
    imp_tsv.set("type", "ecad")
    imp_tsv.set("filename", gds_path)
    imp_tsv.set("updategeomunit", False)
    imp_tsv.set("manualelevation", True)
    imp_tsv.set("splitbydatatype", True)
    imp_tsv.set("layername",   face_names)   # same full list
    imp_tsv.set("elevation",   JDA([0.0] * len(all_combos)))
    imp_tsv.set("height",      JDA(tsv_height))
    imp_tsv.set("importlayer", tsv_on)
    imp_tsv.importData()
    log(f"  TSV layer {LYR_TSV} imported (extruded 0→{H_UM} µm)")

    # ── Si wafer block ────────────────────────────────────────────────────────
    blk_si = g.create("blk_si", "Block")
    blk_si.label("Si_Wafer")
    blk_si.set("size", [L, L, H])
    blk_si.set("base", "center")
    blk_si.set("pos",  ["0", "0", f"{H_UM/2}[um]"])

    # ── Air box block ─────────────────────────────────────────────────────────
    blk_air = g.create("blk_air", "Block")
    blk_air.label("Air_Box")
    blk_air.set("size", [L, L, AH])
    blk_air.set("base", "center")
    blk_air.set("pos",  ["0", "0", f"{H_UM + AIR_H_UM/2}[um]"])

    # ── Explicit BoxSelections for domains ────────────────────────────────────
    # The 3-D ECAD extrusion (TSV) can cause COMSOL to split/merge block domains,
    # making the automatic "geom1_blk_X_dom" selections unreliable.  BoxSelections
    # identify domains purely by bounding-box position and survive Boolean ops.

    # Si wafer: z ∈ [0, H_UM].  BoxSelection with condition="inside" requires
    # the ENTIRE domain bounding box to lie within the selection box, so we must
    # extend zmax BEYOND H_UM (the domain's upper face) and zmin below 0.
    # JI() used for integer args to resolve JPype set(String,int)/set(String,boolean) ambiguity
    sel_si = g.create("sel_si_dom", "BoxSelection")
    sel_si.label("Si_Wafer_Dom")
    sel_si.set("entitydim", JI(3))
    sel_si.set("zmin",  "-1[um]")
    sel_si.set("zmax",  f"{H_UM + 1}[um]")   # must be > H_UM (not H_UM-1)
    sel_si.set("condition", "inside")

    # Air box: z ∈ [H_UM, H_UM + AIR_H_UM].  zmin must be < H_UM so domains
    # whose lower face sits exactly at H_UM are included by "inside" logic.
    sel_air = g.create("sel_air_dom", "BoxSelection")
    sel_air.label("Air_Box_Dom")
    sel_air.set("entitydim", JI(3))
    sel_air.set("zmin",  f"{H_UM - 1}[um]")   # must be < H_UM (not H_UM+1)
    sel_air.set("zmax",  f"{H_UM + AIR_H_UM + 1}[um]")
    sel_air.set("condition", "inside")

    g.run()
    log("  Geometry built and rebuilt")

    # Selection names confirmed by probing m.selection().tags() after geom.run().
    # ECAD auto-selections: geometry local uses dots (imp_faces_LAYER200_0.bnd)
    # but the model-global namespace (used by physics) translates dots → underscores.
    # BoxSelections are registered directly as geom1_<tag>.
    return {
        "sc_bot_bnd":  f"geom1_imp_faces_{layer_name(LYR_SC_BOT)}_bnd",
        "sc_top_bnd":  f"geom1_imp_faces_{layer_name(LYR_SC_TOP)}_bnd",
        "jj_bnd":      f"geom1_imp_faces_{layer_name(LYR_JJ)}_bnd",
        "tsv_dom":     f"geom1_imp_tsv_{layer_name(LYR_TSV)}_dom",
        "tsv_bnd":     f"geom1_imp_tsv_{layer_name(LYR_TSV)}_bnd",
        "si_dom":      "geom1_sel_si_dom",
        "air_dom":     "geom1_sel_air_dom",
    }


# ── Materials ──────────────────────────────────────────────────────────────────

def add_materials(m, sels: dict):
    """Assign Si to wafer domain, Air to air box + TSV interiors."""
    log("Adding materials ...")
    eps_r    = MATERIAL_OVERRIDES.get("sub_eps_r",    "11.45")
    loss_tan = MATERIAL_OVERRIDES.get("sub_loss_tan", "0")

    # Air — covers the air box AND the TSV interior (TSVs are through-holes)
    mat_air = m.component("comp1").material().create("mat_air", "Common")
    mat_air.label("Air")
    mat_air.selection().geom("geom1", 3)
    mat_air.selection().named(sels["air_dom"])
    mat_air.propertyGroup("def").set("relpermittivity",      "1")
    mat_air.propertyGroup("def").set("relpermeability",      "1")
    mat_air.propertyGroup("def").set("electricconductivity", "0")

    # Si wafer (excluding TSV volume — TSVs penetrate through the Si)
    mat_si = m.component("comp1").material().create("mat_si", "Common")
    mat_si.label("Silicon_4K")
    mat_si.selection().geom("geom1", 3)
    mat_si.selection().named(sels["si_dom"])
    mat_si.propertyGroup("def").set("relpermittivity",      eps_r)
    mat_si.propertyGroup("def").set("relpermeability",      "1")
    mat_si.propertyGroup("def").set("electricconductivity", "0")
    mat_si.propertyGroup("def").set("losstangent",          loss_tan)

    log(f"  Materials added (Si εr={eps_r})")


# ── Physics ────────────────────────────────────────────────────────────────────

def add_physics(m, sels: dict, ljj_nH: float):
    """EMW: explicit PEC on all conductor boundaries + lumped inductor at JJ.

    The default pec1 covers the 3D box outer walls; ECAD-imported 2D faces
    (sc_top, sc_bot, TSV) need EXPLICIT PEC features for COMSOL's LumpedElement
    topology check to recognise the adjacent boundaries as conductive.
    Without explicit PEC on these faces the solve raises:
      "Uniform lumped element should be placed between two conductive boundaries."

    Confirmed by probing: inductance property is 'Lelement', not 'L'.
    """
    log("Adding EMW physics ...")
    phys = m.component("comp1").physics().create(
        "emw", "ElectromagneticWaves", "geom1"
    )
    phys.label("EMW_Transmon_D0")
    # default pec1 covers all outer box walls as PEC (shielded box)

    # ── Explicit PEC on ECAD-imported conducting surfaces ─────────────────────
    # Required: LumpedElement topology check uses EXPLICIT feature assignments,
    # not the default pec1, to determine whether adjacent boundaries are conductive.
    pec_top = phys.create("pec_sc_top", "PerfectElectricConductor", 2)
    pec_top.label("PEC_SC_Top")
    pec_top.selection().named(sels["sc_top_bnd"])

    pec_bot = phys.create("pec_sc_bot", "PerfectElectricConductor", 2)
    pec_bot.label("PEC_SC_Bot")
    pec_bot.selection().named(sels["sc_bot_bnd"])

    pec_tsv = phys.create("pec_tsv", "PerfectElectricConductor", 2)
    pec_tsv.label("PEC_TSV")
    pec_tsv.selection().named(sels["tsv_bnd"])

    # ── Lumped element: JJ inductor (layer 100) ───────────────────────────────
    le = phys.create("le_jj", "LumpedElement", 2)
    le.label("JJ_Inductor")
    le.set("LumpedElementType", "Inductor")
    le.set("Lelement", f"{ljj_nH}[nH]")   # correct property name; [nH] unit
    le.selection().named(sels["jj_bnd"])

    log(f"  EMW physics added (JJ inductor: LJJ={ljj_nH} nH, explicit PEC on ECAD faces)")


# ── Mesh ───────────────────────────────────────────────────────────────────────

def add_mesh(m):
    """Free-tet mesh with geometry-based size calibration.

    EMW physics-controlled meshing (autoMeshSize) requires a known frequency for
    wavelength-based sizing, which is unavailable in eigenfrequency studies.
    Instead: explicit Size + FreeTet sequence, hauto=4 (fine), geometry-calibrated.
    """
    log("Meshing ...")
    mesh = m.component("comp1").mesh("mesh1")
    # Global size node — hauto 1=extremely fine, 9=coarser; 4 matches 'fine' preset
    sz = mesh.create("size1", "Size")
    sz.set("hauto", JI(4))
    # Free tetrahedral fill for all domains
    mesh.create("ftet1", "FreeTet")
    mesh.run()
    log("  Mesh done")


# ── Study ──────────────────────────────────────────────────────────────────────

def add_eigenfrequency_study(m):
    """Eigenfrequency study: N_EIGEN modes, shift at SHIFT_GHZ GHz."""
    log("Adding eigenfrequency study ...")
    m.study().create("std1")
    m.study("std1").label("stdEig")
    m.study("std1").create("eig", "Eigenfrequency")

    feat = m.study("std1").feature("eig")
    feat.set("neigsactive", "on")
    feat.set("neigs",   str(N_EIGEN))
    feat.set("eigunit", "GHz")                         # CRITICAL — avoid rad/s confusion
    feat.set("shift",   f"{SHIFT_GHZ}[GHz]")

    log(f"  stdEig: {N_EIGEN} modes, shift={SHIFT_GHZ} GHz")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import mph

    gds_path = PARAM_OVERRIDES.get("GDS_PATH", "")
    ljj_nH   = float(PARAM_OVERRIDES.get("LJJ_nH", "11.2"))

    gate(os.path.isfile(gds_path), "GATE-GDS", f"GDS not found: {gds_path}")
    os.makedirs(OUT_DIR, exist_ok=True)

    log("=== build_D0_capext.py ===")
    log(f"GDS     : {gds_path}")
    log(f"LJJ     : {ljj_nH} nH")
    log(f"OUT_DIR : {OUT_DIR}")
    log(f"Wafer   : {SIM_L_UM}×{SIM_L_UM}×{H_UM} µm")
    log(f"Air box : {AIR_H_UM} µm above wafer")
    log(f"Si εr   : {MATERIAL_OVERRIDES.get('sub_eps_r', '11.45')}")

    log("Starting COMSOL client ...")
    client = mph.start(cores=4)
    pymodel = client.create("D0_Capext")
    m = pymodel.java

    # 3-D component and geometry
    # (geometry dims are passed as Python strings directly — no COMSOL params needed)
    m.component().create("comp1", True)
    m.component("comp1").geom().create("geom1", 3)
    m.component("comp1").geom("geom1").lengthUnit("um")
    m.component("comp1").geom("geom1").angularUnit("deg")
    m.component("comp1").mesh().create("mesh1")

    sels = build_geometry(m, gds_path)
    add_materials(m, sels)
    add_physics(m, sels, ljj_nH)
    add_mesh(m)
    add_eigenfrequency_study(m)

    mph_out = os.path.join(OUT_DIR, "D0_capext_built.mph")
    log(f"Saving model: {mph_out}")
    pymodel.save(mph_out)
    size_mb = Path(mph_out).stat().st_size / 1e6
    gate(size_mb > 0.05, "GATE-SAVE", f"Saved MPH too small ({size_mb:.2f} MB)")
    log(f"Saved {size_mb:.1f} MB: {mph_out}")

    client.remove(pymodel)
    log("=== build_D0_capext.py DONE ===")
    log("Next: run_eigenfrequency_study(mph_path, n_modes=8, freq_start_ghz=1, freq_stop_ghz=10)")
    log(f"Expected max eigenfreq ≈ 4–7 GHz depending on d_q (see D0 reference table)")


if __name__ == "__main__":
    main()

"""
COMSOL build script — half-wave CPW resonator.

Patchable interface (run_custom_comsol_build injects these):
  OUT_DIR            → redirected to runs/<job_id>/
  PARAM_OVERRIDES    → injected geom_params dict
  MATERIAL_OVERRIDES → injected material_params dict

Geometry (all units µm, resonator centred at y=0)
--------------------------------------------------
  Layer 0 (LAYER0_0) — Al metal: centre conductor (3 segs) + ground planes
  Layer 1 (LAYER1_0) — CPW slots (imported off — gaps = absence of metal)
  Layer 2 (LAYER2_0) — port markers (imported off — used only for reference)

  Substrate block: z ∈ [-SUB_T, 0]   (Si, εr=11.9)
  Air box:         z ∈ [0,  AIR_H]
  Metal surfaces:  z = 0 plane (2D PEC boundaries from ECAD import)

Studies built
-------------
  stdEig  — eigenfrequency (5 modes, shift at target f0)
  stdFreq — frequency sweep (optional, run after eigenfrequency confirms f0)

Debug flags
-----------
  Set DEBUG=True below or pass via PARAM_OVERRIDES {"DEBUG": "1"}.
"""

import os
import sys
import time
import math
from pathlib import Path

# ── Patchable interface ────────────────────────────────────────────────────────
OUT_DIR            = "/default"
PARAM_OVERRIDES    = {"GDS_PATH": "PLACEHOLDER_GDS"}
MATERIAL_OVERRIDES = {"sub_eps_r": "11.9", "sub_loss_tan": "0"}

# ── Design constants (must match cad_resonator_halfwave.py) ───────────────────
W_UM       = 5.0        # center conductor width
G_UM       = 20.0       # CPW gap
CG_UM      = 10.0       # coupling gap each end
FEED_UM    = 500.0      # feed line length each end
GROUND_W   = 100.0      # ground plane width each side
LENGTH_UM  = 10730.0    # resonator body length (half-wave at 5.5 GHz on Si)
SUB_T_UM   = 300.0      # substrate thickness
AIR_H_UM   = 1000.0     # air box height above metal
TARGET_GHZ = 5.5        # used as eigenfrequency shift point

# Derived extents
Y_RES_TOP =  LENGTH_UM / 2
Y_RES_BOT = -LENGTH_UM / 2
Y_CG_TOP  =  Y_RES_TOP + CG_UM
Y_CG_BOT  =  Y_RES_BOT - CG_UM
Y_TOP_FEED=  Y_CG_TOP  + FEED_UM
Y_BOT_FEED=  Y_CG_BOT  - FEED_UM
Y_MID     = (Y_TOP_FEED + Y_BOT_FEED) / 2        # = 0
TOTAL_LEN =  Y_TOP_FEED - Y_BOT_FEED             # = 11750 µm
X_HALF    =  W_UM/2 + G_UM + GROUND_W            # = 122.5 µm

DEBUG = False


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gate(condition, tag, msg):
    if not condition:
        print(f"[{tag} FAIL] {msg}", flush=True)
        sys.exit(1)


def JDA(v):
    from jpype import JArray, JDouble
    return JArray(JDouble)([float(x) for x in v])


def JIA(v):
    from jpype import JArray, JInt
    return JArray(JInt)([int(x) for x in v])


def JI(x):
    from jpype import JInt
    return JInt(x)


# ── Geometry builder ───────────────────────────────────────────────────────────

def build_geometry(m, gds_path):
    """Create 3-D geometry: ECAD metal + substrate block + air box."""
    log("Building geometry ...")
    g = m.component("comp1").geom("geom1")

    # ── ECAD import: Layer 0 (metal) only ──
    g.create("imp1", "Import")
    g.feature("imp1").label("ECAD_Metal")
    g.feature("imp1").set("type", "ecad")
    g.feature("imp1").set("filename", gds_path)
    g.feature("imp1").set("updategeomunit", False)   # keep µm from GDS
    g.feature("imp1").set("manualelevation", True)
    g.feature("imp1").set("splitbydatatype", True)
    g.feature("imp1").set("layername", ["LAYER0_0", "LAYER1_0", "LAYER2_0"])
    g.feature("imp1").set("height",      JDA([0, 0, 0]))   # 2D planar surfaces
    g.feature("imp1").set("elevation",   JDA([0, 0, 0]))   # at z = 0
    g.feature("imp1").set("importlayer", ["on", "off", "off"])
    g.feature("imp1").importData()
    log("  ECAD import done")

    # ── Substrate block: centred at (0, Y_MID, -SUB_T/2) ──
    g.create("blk_sub", "Block")
    g.feature("blk_sub").label("Substrate_Si")
    g.feature("blk_sub").set("size", [
        f"{2*X_HALF}[um]",
        f"{TOTAL_LEN}[um]",
        f"{SUB_T_UM}[um]",
    ])
    g.feature("blk_sub").set("base", "center")
    g.feature("blk_sub").set("pos", [
        "0",
        f"{Y_MID}[um]",
        f"{-SUB_T_UM/2}[um]",
    ])

    # ── Air box: centred at (0, Y_MID, AIR_H/2) ──
    # Slightly larger in x/y so the outer faces act as scattering BC, not shared
    # with the substrate block edges.
    AIR_MARGIN = 0.0   # µm — set >0 if you want the air to overhang the chip
    g.create("blk_air", "Block")
    g.feature("blk_air").label("Air_Box")
    g.feature("blk_air").set("size", [
        f"{2*X_HALF + 2*AIR_MARGIN}[um]",
        f"{TOTAL_LEN}[um]",
        f"{AIR_H_UM}[um]",
    ])
    g.feature("blk_air").set("base", "center")
    g.feature("blk_air").set("pos", [
        "0",
        f"{Y_MID}[um]",
        f"{AIR_H_UM/2}[um]",
    ])

    # ── Build ──
    g.run()
    log("  Geometry built")


# ── Materials ──────────────────────────────────────────────────────────────────

def add_materials(m):
    """Assign Si to substrate domain, air to air box domain."""
    log("Adding materials ...")
    eps_r    = MATERIAL_OVERRIDES.get("sub_eps_r",    "11.9")
    loss_tan = MATERIAL_OVERRIDES.get("sub_loss_tan", "0")

    # Silicon substrate
    mat_si = m.component("comp1").material().create("mat_si", "Common")
    mat_si.label("Silicon_Si")
    mat_si.selection().geom("geom1", 3)
    mat_si.selection().named("geom1_blk_sub_dom")   # domain selection from block
    mat_si.propertyGroup("def").set("relpermittivity",   eps_r)
    mat_si.propertyGroup("def").set("relpermeability",   "1")
    mat_si.propertyGroup("def").set("electricconductivity", "0")
    mat_si.propertyGroup("def").set("losstangent",       loss_tan)

    # Air
    mat_air = m.component("comp1").material().create("mat_air", "Common")
    mat_air.label("Air")
    mat_air.selection().geom("geom1", 3)
    mat_air.selection().named("geom1_blk_air_dom")
    mat_air.propertyGroup("def").set("relpermittivity",     "1")
    mat_air.propertyGroup("def").set("relpermeability",     "1")
    mat_air.propertyGroup("def").set("electricconductivity","0")

    log("  Materials added")


# ── Physics ────────────────────────────────────────────────────────────────────

def add_physics(m):
    """EMW: PEC on metal, scattering BC on outer box, lumped ports at feed ends."""
    log("Adding EMW physics ...")
    phys = m.component("comp1").physics().create("emw", "ElectromagneticWaves", "geom1")
    phys.label("EMW_CPW")

    # ── Explicit PEC on ECAD metal surfaces (Al layer 0) ──
    # Use a unique tag — EMW auto-creates a default feature with tag "pec1";
    # creating another feature with the same tag conflicts.  The model-global
    # selection name follows the pattern geom1_<import_tag>_<layer>_bnd:
    #   import tag = "imp1"  →  geom1_imp1_LAYER0_0_bnd
    # The default EMW pec1 covers all outer box walls as PEC (shielded box);
    # we only need an explicit feature for the ECAD metal faces, because
    # LumpedPort/LumpedElement topology checks require explicit assignments.
    pec_metal = phys.create("pec_metal", "PerfectElectricConductor", 2)
    pec_metal.label("PEC_Metal_Al")
    pec_metal.selection().named("geom1_imp1_LAYER0_0_bnd")

    # NOTE: ScatteringBoundaryCondition does not exist in this COMSOL version.
    # The default pec1 (auto-created by EMW) covers all outer box boundaries
    # as a shielded-box PEC — appropriate for eigenfrequency studies.

    # ── Lumped Port 1 — bottom feed end (y = Y_BOT_FEED) ──
    lp1 = phys.create("lp1", "LumpedPort", 2)
    lp1.label("Port1_Bottom")
    lp1.set("PortNumber",   JI(1))
    lp1.set("PortExcited",  "on")   # port 1 is the driven port
    # Port face: boundary at y ≈ Y_BOT_FEED (tolerance 1 µm)
    lp1.selection().geom("geom1", 2)
    lp1.selection().set()  # empty — must be set interactively or via box selection
    # NOTE: box selection for port faces is set after geom.run() in finalize_ports()

    # ── Lumped Port 2 — top feed end (y = Y_TOP_FEED) ──
    lp2 = phys.create("lp2", "LumpedPort", 2)
    lp2.label("Port2_Top")
    lp2.set("PortNumber",  JI(2))
    lp2.set("PortExcited", "off")
    lp2.selection().geom("geom1", 2)
    lp2.selection().set()

    log("  EMW physics added (port face selections must be set in GUI before solve)")
    log("  TIP: in COMSOL GUI, use Box Selection at y≈Y_BOT_FEED and y≈Y_TOP_FEED")
    log(f"  Port 1 face: y ≈ {Y_BOT_FEED:.0f} µm")
    log(f"  Port 2 face: y ≈ {Y_TOP_FEED:.0f} µm")

    return phys


# ── Mesh ───────────────────────────────────────────────────────────────────────

def add_mesh(m):
    """Free-tet mesh with geometry-based size calibration.

    mesh.autoMeshSize() fails for EMW eigenfrequency studies — it requires a
    known frequency for wavelength-based sizing, which is unavailable before
    solving.  Explicit FreeTet + Size sequence is used instead.
    """
    log("Meshing ...")
    mesh = m.component("comp1").mesh("mesh1")
    sz = mesh.create("size1", "Size")
    sz.set("hauto", JI(4))          # 4=fine; range 1(coarsest)–9(finest)
    mesh.create("ftet1", "FreeTet")
    mesh.run()
    log("  Mesh done")


# ── Studies ────────────────────────────────────────────────────────────────────

def add_eigenfrequency_study(m):
    """Eigenfrequency study: 5 modes near 5.5 GHz."""
    log("Adding eigenfrequency study ...")
    m.study().create("std1")
    m.study("std1").label("stdEig")
    m.study("std1").create("eig", "Eigenfrequency")

    feat = m.study("std1").feature("eig")
    feat.set("neigsactive", "on")
    feat.set("neigs",    "5")
    feat.set("eigunit",  "GHz")           # CRITICAL: avoid rad/s confusion
    feat.set("shift",    f"{TARGET_GHZ}[GHz]")
    log(f"  stdEig: 5 modes, shift={TARGET_GHZ} GHz")


def add_frequency_sweep_study(m, f_start_ghz=5.0, f_stop_ghz=6.0, n_pts=201):
    """Optional S-parameter frequency sweep for Q extraction."""
    log("Adding frequency sweep study ...")
    m.study().create("std2")
    m.study("std2").label("stdFreq")
    m.study("std2").create("freq", "Frequency")

    feat = m.study("std2").feature("freq")
    feat.set("plist", f"range({f_start_ghz},{(f_stop_ghz-f_start_ghz)/(n_pts-1):.6f},{f_stop_ghz})[GHz]")
    log(f"  stdFreq: {n_pts} pts from {f_start_ghz} to {f_stop_ghz} GHz")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import mph

    # ── Validation ──
    gds_path = PARAM_OVERRIDES.get("GDS_PATH", "")
    gate(os.path.isfile(gds_path), "GATE-1", f"GDS not found: {gds_path}")
    os.makedirs(OUT_DIR, exist_ok=True)

    log("=== build_resonator_halfwave.py ===")
    log(f"GDS       : {gds_path}")
    log(f"OUT_DIR   : {OUT_DIR}")
    log(f"Resonator : L={LENGTH_UM} µm  W={W_UM} µm  G={G_UM} µm  CG={CG_UM} µm")
    log(f"Domain    : x=±{X_HALF} µm  y=[{Y_BOT_FEED:.0f}, {Y_TOP_FEED:.0f}] µm")
    log(f"Substrate : {SUB_T_UM} µm Si (εr={MATERIAL_OVERRIDES.get('sub_eps_r','11.9')})")
    log(f"Air box   : {AIR_H_UM} µm")

    # ── Start COMSOL ──
    log("Starting COMSOL client ...")
    client = mph.start(cores=4)
    pymodel = client.create("CPW_HalfWave_Resonator")
    m = pymodel.java

    # ── Apply param overrides ──
    for name, val in PARAM_OVERRIDES.items():
        if name not in ("GDS_PATH", "DEBUG"):
            m.param().set(name, val)

    # ── Component and geometry ──
    m.component().create("comp1", True)
    m.component("comp1").geom().create("geom1", 3)
    m.component("comp1").geom("geom1").lengthUnit("um")
    m.component("comp1").geom("geom1").angularUnit("deg")
    m.component("comp1").mesh().create("mesh1")

    build_geometry(m, gds_path)
    add_materials(m)
    add_physics(m)
    add_mesh(m)
    add_eigenfrequency_study(m)
    add_frequency_sweep_study(m)   # builds it but does not solve

    # ── Save built model ──
    mph_out = os.path.join(OUT_DIR, "resonator_built.mph")
    log(f"Saving model: {mph_out}")
    pymodel.save(mph_out)
    size_mb = Path(mph_out).stat().st_size / 1e6
    gate(size_mb > 0.1, "GATE-SAVE", f"Saved MPH is too small ({size_mb:.2f} MB) — likely empty")
    log(f"Saved {size_mb:.1f} MB: {mph_out}")

    client.remove(pymodel)
    log("=== build_resonator_halfwave.py DONE ===")
    log(f"Open {mph_out} in COMSOL GUI to:")
    log("  1. Assign port face boundaries (Port 1/2 lumped port selections)")
    log("  2. Run stdEig to confirm f0 ≈ 5.5 GHz")
    log("  3. Run stdFreq for Q extraction")


if __name__ == "__main__":
    main()

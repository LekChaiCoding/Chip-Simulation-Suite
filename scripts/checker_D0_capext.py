#!/usr/bin/env python3
"""GDS checker for AlNtransmon D0 capacitance-extraction geometry.

Validates the ``*_sim.gds`` files produced by the D0 notebook before
passing them into COMSOL.  The _sim variants have layer offsets applied,
so the expected layers are 200, 201, 50, 100, and 300 (not the raw 0, 1, ...).

Usage with verify_cad MCP tool
------------------------------
    verify_cad(
        gds_path       = ".../tmp_cad_data/D0_capext350_sim.gds",
        checker_script = ".../scripts/checker_D0_capext.py",
        gds_var        = "GDS_PATH",
    )

Direct CLI usage (for debugging without the MCP tool)
------------------------------------------------------
    python checker_D0_capext.py --gds /path/to/D0_capext350_sim.gds
"""

import argparse
import sys

# ── Override target ───────────────────────────────────────────────────────────
GDS_PATH = "/path/to/default.gds"   # overridden by verify_cad tool

# ── Expected geometry constants ───────────────────────────────────────────────
# After the _sim.gds processing, all layers have the sim_layer_offset (200)
# applied to the SC layers.
REQUIRED_LAYERS = {200, 201, 50, 100, 300}

# Simulation reference window: 1500 × 1500 µm (set in D0 notebook)
SIM_WINDOW_UM = 1500.0

# Layer 100 (JJ lumped element) must have exactly 1 polygon per qubit model
JJ_LAYER = 100
JJ_POLY_COUNT = 1

# Layer 201 (sc_top_sim: qubit disk + ground) must have at least 1 polygon
SC_TOP_LAYER = 201
SC_TOP_MIN_POLYS = 1

# Layer 50 (TSV) must have at least 1 polygon (8 TSVs around the qubit)
TSV_LAYER = 50
TSV_MIN_POLYS = 1


def main() -> int:
    """Run all geometry checks.  Return 0 if all pass, >0 if any fail."""
    import gdstk

    try:
        lib = gdstk.read_gds(GDS_PATH)
    except Exception as exc:
        print(f"[FAIL] Cannot read GDS file: {exc}", flush=True)
        return 1

    failures = []

    # ── Check 1: exactly 1 cell ───────────────────────────────────────────────
    if len(lib.cells) != 1:
        failures.append(
            f"Expected 1 cell, got {len(lib.cells)}: "
            f"{[c.name for c in lib.cells]}"
        )

    if not lib.cells:
        for f in failures:
            print(f"[FAIL] {f}", flush=True)
        return len(failures)

    cell = lib.cells[0]

    # ── Check 2: required layers all present ──────────────────────────────────
    actual_layers = {poly.layer for poly in cell.polygons}
    missing = REQUIRED_LAYERS - actual_layers
    if missing:
        failures.append(
            f"Missing layers: {sorted(missing)} "
            f"(present: {sorted(actual_layers)})"
        )

    # ── Check 3: layer 201 (sc_top_sim) has at least 1 polygon ───────────────
    sc_top_polys = [p for p in cell.polygons if p.layer == SC_TOP_LAYER]
    if len(sc_top_polys) < SC_TOP_MIN_POLYS:
        failures.append(
            f"Layer {SC_TOP_LAYER} (sc_top_sim) has {len(sc_top_polys)} polygons, "
            f"expected ≥ {SC_TOP_MIN_POLYS}"
        )

    # ── Check 4: layer 100 (JJ) has exactly 1 polygon ────────────────────────
    jj_polys = [p for p in cell.polygons if p.layer == JJ_LAYER]
    if len(jj_polys) != JJ_POLY_COUNT:
        failures.append(
            f"Layer {JJ_LAYER} (JJ lumped element) has {len(jj_polys)} polygons, "
            f"expected exactly {JJ_POLY_COUNT}"
        )

    # ── Check 5: layer 50 (TSV) has at least TSV_MIN_POLYS polygons ──────────
    tsv_polys = [p for p in cell.polygons if p.layer == TSV_LAYER]
    if len(tsv_polys) < TSV_MIN_POLYS:
        failures.append(
            f"Layer {TSV_LAYER} (TSV) has {len(tsv_polys)} polygons, "
            f"expected ≥ {TSV_MIN_POLYS}"
        )

    # ── Check 6: bounding box within sim window ───────────────────────────────
    # Layer 300 (sim_ref) defines the simulation boundary.
    ref_polys = [p for p in cell.polygons if p.layer == 300]
    if ref_polys:
        import numpy as np
        for p in ref_polys:
            pts = np.array(p.points)
            w = pts[:, 0].max() - pts[:, 0].min()
            h = pts[:, 1].max() - pts[:, 1].min()
            if w > SIM_WINDOW_UM * 1.05 or h > SIM_WINDOW_UM * 1.05:
                failures.append(
                    f"Layer 300 (sim_ref) bounding box {w:.0f}×{h:.0f} µm "
                    f"exceeds expected {SIM_WINDOW_UM}×{SIM_WINDOW_UM} µm"
                )
    else:
        failures.append("Layer 300 (sim_ref) has no polygons — simulation boundary missing")

    # ── Report ────────────────────────────────────────────────────────────────
    for f in failures:
        print(f"[FAIL] {f}", flush=True)

    if not failures:
        n_sc_top = len(sc_top_polys)
        n_tsv = len(tsv_polys)
        print(
            f"[PASS] All checks passed — {GDS_PATH} "
            f"(sc_top: {n_sc_top} polys, TSVs: {n_tsv}, JJ: {len(jj_polys)})",
            flush=True,
        )

    return len(failures)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="D0 capacitance-extraction GDS checker")
    p.add_argument("--gds", metavar="PATH",
                   help="GDS file to check (overrides GDS_PATH constant)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.gds:
        GDS_PATH = args.gds  # noqa: F811
    sys.exit(main())

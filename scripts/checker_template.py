#!/usr/bin/env python3
"""GDS geometry checker template — copy and customise for each device.

This file shows the interface required by the ``verify_cad`` MCP tool so you
can write a checker for any chip geometry.  Rename a copy of this file to
something like ``check_my_device.py`` and fill in the device-specific checks.

Usage with the ``verify_cad`` MCP tool
--------------------------------------
    verify_cad(
        gds_path       = "/path/to/your_device.gds",
        checker_script = "/path/to/check_my_device.py",
        gds_var        = "GDS_PATH",   # must match the constant name below
    )

The tool will:
  1. Import this module.
  2. Override the ``GDS_PATH`` constant with the value passed as ``gds_path``.
  3. Call ``main()`` and capture its return code + printed output.
  4. Count ``[FAIL]`` lines and return ``passed = (rc == 0)``.

Requirements for a valid checker script
----------------------------------------
  1. A module-level string constant whose name matches ``gds_var`` (default
     ``RECR`` for backward compatibility with the JTWPA checker; ``GDS_PATH``
     is the recommended name for new devices).
  2. A ``main()`` function that returns 0 on full pass, >0 on any failure.
  3. Each failure printed as ``[FAIL] <description>`` on its own line.
  4. Imports of heavy libraries (gdstk, numpy) done **inside** ``main()``, not
     at module top-level, so that importing the module as a Python module is
     side-effect-free.

Debugging
---------
Run this file directly to test against a GDS without the MCP tool:

    python check_my_device.py --gds /path/to/your_device.gds

or just:

    python check_my_device.py          # uses the GDS_PATH default
"""

import argparse
import sys

# ── Override target ───────────────────────────────────────────────────────────
# verify_cad patches this line — do not rename the constant without updating
# the gds_var argument in your verify_cad call.
GDS_PATH = "/path/to/default.gds"   # overridden by verify_cad tool


# ── Customisation constants (edit for your device) ────────────────────────────
# Uncomment and fill in expected values for your geometry checks.

# EXPECTED_LAYERS = {0, 1, 3}           # set of layer numbers in the GDS
# EXPECTED_CELL_COUNT = 1               # number of top-level cells
# EXPECTED_POLYGON_COUNT_LAYER0 = 42    # polygons in metal layer
# BBOX_XMIN_UM, BBOX_XMAX_UM = 0.0, 500.0   # bounding box x range in µm
# BBOX_YMIN_UM, BBOX_YMAX_UM = 0.0, 500.0   # bounding box y range in µm


# ── Checker ───────────────────────────────────────────────────────────────────

def main() -> int:
    """Run all geometry checks.  Return 0 if all pass, >0 if any fail."""
    import gdstk  # import here so the module is import-safe

    # ── Load GDS ──────────────────────────────────────────────────────────────
    lib = gdstk.read_gds(GDS_PATH)
    failures = []

    # ── Check 1: expected layer set ───────────────────────────────────────────
    # Verify the GDS contains exactly the expected layers.
    #
    # actual_layers = {
    #     poly.layer
    #     for cell in lib.cells
    #     for poly in cell.polygons
    # }
    # if actual_layers != EXPECTED_LAYERS:
    #     failures.append(
    #         f"Layer set mismatch: expected {sorted(EXPECTED_LAYERS)}, "
    #         f"got {sorted(actual_layers)}"
    #     )

    # ── Check 2: cell count ───────────────────────────────────────────────────
    # if len(lib.cells) != EXPECTED_CELL_COUNT:
    #     failures.append(
    #         f"Cell count: expected {EXPECTED_CELL_COUNT}, got {len(lib.cells)}"
    #     )

    # ── Check 3: polygon count in a specific layer ────────────────────────────
    # top = lib.cells[0]
    # layer0_polys = [p for p in top.polygons if p.layer == 0]
    # if len(layer0_polys) != EXPECTED_POLYGON_COUNT_LAYER0:
    #     failures.append(
    #         f"Layer 0 polygon count: expected {EXPECTED_POLYGON_COUNT_LAYER0}, "
    #         f"got {len(layer0_polys)}"
    #     )

    # ── Check 4: bounding box ─────────────────────────────────────────────────
    # top = lib.cells[0]
    # bbox = top.bounding_box()   # returns [[xmin, ymin], [xmax, ymax]] in µm
    # if bbox is None:
    #     failures.append("Cell has no geometry (empty bounding box)")
    # else:
    #     xmin, ymin = bbox[0]
    #     xmax, ymax = bbox[1]
    #     if not (BBOX_XMIN_UM <= xmin <= BBOX_XMAX_UM):
    #         failures.append(
    #             f"Bounding box x_min out of range: {xmin:.3f} µm "
    #             f"(expected {BBOX_XMIN_UM}–{BBOX_XMAX_UM} µm)"
    #         )

    # ── Check 5: specific feature (e.g. junction bar spacing) ─────────────────
    # Add device-specific geometric checks here.  Each failure appended as a
    # plain string; the [FAIL] prefix is added below.

    # ── Report ────────────────────────────────────────────────────────────────
    for f in failures:
        print(f"[FAIL] {f}", flush=True)

    if not failures:
        print(f"[PASS] All checks passed — {GDS_PATH}", flush=True)

    return len(failures)


# ── CLI wrapper ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GDS geometry checker")
    p.add_argument("--gds", metavar="PATH",
                   help="GDS file to check (overrides GDS_PATH constant)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.gds:
        GDS_PATH = args.gds  # noqa: F811  (intentional reassignment for CLI)
    sys.exit(main())

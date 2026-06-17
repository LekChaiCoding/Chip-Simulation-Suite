"""End-to-end CAD tests: generate the real device GDS, verify it, and exercise
the custom-CAD path using the same script as a stand-in for a user script.

This exercises the first link of the pipeline against the user's actual CAD
(the device imported into COMSOL) with no mocking.
"""

from __future__ import annotations

import pytest

from comsol_suite.config import load_config
from comsol_suite.tools.cad import generate_cad, run_custom_cad, verify_cad


def test_generate_then_verify(tmp_path):
    gen = generate_cad(output_dir=str(tmp_path), debug=True)
    assert gen["ok"], f"generate_cad failed: {gen.get('log_tail')}"
    assert gen["gds_path"] and gen["gds_path"].endswith(".gds")

    ver = verify_cad(gen["gds_path"], debug=True)
    assert ver["passed"], f"verify_cad failed:\n{ver.get('report')}"
    assert ver["n_failures"] == 0


def test_reference_gds_passes_checker():
    # The committed reference GDS must itself pass the geometry checker.
    ver = verify_cad(debug=True)
    assert ver["passed"], ver.get("report")


def test_run_custom_cad_produces_gds(tmp_path):
    """run_custom_cad can drive converter_group_recreation.py as a custom script.

    Uses the project's existing CAD script as the 'user' script to prove the
    generic path works end-to-end without needing a separate fixture script.
    """
    cfg = load_config()
    result = run_custom_cad(
        cad_script=str(cfg.script("cad_generator")),
        output_dir=str(tmp_path / "custom_cad"),
        out_gds_var="OUT_GDS",
        out_png_var="OUT_PNG",
        gds_filename="custom_device.gds",
        debug=True,
    )
    assert result["ok"], f"run_custom_cad failed: {result.get('log_tail')}"
    assert result["gds_path"] is not None
    assert result["gds_path"].endswith("custom_device.gds")

    # The generated GDS should also pass the geometry checker.
    ver = verify_cad(result["gds_path"], debug=True)
    assert ver["passed"], f"custom CAD GDS failed geometry check:\n{ver.get('report')}"

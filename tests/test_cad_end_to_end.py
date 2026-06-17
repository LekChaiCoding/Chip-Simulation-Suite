"""End-to-end CAD test: generate the real device GDS, then verify it passes.

This exercises the first link of the pipeline against the user's actual CAD
(the device imported into COMSOL) with no mocking.
"""

from __future__ import annotations

import pytest

from comsol_suite.tools.cad import generate_cad, verify_cad


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

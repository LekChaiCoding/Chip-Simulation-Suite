"""End-to-end fitting test: fit the real bridge/003 sweep, check sane output.

The bridge/003 ``stub_length_sweep.dat`` stands in for what COMSOL emits, so this
proves the S-parameters -> circuit-extraction half of the pipeline using the
user's real data. Run synchronously (background=False) for determinism.
"""

from __future__ import annotations

import csv

from comsol_suite.config import load_config
from comsol_suite.jobs import JobRegistry
from comsol_suite.tools.fitting import run_abcd_fit


def test_abcd_fit_produces_sane_results(tmp_path):
    cfg = load_config()
    registry = JobRegistry(tmp_path / "runs")

    out = run_abcd_fit(registry, output_dir=str(tmp_path / "abcd"),
                       background=False, debug=True)
    job = registry.get(out["job_id"])
    assert job is not None
    assert job.status == "completed", f"fit failed: {job.error}"

    results_csv = [p for p in job.result["output_files"]
                   if p.endswith("abcd_fit_results.csv")]
    assert results_csv, "no abcd_fit_results.csv produced"

    with open(results_csv[0], newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "results CSV is empty"

    # Every fit should at least return a finite, positive Cg (no crashes / NaNs).
    # Note: abcd_fit.py deliberately explores 5 objectives x 3 topologies,
    # including 2-parameter fits that are EXPECTED to collapse in the bridge/003
    # breakdown region (300-400 um) — so we do not require every row to be sane.
    for r in rows:
        cg = float(r["Cg_fit_fF"])
        assert cg == cg and cg > 0, f"bad Cg: {cg}"          # not NaN, positive

    # The meaningful physics check: the canonical reference objective
    # (topology A, fit objective A — the Julia-reference complex residual) must
    # give a well-behaved ~50-ohm characteristic impedance for every stub.
    canonical = [r for r in rows
                 if r["topology"] == "topoA" and r["fit_method"] == "fitA"]
    assert canonical, "no topoA/fitA rows present"
    for r in canonical:
        z0 = float(r["Z0_implied_ohm"])
        assert 40.0 < z0 < 60.0, (
            f"canonical fit Z0 off 50 ohm at stub {r['stub_um']}: {z0}")

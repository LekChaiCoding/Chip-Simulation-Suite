"""COMSOL stage tests — exercise the dry-run / health-check path only.

Actually solving requires a live COMSOL connection (out of scope here), so these
tests confirm the *wrapping* is sound: health checks run, and each tool's
dry-run returns a well-formed plan without attempting a solve.
"""

from __future__ import annotations

from comsol_suite.jobs import JobRegistry
from comsol_suite.tools import comsol


def test_health_check_runs_without_comsol():
    # Should never raise, even with no COMSOL installed. mph is optional.
    out = comsol.comsol_health_check(comsol_host=None)
    assert "mph_available" in out
    assert "detail" in out


def test_build_dry_run_reports_plan(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.build_comsol_model(reg, gds_path="dummy.gds", dry_run=True)
    assert out["dry_run"] is True
    assert out["tool"] == "build_comsol_model"
    assert any("recreate_and_solve" in a or "--gds" in a for a in out["would_run"])
    assert "comsol_health" in out


def test_sweep_dry_run_reports_plan(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_stub_length_sweep(
        reg, mph_path="dummy.mph", stub_lengths_um=[300, 400],
        freq_ghz=[1, 2, 3], dry_run=True)
    assert out["dry_run"] is True
    assert "300" in ",".join(out["would_run"])

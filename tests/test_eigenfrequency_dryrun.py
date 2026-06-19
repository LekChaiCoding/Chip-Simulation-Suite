"""Eigenfrequency study dry-run tests.

Verifies the wrapping is sound: dry_run returns a well-formed plan dict,
input validation fires correctly, and no COMSOL connection is required.
All three tests must pass as part of the standard ``pytest`` suite.
"""

from __future__ import annotations

from comsol_suite.jobs import JobRegistry
from comsol_suite.tools import comsol


def test_eigenfrequency_dry_run_reports_plan(tmp_path):
    """Dry-run returns a complete plan dict with all required keys."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_eigenfrequency_study(
        reg, mph_path="dummy.mph", dry_run=True
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_eigenfrequency_study"
    # Script name must appear in the would_run argv.
    assert any("eigenfrequency_analysis" in str(a) for a in out["would_run"]), \
        f"eigenfrequency_analysis not in would_run: {out['would_run']}"
    # Patch plan must include the BASE_MPH redirect.
    patch_keys = " ".join(out["patches_applied"].keys())
    assert "BASE_MPH" in patch_keys, \
        f"BASE_MPH not in patches_applied keys: {patch_keys}"
    # At least one .mph file should be in the save plan.
    assert any(".mph" in p for p in out["mph_files_would_save"]), \
        f"No .mph in mph_files_would_save: {out['mph_files_would_save']}"


def test_eigenfrequency_dry_run_validates_n_modes(tmp_path):
    """n_modes outside [1, 20] must fail fast with an informative error."""
    reg = JobRegistry(tmp_path / "runs")

    out_zero = comsol.run_eigenfrequency_study(
        reg, mph_path="dummy.mph", n_modes=0, dry_run=True
    )
    assert out_zero["ok"] is False, "n_modes=0 should fail"
    assert "n_modes" in out_zero["error"], \
        f"'n_modes' not in error: {out_zero['error']}"

    out_big = comsol.run_eigenfrequency_study(
        reg, mph_path="dummy.mph", n_modes=21, dry_run=True
    )
    assert out_big["ok"] is False, "n_modes=21 should fail"
    assert "n_modes" in out_big["error"], \
        f"'n_modes' not in error: {out_big['error']}"


def test_eigenfrequency_dry_run_validates_freq_range(tmp_path):
    """freq_start_ghz ≥ freq_stop_ghz must fail with a clear error."""
    reg = JobRegistry(tmp_path / "runs")

    out = comsol.run_eigenfrequency_study(
        reg, mph_path="dummy.mph",
        freq_start_ghz=10.0, freq_stop_ghz=5.0,
        dry_run=True,
    )
    assert out["ok"] is False, "inverted freq range should fail"
    assert "freq_start" in out["error"], \
        f"'freq_start' not in error: {out['error']}"

    # Equal bounds must also fail.
    out_eq = comsol.run_eigenfrequency_study(
        reg, mph_path="dummy.mph",
        freq_start_ghz=5.0, freq_stop_ghz=5.0,
        dry_run=True,
    )
    assert out_eq["ok"] is False, "equal freq bounds should fail"

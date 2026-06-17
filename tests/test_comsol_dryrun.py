"""COMSOL stage tests — exercise the dry-run / health-check path only.

Actually solving requires a live COMSOL connection (out of scope here), so these
tests confirm the *wrapping* is sound: health checks run, and each tool's
dry-run returns a well-formed plan — including the new geom_params /
material_params injection, mph_files_would_save list, and patches_applied dict.
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
    # Should show the real recreate_and_solve.py script path
    assert any("recreate_and_solve" in a for a in out["would_run"])
    assert "comsol_health" in out


def test_build_dry_run_includes_geom_and_material_params(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.build_comsol_model(
        reg,
        gds_path="dummy.gds",
        geom_params={"add_stub_length": "350[um]", "metal_t": "200[nm]"},
        material_params={"sub_eps_r": "11.7", "sub_loss_tan": "1e-6"},
        comsol_cores=8,
        build_only=True,
        dry_run=True,
    )
    assert out["dry_run"] is True
    # Params must appear in the patches_applied report.
    patches = out["patches_applied"]
    assert any("350[um]" in str(v) for v in patches.values()), \
        f"geom_params missing from patches_applied: {patches}"
    assert any("11.7" in str(v) for v in patches.values()), \
        f"material_params missing from patches_applied: {patches}"
    # build_only → only model_built.mph, no model_solved.mph
    mph = out["mph_files_would_save"]
    assert any("model_built.mph" in p for p in mph)
    assert not any("model_solved.mph" in p for p in mph)


def test_build_dry_run_shows_mph_save_plan(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.build_comsol_model(reg, gds_path="dummy.gds", dry_run=True)
    assert "mph_files_would_save" in out
    assert len(out["mph_files_would_save"]) >= 1
    assert all(p.endswith(".mph") for p in out["mph_files_would_save"])


def test_sweep_dry_run_reports_plan(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_stub_length_sweep(
        reg, mph_path="dummy.mph", stub_lengths_um=[300, 400],
        freq_ghz=[1, 2, 3], dry_run=True)
    assert out["dry_run"] is True
    # Stub values appear in the argv
    assert "300" in " ".join(out["would_run"])
    # Patch plan shows BASE_MPH redirect
    assert "BASE_MPH" in " ".join(out["patches_applied"].keys())
    # Per-stub MPH files planned
    mph = out["mph_files_would_save"]
    assert any("300" in p and ".mph" in p for p in mph)


def test_sweep_dry_run_with_port_and_resume(tmp_path):
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_stub_length_sweep(
        reg, mph_path="dummy.mph", stub_lengths_um=[340],
        freq_ghz=[5, 10], port="1", resume=True, dry_run=True)
    assert out["dry_run"] is True
    argv_str = " ".join(out["would_run"])
    assert "--port" in argv_str and "1" in argv_str
    assert "--resume" in argv_str


def test_custom_comsol_build_dry_run(tmp_path):
    """run_custom_comsol_build dry-run shows patch plan even for a fake script."""
    import comsol_suite.config as cfg_mod
    cfg = cfg_mod.load_config()
    # Use any real file as a stand-in; content doesn't matter for dry-run.
    dummy = cfg.script("comsol_build")
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_custom_comsol_build(
        reg,
        build_script=str(dummy),
        geom_params={"pad_width": "200[um]", "sub_t": "525[um]"},
        material_params={"sub_eps_r": "11.7"},
        comsol_cores=8,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_custom_comsol_build"
    patches = out["patches_applied"]
    assert any("200[um]" in str(v) for v in patches.values()), \
        f"geom_params not in patches: {patches}"
    assert any("11.7" in str(v) for v in patches.values()), \
        f"material_params not in patches: {patches}"
    assert "comsol_health" in out

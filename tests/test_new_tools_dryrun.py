"""Dry-run / unit tests for tools added in the device-agnostic refactor.

Covers:
  - run_geometry_param_sweep  (eigenfrequency and frequency_domain modes)
  - run_decay_rate_sweep
  - run_coupling_extraction   (pure-Python, no COMSOL)
  - run_custom_comsol_build   for resonator and D0 build scripts
  - resonator CAD generation  (cad_resonator_halfwave.py)

None of these tests require a live COMSOL connection.
"""

from __future__ import annotations

import csv
import os

import pytest

from comsol_suite.config import load_config
from comsol_suite.jobs import JobRegistry
from comsol_suite.tools import comsol
from comsol_suite.tools.cad import run_custom_cad


# ─────────────────────────────────────────────────────────────────────────────
# run_geometry_param_sweep  — dry-run
# ─────────────────────────────────────────────────────────────────────────────

def test_geom_sweep_eigenfreq_dry_run_reports_plan(tmp_path):
    """Eigenfrequency sweep dry-run returns a well-formed plan dict."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg,
        mph_path="dummy.mph",
        param_name="l_slider_single",
        param_values=[200.0, 250.0, 300.0],
        param_unit="um",
        study_type="eigenfrequency",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_geometry_param_sweep"
    argv_str = " ".join(out["would_run"])
    assert "l_slider_single" in argv_str, f"param_name missing from argv: {argv_str}"
    assert "250" in argv_str, f"param value 250 missing from argv: {argv_str}"
    assert "eigenfrequency" in argv_str, f"study_type missing: {argv_str}"
    # Three MPH files planned (one per sweep value)
    assert len(out["mph_files_would_save"]) == 3


def test_geom_sweep_freq_domain_dry_run(tmp_path):
    """Frequency-domain sweep dry-run includes freq_points in argv."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg,
        mph_path="dummy.mph",
        param_name="stub_length",
        param_values=[300.0, 400.0],
        param_unit="um",
        study_type="frequency_domain",
        freq_points_ghz=[1.0, 5.0, 10.0],
        dry_run=True,
    )
    assert out["dry_run"] is True
    argv_str = " ".join(out["would_run"])
    assert "frequency_domain" in argv_str
    assert "1.0" in argv_str and "10.0" in argv_str


def test_geom_sweep_rejects_empty_values(tmp_path):
    """Empty param_values must fail immediately without COMSOL."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg, mph_path="x.mph", param_name="p", param_values=[], dry_run=True
    )
    assert out.get("ok") is False
    assert "param_values" in out["error"]


def test_geom_sweep_rejects_invalid_study_type(tmp_path):
    """Unknown study_type must fail with a clear error."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg, mph_path="x.mph", param_name="p", param_values=[1.0],
        study_type="harmonic", dry_run=True
    )
    assert out.get("ok") is False
    assert "study_type" in out["error"]


def test_geom_sweep_rejects_freq_domain_without_freq_points(tmp_path):
    """frequency_domain without freq_points_ghz must fail."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg, mph_path="x.mph", param_name="p", param_values=[1.0],
        study_type="frequency_domain", freq_points_ghz=None, dry_run=True
    )
    assert out.get("ok") is False


def test_geom_sweep_patch_plan_includes_base_mph(tmp_path):
    """Dry-run patch plan must include BASE_MPH substitution."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_geometry_param_sweep(
        reg, mph_path="my_model.mph", param_name="gap",
        param_values=[10.0], dry_run=True
    )
    patches = out["patches_applied"]
    assert any("BASE_MPH" in k for k in patches), \
        f"BASE_MPH not in patches: {patches}"


# ─────────────────────────────────────────────────────────────────────────────
# run_decay_rate_sweep  — dry-run
# ─────────────────────────────────────────────────────────────────────────────

def test_decay_sweep_dry_run_reports_plan(tmp_path):
    """Decay-rate sweep dry-run returns a complete plan dict."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_decay_rate_sweep(
        reg,
        mph_path="qubit_model.mph",
        sweep_param="LJJ",
        sweep_values=[10.0, 11.0, 12.0],
        sweep_unit="nH",
        junction_selection="jj_node",
        port_selection="readout_port",
        shunt_capacitance_F=90e-15,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_decay_rate_sweep"
    argv_str = " ".join(out["would_run"])
    assert "LJJ" in argv_str, f"sweep_param missing: {argv_str}"
    assert "jj_node" in argv_str
    assert "readout_port" in argv_str
    # Three MPH files (one per sweep value)
    assert len(out["mph_files_would_save"]) == 3


def test_decay_sweep_dry_run_with_fixed_freq(tmp_path):
    """Fixed drive frequency appears in the argv."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_decay_rate_sweep(
        reg,
        mph_path="q.mph",
        sweep_param="gap_um",
        sweep_values=[5.0],
        sweep_unit="um",
        junction_selection="junc",
        port_selection="port1",
        shunt_capacitance_F=100e-15,
        freq_ghz=6.5,
        dry_run=True,
    )
    assert "6.5" in " ".join(out["would_run"])


def test_decay_sweep_rejects_empty_values(tmp_path):
    """Empty sweep_values must fail immediately."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_decay_rate_sweep(
        reg, mph_path="x.mph", sweep_param="L",
        sweep_values=[], sweep_unit="H",
        junction_selection="j", port_selection="p",
        shunt_capacitance_F=1e-13, dry_run=True,
    )
    assert out.get("ok") is False


# ─────────────────────────────────────────────────────────────────────────────
# run_coupling_extraction  — pure-Python, no COMSOL
# ─────────────────────────────────────────────────────────────────────────────

def _write_mock_eigenfreq_csv(path, mode1_col, mode2_col):
    """Write a two-row mock eigenfrequency CSV with synthetic field data."""
    rows = [
        # resonator-like mode: higher path integral along mode1_col
        {"freq_ghz": "6.5", "We_J": "1.02e-22", "Wm_J": "1.00e-22",
         mode1_col: "50.0", mode2_col: "5.0"},
        # qubit-like mode: higher path integral along mode2_col
        {"freq_ghz": "5.8", "We_J": "1.01e-22", "Wm_J": "1.00e-22",
         mode1_col: "3.0", mode2_col: "60.0"},
    ]
    fieldnames = ["freq_ghz", "We_J", "Wm_J", mode1_col, mode2_col]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def test_coupling_extraction_succeeds_with_well_formed_csv(tmp_path):
    """run_coupling_extraction returns g_Hz > 0 for a synthetic two-mode CSV."""
    csv_path = str(tmp_path / "modes.csv")
    _write_mock_eigenfreq_csv(csv_path, "path_res", "path_qubit")

    result = comsol.run_coupling_extraction(
        eigenfreq_csv=csv_path,
        mode1_path_col="path_res",
        mode2_path_col="path_qubit",
        lumped_inductance_H=11.2e-9,
    )
    assert result.get("ok") is True, f"Extraction failed: {result.get('error')}"
    assert result["g_Hz"] != 0, "g_Hz should be non-zero"
    assert result["g_MHz"] == pytest.approx(result["g_Hz"] / 1e6)
    assert "f_mode1_Hz" in result and "f_mode2_Hz" in result
    assert result["participation_ratio"] > 0


def test_coupling_extraction_fails_on_missing_csv(tmp_path):
    """Nonexistent CSV must return ok=False with a clear error."""
    result = comsol.run_coupling_extraction(
        eigenfreq_csv=str(tmp_path / "nonexistent.csv"),
        mode1_path_col="path_a",
        mode2_path_col="path_b",
        lumped_inductance_H=11e-9,
    )
    assert result.get("ok") is False
    assert "not found" in result["error"]


def test_coupling_extraction_fails_on_missing_columns(tmp_path):
    """CSV missing required columns must return ok=False."""
    bad_csv = str(tmp_path / "bad.csv")
    with open(bad_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["freq_ghz", "We_J"])
        w.writeheader()
        w.writerow({"freq_ghz": "6.0", "We_J": "1e-22"})

    result = comsol.run_coupling_extraction(
        eigenfreq_csv=bad_csv,
        mode1_path_col="path_res",
        mode2_path_col="path_qubit",
        lumped_inductance_H=11e-9,
    )
    assert result.get("ok") is False
    assert "missing columns" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# run_custom_comsol_build  — dry-run with resonator & D0 build scripts
# (validates that the fixed single-line PARAM_OVERRIDES can be patched)
# ─────────────────────────────────────────────────────────────────────────────

def _suite_script(rel_path):
    cfg = load_config()
    return str(cfg.chip_sim_root / "COMSOL Simulation Suite" / rel_path)


def test_custom_build_resonator_dry_run_patches_correctly(tmp_path):
    """Dry-run for build_resonator_halfwave.py succeeds and shows the patch plan."""
    build_script = _suite_script("scripts/build_resonator_halfwave.py")
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_custom_comsol_build(
        reg,
        build_script=build_script,
        geom_params={"GDS_PATH": "/path/to/resonator.gds"},
        material_params={"sub_eps_r": "11.5"},
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_custom_comsol_build"
    patches = out["patches_applied"]
    assert any("/path/to/resonator.gds" in str(v) for v in patches.values()), \
        f"GDS_PATH not in patch plan: {patches}"
    assert any("11.5" in str(v) for v in patches.values()), \
        f"sub_eps_r not in patch plan: {patches}"


def test_custom_build_d0_dry_run_patches_correctly(tmp_path):
    """Dry-run for build_D0_capext.py succeeds and shows the patch plan."""
    build_script = _suite_script("scripts/build_D0_capext.py")
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_custom_comsol_build(
        reg,
        build_script=build_script,
        geom_params={"GDS_PATH": "/path/to/d0_sim.gds", "LJJ_nH": "11.2"},
        material_params={"sub_eps_r": "11.45"},
        dry_run=True,
    )
    assert out["dry_run"] is True
    patches = out["patches_applied"]
    assert any("d0_sim.gds" in str(v) for v in patches.values()), \
        f"GDS_PATH not in D0 patch plan: {patches}"
    assert any("11.2" in str(v) for v in patches.values()), \
        f"LJJ_nH not in D0 patch plan: {patches}"


def test_custom_build_script_not_found(tmp_path):
    """Passing a nonexistent build script returns ok=False immediately."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_custom_comsol_build(
        reg,
        build_script="/nonexistent/build_script.py",
        dry_run=True,
    )
    assert out.get("ok") is False
    assert "not found" in out["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Resonator CAD generation
# ─────────────────────────────────────────────────────────────────────────────

def test_resonator_cad_produces_gds(tmp_path):
    """cad_resonator_halfwave.py generates a valid GDS via run_custom_cad."""
    cad_script = _suite_script("scripts/cad_resonator_halfwave.py")
    result = run_custom_cad(
        cad_script=cad_script,
        output_dir=str(tmp_path / "resonator_cad"),
        out_gds_var="OUT_GDS",
        out_png_var="OUT_PNG",
        gds_filename="resonator_halfwave.gds",
        debug=True,
    )
    assert result["ok"], f"Resonator CAD failed:\n{result.get('log_tail')}"
    gds_path = result["gds_path"]
    assert gds_path is not None and gds_path.endswith(".gds")
    assert os.path.isfile(gds_path), f"GDS file not found: {gds_path}"
    # Sanity check: GDS is non-trivially large (must have real geometry)
    size_bytes = os.path.getsize(gds_path)
    assert size_bytes > 500, f"GDS suspiciously small: {size_bytes} bytes"


def test_resonator_cad_has_expected_layers(tmp_path):
    """Generated resonator GDS must contain layer 0 (metal), 1 (gap), 2 (ports)."""
    import gdstk
    cad_script = _suite_script("scripts/cad_resonator_halfwave.py")
    result = run_custom_cad(
        cad_script=cad_script,
        output_dir=str(tmp_path / "resonator_layers"),
        out_gds_var="OUT_GDS",
        out_png_var=None,
        gds_filename="resonator.gds",
    )
    assert result["ok"], result.get("log_tail")
    lib = gdstk.read_gds(result["gds_path"])
    cells = {c.name: c for c in lib.cells}
    assert cells, "GDS has no cells"
    top = list(cells.values())[0]
    layers_used = {p.layer for p in top.polygons}
    assert 0 in layers_used, f"Layer 0 (metal) missing: layers={layers_used}"
    assert 1 in layers_used, f"Layer 1 (gap) missing: layers={layers_used}"
    assert 2 in layers_used, f"Layer 2 (ports) missing: layers={layers_used}"


def test_resonator_cad_has_two_port_markers(tmp_path):
    """Resonator GDS must have exactly 2 port marker rectangles on layer 2."""
    import gdstk
    cad_script = _suite_script("scripts/cad_resonator_halfwave.py")
    result = run_custom_cad(
        cad_script=cad_script,
        output_dir=str(tmp_path / "resonator_ports"),
        out_gds_var="OUT_GDS",
        out_png_var=None,
        gds_filename="resonator.gds",
    )
    assert result["ok"], result.get("log_tail")
    lib = gdstk.read_gds(result["gds_path"])
    top = list(lib.cells)[0]
    port_polys = [p for p in top.polygons if p.layer == 2]
    assert len(port_polys) == 2, \
        f"Expected 2 port markers on layer 2, got {len(port_polys)}"


# ─────────────────────────────────────────────────────────────────────────────
# run_parameter_inversion  — dry-run, validation, and pure-Python inversion
# ─────────────────────────────────────────────────────────────────────────────

def test_param_inversion_dry_run_returns_plan(tmp_path):
    """Dry-run returns sweep plan + inversion sub-dict."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_parameter_inversion(
        reg,
        mph_path="qubit_model.mph",
        param_name="d_q",
        param_range=[150.0, 350.0],
        target_value=6.5,
        n_sweep_points=7,
        param_unit="um",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "run_parameter_inversion"
    assert "inversion" in out
    inv = out["inversion"]
    assert inv["target_value_ghz"] == 6.5
    assert len(inv["param_values"]) == 7
    assert inv["poly_degree"] == 3
    assert inv["post_physics"] is None


def test_param_inversion_dry_run_linspace(tmp_path):
    """Sweep values are evenly spaced within param_range, endpoints included."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_parameter_inversion(
        reg,
        mph_path="model.mph",
        param_name="l_slider",
        param_range=[200.0, 400.0],
        target_value=5.5,
        n_sweep_points=5,
        dry_run=True,
    )
    vals = out["inversion"]["param_values"]
    assert len(vals) == 5
    assert abs(vals[0] - 200.0) < 0.01
    assert abs(vals[-1] - 400.0) < 0.01
    diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    assert all(abs(d - diffs[0]) < 0.01 for d in diffs), \
        f"Linspace not evenly spaced: {vals}"


def test_param_inversion_rejects_invalid_range(tmp_path):
    """Reversed param_range [max, min] must fail immediately."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_parameter_inversion(
        reg,
        mph_path="x.mph",
        param_name="d_q",
        param_range=[350.0, 150.0],   # reversed
        target_value=6.5,
        dry_run=True,
    )
    assert out.get("ok") is False
    assert "param_range" in out["error"]


def test_param_inversion_rejects_transmon_without_inductance(tmp_path):
    """post_physics='transmon' without lumped_inductance_H must fail."""
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.run_parameter_inversion(
        reg,
        mph_path="x.mph",
        param_name="d_q",
        param_range=[150.0, 350.0],
        target_value=6.5,
        post_physics="transmon",
        lumped_inductance_H=None,
        dry_run=True,
    )
    assert out.get("ok") is False
    assert "lumped_inductance_H" in out["error"]


def test_param_inversion_pure_python_inversion():
    """Validates polynomial_inverse math with a synthetic linear dataset.

    No COMSOL or CSV I/O — just the inversion function itself.
    freq = 8.0 - 0.01 * d_q  →  d_q for freq=6.0 should be 200 µm.
    """
    from comsol_suite.tools.circuit_physics import polynomial_inverse

    d_q_vals  = [150.0, 200.0, 250.0, 300.0, 350.0]
    freq_vals = [8.0 - 0.01 * d for d in d_q_vals]  # 6.5, 6.0, 5.5, 5.0, 4.5

    roots = polynomial_inverse(d_q_vals, freq_vals, 6.0, degree=1)
    assert len(roots) == 1, f"Expected 1 root, got {roots}"
    assert abs(roots[0] - 200.0) < 1.0, \
        f"Root {roots[0]:.3f} should be near 200.0 µm"

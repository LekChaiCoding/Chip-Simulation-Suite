"""Dry-run / unit tests for the F1 capacitance-campaign MCP wrappers
(comsol_suite.tools.qleap_cs002). No live COMSOL connection required —
every test passes ``registry=None``, so any accidental launch would blow
up on the missing JobRegistry instead of silently running something.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comsol_suite.tools import qleap_cs002


@pytest.fixture
def patched_dirs(monkeypatch, tmp_path: Path):
    """Redirect the campaign directories into tmp_path (CCT001 test pattern)."""
    f1 = tmp_path / "F1_QubitLoop001"
    cs002 = tmp_path / "CapacitanceSimulation002"
    (f1 / "tools").mkdir(parents=True)
    (cs002 / "tools").mkdir(parents=True)
    monkeypatch.setattr(qleap_cs002, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(qleap_cs002, "_f1_dir", lambda: f1)
    monkeypatch.setattr(qleap_cs002, "_cs002_dir", lambda: cs002)
    return tmp_path


def _assert_uv_prefix(argv: list) -> None:
    """Every wrapper must build the repo-standard campaign launch pattern."""
    assert argv[:6] == ["env", "-u", "VIRTUAL_ENV", "uv", "run", "--no-project"]
    assert "python" in argv


# ── qleap_cs002_optimize ─────────────────────────────────────────────────────

def test_optimize_dry_run_builds_run_cj_loop_argv(patched_dirs):
    out = qleap_cs002.qleap_cs002_optimize(None, tile="U0_R0", dry_run=True)
    assert out["dry_run"] is True
    argv = out["would_run"]
    _assert_uv_prefix(argv)
    assert any(a.endswith("F1_QubitLoop001/tools/run_cj_loop.py") for a in argv)
    assert "--tile" in argv and "U0_R0" in argv
    assert "--rounds" in argv
    # No solve requested: no --solve flag and no mph extra (numpy only).
    assert "--solve" not in argv and "mph" not in argv


def test_optimize_solve_mode_adds_solve_flag_and_mph_extra(patched_dirs):
    out = qleap_cs002.qleap_cs002_optimize(None, tile="U1_R1", solve=True,
                                           dry_run=True)
    argv = out["would_run"]
    assert "--solve" in argv
    assert "mph" in argv  # --with mph for the COMSOL DO stages


def test_optimize_offline_modes_and_j_sim(patched_dirs, tmp_path):
    out = qleap_cs002.qleap_cs002_optimize(None, tile="U0_R0",
                                           print_plan=True, dry_run=True)
    assert "--print-plan" in out["would_run"]

    j_sim = tmp_path / "extraction.json"
    out = qleap_cs002.qleap_cs002_optimize(None, tile="U0_R0",
                                           j_sim_path=str(j_sim), dry_run=True)
    argv = out["would_run"]
    assert "--j-sim" in argv and str(j_sim) in argv

    # replay may span all tiles: tile is optional there (and only there).
    out = qleap_cs002.qleap_cs002_optimize(None, replay=True, dry_run=True)
    argv = out["would_run"]
    assert "--replay" in argv and "--tile" not in argv


def test_optimize_validation_errors(patched_dirs):
    with pytest.raises(ValueError, match="tile"):
        qleap_cs002.qleap_cs002_optimize(None, dry_run=True)
    with pytest.raises(ValueError, match="unknown tile"):
        qleap_cs002.qleap_cs002_optimize(None, tile="Z9_R9", dry_run=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        qleap_cs002.qleap_cs002_optimize(None, tile="U0_R0", solve=True,
                                         print_plan=True, dry_run=True)


# ── qleap_cs002_direct_probe ─────────────────────────────────────────────────

def test_direct_probe_dry_run_builds_argv(patched_dirs):
    params = [{"qubit_pad_r": 108.0, "qubit_readout_pad_angle": 59.2}]
    out = qleap_cs002.qleap_cs002_direct_probe(
        None, tile="U0_R1", letter="C", run_id="probe_20260727",
        params=params, dry_run=True)
    assert out["dry_run"] is True
    argv = out["would_run"]
    _assert_uv_prefix(argv)
    assert any(a.endswith("CapacitanceSimulation002/tools/direct_probe.py")
               for a in argv)
    # The script's flag is --unit (not --tile), plus mph for the solves.
    assert "--unit" in argv and "U0_R1" in argv
    assert "--letter" in argv and "C" in argv
    assert "--run-id" in argv and "probe_20260727" in argv
    assert "mph" in argv
    payload = argv[argv.index("--params-json") + 1]
    assert json.loads(payload) == params


def test_direct_probe_wraps_single_dict_and_validates(patched_dirs):
    out = qleap_cs002.qleap_cs002_direct_probe(
        None, tile="U0_R0", letter="A", run_id="r1",
        params={"qubit_pad_r": 100.0}, dry_run=True)
    payload = out["would_run"][out["would_run"].index("--params-json") + 1]
    assert json.loads(payload) == [{"qubit_pad_r": 100.0}]

    with pytest.raises(ValueError, match="at least one"):
        qleap_cs002.qleap_cs002_direct_probe(
            None, tile="U0_R0", letter="A", run_id="r1", params=[],
            dry_run=True)
    with pytest.raises(ValueError, match="run_id"):
        qleap_cs002.qleap_cs002_direct_probe(
            None, tile="U0_R0", letter="A", run_id="", params=[{"x": 1.0}],
            dry_run=True)
    with pytest.raises(ValueError, match="letter"):
        qleap_cs002.qleap_cs002_direct_probe(
            None, tile="U0_R0", letter="Z", run_id="r1", params=[{"x": 1.0}],
            dry_run=True)


# ── qleap_cs002_coupling_correct ─────────────────────────────────────────────

def test_coupling_correct_requires_j_sim_path(patched_dirs):
    out = qleap_cs002.qleap_cs002_coupling_correct(None, dry_run=True)
    assert out.get("ok") is False
    assert "j_sim_path" in out["error"]


def test_coupling_correct_dry_run_builds_argv(patched_dirs, tmp_path):
    j_sim = tmp_path / "round1" / "extraction.json"
    out = qleap_cs002.qleap_cs002_coupling_correct(
        None, tile="U2_R0", j_sim_path=str(j_sim), brickwall=True,
        out_path=str(tmp_path / "round2" / "correction.json"), dry_run=True)
    assert out["dry_run"] is True
    argv = out["would_run"]
    _assert_uv_prefix(argv)
    assert any(a.endswith("F1_QubitLoop001/tools/coupling_correct.py")
               for a in argv)
    assert "--j-sim" in argv and str(j_sim) in argv
    assert "--brickwall" in argv
    assert "--tile" in argv and "U2_R0" in argv
    assert "--out" in argv


# ── qleap_cs002_finalize ─────────────────────────────────────────────────────

def test_finalize_dry_run_builds_argv(patched_dirs, tmp_path):
    record = tmp_path / "Data" / "baseline_20260727" / "baseline.json"
    out = qleap_cs002.qleap_cs002_finalize(
        None, tile="U0_R1", letter="B", record_path=str(record),
        label="baseline_20260727", dry_run=True)
    assert out["dry_run"] is True
    argv = out["would_run"]
    _assert_uv_prefix(argv)
    assert any(a.endswith(
        "CapacitanceSimulation002/tools/finalize_optimized_model.py")
        for a in argv)
    assert "--unit" in argv and "U0_R1" in argv
    assert "--letter" in argv and "B" in argv
    assert "--record" in argv and str(record) in argv
    assert "--label" in argv and "baseline_20260727" in argv
    assert "--allow-out-of-tolerance" not in argv
    # Promotion target is reported so the HITL reviewer sees the blast radius.
    assert any("Optimized Models" in o for o in out["outputs_would_write"])


def test_finalize_flags_and_validation(patched_dirs):
    out = qleap_cs002.qleap_cs002_finalize(
        None, tile="U0_R0", letter="A", record_path="rec.json",
        label="l1", allow_out_of_tolerance=True, dry_run=True)
    assert "--allow-out-of-tolerance" in out["would_run"]

    with pytest.raises(ValueError, match="label"):
        qleap_cs002.qleap_cs002_finalize(
            None, tile="U0_R0", letter="A", record_path="rec.json",
            label="", dry_run=True)
    with pytest.raises(ValueError, match="unknown tile"):
        qleap_cs002.qleap_cs002_finalize(
            None, tile="Q9_R9", letter="A", record_path="rec.json",
            label="l1", dry_run=True)

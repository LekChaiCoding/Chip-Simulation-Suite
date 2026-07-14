"""Dry-run / unit tests for the NT002 campaign-driver MCP wrappers
(comsol_suite.tools.qleap_nt2).

No live COMSOL connection or real campaign scripts required: background-job
tools are tested via dry_run=True (never subprocesses); the always-execute
foreground gates are tested by monkeypatching ``run_command`` with a canned
:class:`CommandResult` so argv-building and report-parsing are exercised
without spawning a real process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comsol_suite.jobs import JobRegistry
from comsol_suite.runner import CommandResult
from comsol_suite.tools import qleap_nt2


def _patch_dirs(monkeypatch, tmp_path: Path) -> Path:
    nt2 = tmp_path / "NotchTuning002"
    nt1 = tmp_path / "NotchTuning001"
    nt2.mkdir()
    nt1.mkdir()
    monkeypatch.setattr(qleap_nt2, "_nt2_dir", lambda: nt2)
    monkeypatch.setattr(qleap_nt2, "_nt1_dir", lambda: nt1)
    monkeypatch.setattr(qleap_nt2, "_python_bin", lambda: "/venv/python")
    return nt2


def _fake_run_command(returncode: int, stdout_json_text: str):
    """Return a stand-in for runner.run_command that writes ``stdout_json_text``
    to the log and reports ``returncode``, without spawning any process."""
    def _run(argv, log_path, *, cwd=None, env=None, timeout_s=None, debug=False):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(stdout_json_text, encoding="utf-8")
        return CommandResult(returncode=returncode, log_path=log_path,
                             duration_s=0.01, timed_out=False, argv=list(argv))
    return _run


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_linear_retune
# ─────────────────────────────────────────────────────────────────────────────
def test_linear_retune_dry_run_builds_argv(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_nt2.qleap_nt2_linear_retune(
        registry=None, tile="U0_R0", letter="B", dry_run=True)
    assert out["dry_run"] is True
    assert "--tile" in out["would_run"] and "U0_R0" in out["would_run"]
    assert "--letter" in out["would_run"] and "B" in out["would_run"]


def test_linear_retune_rejects_unknown_tile(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="tile"):
        qleap_nt2.qleap_nt2_linear_retune(
            registry=None, tile="Z9_R9", letter="A", dry_run=True)


def test_linear_retune_rejects_unknown_letter(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="letter"):
        qleap_nt2.qleap_nt2_linear_retune(
            registry=None, tile="U0_R0", letter="Z", dry_run=True)


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_purcell_check
# ─────────────────────────────────────────────────────────────────────────────
def test_purcell_check_missing_record_returns_error_without_subprocess(
    monkeypatch, tmp_path
):
    nt2 = _patch_dirs(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("run_command should not be called when a "
                             "prerequisite record is missing")
    monkeypatch.setattr(qleap_nt2, "run_command", _boom)

    out = qleap_nt2.qleap_nt2_purcell_check(tile="U0_R0", letters="AB")
    assert out.get("ok") is False
    assert "U0_R0_A.LINEAR.json" in out["error"]


def test_purcell_check_rejects_bad_record_suffix(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="record_suffix"):
        qleap_nt2.qleap_nt2_purcell_check(tile="U0_R0", record_suffix="BOGUS")


def test_purcell_check_csv_override_requires_one_letter(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="csv_override"):
        qleap_nt2.qleap_nt2_purcell_check(
            tile="U0_R0", letters="AB", csv_override="/some.csv")


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_ratio_retune
# ─────────────────────────────────────────────────────────────────────────────
def test_ratio_retune_dry_run_builds_argv(monkeypatch, tmp_path):
    nt2 = _patch_dirs(monkeypatch, tmp_path)
    (nt2 / "campaign_config_nt002d.json").write_text(
        json.dumps({"strategy": {"D": {"routes": []}}}))
    out = qleap_nt2.qleap_nt2_ratio_retune(
        registry=None, tile="U0_R0", letter="D", dry_run=True)
    assert out["dry_run"] is True
    assert "--letter" in out["would_run"] and "D" in out["would_run"]


def test_ratio_retune_rejects_letter_without_strategy(monkeypatch, tmp_path):
    nt2 = _patch_dirs(monkeypatch, tmp_path)
    (nt2 / "campaign_config_nt002d.json").write_text(
        json.dumps({"strategy": {"D": {"routes": []}}}))
    out = qleap_nt2.qleap_nt2_ratio_retune(
        registry=None, tile="U0_R0", letter="A", dry_run=True)
    assert out.get("ok") is False
    assert "strategy" in out["error"]


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_ratio_gap_check / qleap_nt2_ratio_geometry_gate
# ─────────────────────────────────────────────────────────────────────────────
def test_ratio_gap_check_builds_set_overrides_and_ignores_nonzero_ok(
    monkeypatch, tmp_path
):
    _patch_dirs(monkeypatch, tmp_path)
    report = {"tile": "U0_R0", "tag": "cand", "verdict": "PASS"}
    monkeypatch.setattr(qleap_nt2, "run_command",
                        _fake_run_command(0, json.dumps(report, indent=1)))
    out = qleap_nt2.qleap_nt2_ratio_gap_check(
        tile="U0_R0", letter_model="D",
        param_overrides={"g_filter4_n_meander_curve": "3"}, tag="cand")
    assert out["ok"] is True
    assert out["report"]["verdict"] == "PASS"


def test_ratio_gap_check_rejects_unsafe_tag(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="tag"):
        qleap_nt2.qleap_nt2_ratio_gap_check(
            tile="U0_R0", letter_model="D", param_overrides={}, tag="../x")


def test_ratio_geometry_gate_rc2_is_ok_with_fail_verdict(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    report = {"gate": "constant_length_ratio_geometry_v1", "verdict": "FAIL"}
    monkeypatch.setattr(qleap_nt2, "run_command",
                        _fake_run_command(2, json.dumps(report, indent=2)))
    out = qleap_nt2.qleap_nt2_ratio_geometry_gate(
        tile="U0_R0", letter_model="D",
        param_overrides={"g_filter4_l_end": "600[um]"}, tag="cand")
    assert out["ok"] is True, "rc=2 is a legitimate FAIL verdict, not a crash"
    assert out["returncode"] == 2
    assert out["verdict"] == "FAIL"


def test_ratio_geometry_gate_other_rc_is_not_ok(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(qleap_nt2, "run_command", _fake_run_command(1, ""))
    out = qleap_nt2.qleap_nt2_ratio_geometry_gate(
        tile="U0_R0", letter_model="D", param_overrides={"k": "1"}, tag="cand")
    assert out["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_run_ratio_trade_probe
# ─────────────────────────────────────────────────────────────────────────────
def test_ratio_trade_probe_requires_overrides(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="override"):
        qleap_nt2.qleap_nt2_run_ratio_trade_probe(
            registry=None, tile="U0_R0", letter="D", tag="cand",
            center_ghz=4.1, param_overrides={}, dry_run=True)


def test_ratio_trade_probe_save_model_must_stay_inside_nt2(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="NotchTuning002"):
        qleap_nt2.qleap_nt2_run_ratio_trade_probe(
            registry=None, tile="U0_R0", letter="D", tag="cand",
            center_ghz=4.1, param_overrides={"k": "1[um]"},
            save_model="/tmp/escape.mph", dry_run=True)


def test_ratio_trade_probe_dry_run_builds_argv(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_nt2.qleap_nt2_run_ratio_trade_probe(
        registry=None, tile="U0_R0", letter="D", tag="cand",
        center_ghz=4.1, param_overrides={"g_filter4_l_end": "600[um]"},
        dry_run=True)
    assert out["dry_run"] is True
    assert "g_filter4_l_end=600[um]" in out["would_run"]


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_build_merged_model / qleap_nt2_verify_merged_notches
# ─────────────────────────────────────────────────────────────────────────────
def test_build_merged_model_missing_source_returns_error(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_nt2.qleap_nt2_build_merged_model(
        registry=None, tile="U0_R0", dry_run=True)
    assert out.get("ok") is False
    assert "psqtop source not found" in out["error"]


def test_build_merged_model_dry_run_builds_argv(monkeypatch, tmp_path):
    nt2 = _patch_dirs(monkeypatch, tmp_path)
    nt1 = qleap_nt2._nt1_dir()
    source = nt1 / "U0_R0" / "work" / "U0_R0_sparam_psqtop.mph"
    source.parent.mkdir(parents=True)
    source.write_text("")
    out = qleap_nt2.qleap_nt2_build_merged_model(
        registry=None, tile="U0_R0", with_notch_finals=True, dry_run=True)
    assert out["dry_run"] is True
    assert "--with-notch-finals" in out["would_run"]


def test_build_merged_model_output_path_must_stay_inside_nt2(monkeypatch, tmp_path):
    nt2 = _patch_dirs(monkeypatch, tmp_path)
    nt1 = qleap_nt2._nt1_dir()
    source = nt1 / "U0_R0" / "work" / "U0_R0_sparam_psqtop.mph"
    source.parent.mkdir(parents=True)
    source.write_text("")
    with pytest.raises(ValueError, match="NotchTuning002"):
        qleap_nt2.qleap_nt2_build_merged_model(
            registry=None, tile="U0_R0", output_path="/tmp/escape.mph",
            dry_run=True)


def test_verify_merged_notches_missing_model_returns_error(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_nt2.qleap_nt2_verify_merged_notches(
        registry=None, tile="U0_R0", dry_run=True)
    assert out.get("ok") is False
    assert "merged model missing" in out["error"]


# ─────────────────────────────────────────────────────────────────────────────
# qleap_nt2_publish_optimized
# ─────────────────────────────────────────────────────────────────────────────
def test_publish_optimized_missing_prereqs_returns_error_without_subprocess(
    monkeypatch, tmp_path
):
    _patch_dirs(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("run_command should not be called when "
                             "prerequisites are missing")
    monkeypatch.setattr(qleap_nt2, "run_command", _boom)

    out = qleap_nt2.qleap_nt2_publish_optimized(tile="U0_R0")
    assert out.get("ok") is False
    assert "prerequisite" in out["error"]

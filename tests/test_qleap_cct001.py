"""Dry-run / unit tests for the CCT001 MCP wrappers
(comsol_suite.tools.qleap_cct001). No live COMSOL connection required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comsol_suite.tools import qleap_cct001


def _patch_dirs(monkeypatch, tmp_path: Path) -> None:
    cct = tmp_path / "CableCouplingTuning001"
    qcs = tmp_path / "QubitCableSimulation001"
    cct.mkdir()
    qcs.mkdir()
    monkeypatch.setattr(qleap_cct001, "_cct_dir", lambda: cct)
    monkeypatch.setattr(qleap_cct001, "_qcs_dir", lambda: qcs)
    monkeypatch.setattr(qleap_cct001, "_python_bin", lambda: "/venv/python")


def test_tune_width_missing_pristine_returns_error(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_cct001.qleap_cct001_tune_width(
        registry=None, tile="U0_R0", letter="A", dry_run=True)
    assert out.get("ok") is False
    assert "pristine copy missing" in out["error"]


def test_tune_width_dry_run_builds_argv_with_width_bounds(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    pristine = qleap_cct001._pristine_model("U0_R0", "A")
    pristine.parent.mkdir(parents=True)
    pristine.write_text("")

    out = qleap_cct001.qleap_cct001_tune_width(
        registry=None, tile="U0_R0", letter="A",
        width_bounds_um=[5.0, 60.0], dry_run=True)
    assert out["dry_run"] is True
    argv_str = " ".join(out["would_run"])
    assert "--width-bounds-um" in argv_str
    assert "5.0" in argv_str and "60.0" in argv_str


def test_tune_width_rejects_malformed_width_bounds(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    pristine = qleap_cct001._pristine_model("U0_R0", "A")
    pristine.parent.mkdir(parents=True)
    pristine.write_text("")

    with pytest.raises(ValueError, match="width_bounds_um"):
        qleap_cct001.qleap_cct001_tune_width(
            registry=None, tile="U0_R0", letter="A",
            width_bounds_um=[60.0, 5.0], dry_run=True)

    with pytest.raises(ValueError, match="width_bounds_um"):
        qleap_cct001.qleap_cct001_tune_width(
            registry=None, tile="U0_R0", letter="A",
            width_bounds_um=[5.0], dry_run=True)


def test_rollout_letter_missing_source_returns_error(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    out = qleap_cct001.qleap_cct001_rollout_letter(
        registry=None, tile="U0_R0", letter="A", dry_run=True)
    assert out.get("ok") is False
    assert "QCS001 source model missing" in out["error"]


def test_rollout_letter_dry_run_passes_through_force_n(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    source = qleap_cct001._qcs001_source_model("U0_R0", "A")
    source.parent.mkdir(parents=True)
    source.write_text("")

    out = qleap_cct001.qleap_cct001_rollout_letter(
        registry=None, tile="U0_R0", letter="A", force_n=3, dry_run=True)
    assert out["dry_run"] is True
    assert "--force-n" in out["would_run"] and "3" in out["would_run"]


def test_rejects_unknown_tile_and_letter(monkeypatch, tmp_path):
    _patch_dirs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="tile"):
        qleap_cct001.qleap_cct001_tune_width(
            registry=None, tile="Z9_R9", letter="A", dry_run=True)
    with pytest.raises(ValueError, match="letter"):
        qleap_cct001.qleap_cct001_tune_width(
            registry=None, tile="U0_R0", letter="Z", dry_run=True)

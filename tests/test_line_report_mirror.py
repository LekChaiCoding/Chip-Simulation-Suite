"""``qleap_line_execute`` mirrors its report to the canonical ``runs/`` tree.

Measured gap this pins (2026-08-14): the tool always names its own ``--out``,
so ``ChipReconstruction008``'s report existed ONLY under
``simulations/_factory/line_runs/`` — the canonical
``<state_root>/runs/ChipReconstruction008.json`` never did, and every reader
of the line tree (scorecard, ``line_status``, a human) saw a walk with no
report. The incident log has three separate entries about hunting for "the
MCP-redirected report path".

The unit under test is the pure mirror helper; nothing here spawns the
driver or touches COMSOL.
"""

from __future__ import annotations

import json
from pathlib import Path

from comsol_suite.tools.qleap_factory import _mirror_report_to_runs


def _report_file(tmp_path: Path, payload: dict) -> Path:
    source = tmp_path / "line_runs" / "execute_20260817-000000_1.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload), encoding="utf-8")
    return source


def test_a_named_run_lands_at_state_root_runs_run_id(tmp_path: Path) -> None:
    state_root = tmp_path / "simulations" / "_line" / "hex_low_freq_v2"
    parsed = {"state_root": str(state_root), "run_id": "ChipReconstruction008"}
    source = _report_file(tmp_path, parsed)

    mirrored = _mirror_report_to_runs(parsed, source)

    wanted = state_root / "runs" / "ChipReconstruction008.json"
    assert mirrored == str(wanted)
    assert wanted.read_bytes() == source.read_bytes(), "byte-identical mirror"
    assert not wanted.with_name(wanted.name + ".tmp").exists(), "atomic replace"


def test_a_refused_or_dry_run_mirrors_nothing(tmp_path: Path) -> None:
    """execute_line reports run_id null for those on purpose — a name is not
    evidence a run happened, so no file may wear one in runs/."""
    state_root = tmp_path / "line"
    parsed = {"state_root": str(state_root), "run_id": None}
    source = _report_file(tmp_path, parsed)

    assert _mirror_report_to_runs(parsed, source) is None
    assert not (state_root / "runs").exists()
    assert _mirror_report_to_runs(None, source) is None
    assert _mirror_report_to_runs({"run_id": "X"}, source) is None  # no root


def test_an_already_canonical_report_is_left_alone(tmp_path: Path) -> None:
    state_root = tmp_path / "line"
    canonical = state_root / "runs" / "CR001.json"
    canonical.parent.mkdir(parents=True)
    parsed = {"state_root": str(state_root), "run_id": "CR001"}
    canonical.write_text(json.dumps(parsed), encoding="utf-8")
    before = canonical.stat().st_mtime_ns

    assert _mirror_report_to_runs(parsed, canonical) == str(canonical)
    assert canonical.stat().st_mtime_ns == before, "no self-copy"


def test_a_mirror_failure_is_reported_never_raised(tmp_path: Path) -> None:
    """The primary report already exists; losing it over a mirror inverts the
    priorities, so the helper returns the failure as a string."""
    blocker = tmp_path / "state_root_is_a_file"
    blocker.write_text("not a directory", encoding="utf-8")
    parsed = {"state_root": str(blocker), "run_id": "CR002"}
    source = _report_file(tmp_path, parsed)

    outcome = _mirror_report_to_runs(parsed, source)
    assert outcome is not None and outcome.startswith("MIRROR_FAILED: ")

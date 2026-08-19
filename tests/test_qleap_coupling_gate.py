"""`qleap_coupling_gate` — the coupling gates' first agent-callable surface.

The five gates are library functions the runner NEVER dispatches, so reading one
is an operator act. `qleap_line_execute` could already spend solver time on
seventeen blocks while nothing on this surface could read a single gate, which is
why "the gates have no callable surface" sat first on the Qwen-readiness blocker
list (2026-08-12). This pins what the wrapper SENDS, what it REFUSES, and where
it reads its verdict from — not the gate runner itself, which has its own suite
under QubitDesignPipeline/NewPipeline/tests/.

`run_command` is monkeypatched throughout: these are contract tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from comsol_suite.tools import qleap_factory as qleap


class _FakeResult:
    def __init__(self, returncode: int, log_path: Path):
        self.returncode = returncode
        self.ok = returncode == 0
        self._log_path = log_path

    def log_tail(self, n: int) -> str:
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])


@pytest.fixture
def gate_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = (tmp_path / "QubitDesignPipeline" / "NewPipeline" / "tools"
              / "run_coupling_gate.py")
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in; never executed — run_command is faked\n")
    monkeypatch.setattr(qleap, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(qleap, "_factory_home", lambda: tmp_path / "_factory")
    return tmp_path


def _install_runner(monkeypatch, responder):
    calls: list[dict] = []

    def fake_run_command(argv, log_path, *, cwd=None, env=None,
                         timeout_s=None, **kwargs):
        returncode, log_text = responder(list(argv))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(log_text, encoding="utf-8")
        calls.append({"argv": [str(a) for a in argv], "timeout_s": timeout_s})
        return _FakeResult(returncode, Path(log_path))

    monkeypatch.setattr(qleap, "run_command", fake_run_command)
    return calls


# ---------------------------------------------------------------------------
# the dry run: the default, and the reason the argument exists
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_launches_nothing(gate_tree, monkeypatch):
    calls = _install_runner(monkeypatch, lambda argv: (0, ""))
    out = qleap.qleap_coupling_gate(gate="gamma", design="hex_low_freq_v2",
                                   tile="U0_R0")
    assert out["ok"] is True and out["dry_run"] is True
    assert not calls, "a dry run must not reach run_command at all"
    assert out["takes_comsol_slot"] is True, "gamma opens an mph session per unit"
    assert out["gate"] == "gamma"
    assert out["scope"] == {"design": "hex_low_freq_v2", "tile": "U0_R0",
                            "letter": None}
    argv = [str(a) for a in out["would_run"]]
    assert argv[0] == sys.executable, "this interpreter, never uv over SMB"
    assert argv[1].endswith("run_coupling_gate.py")
    assert "gamma" in argv and "--design" in argv and "--tile" in argv
    assert "--json" in argv
    assert "mph session" in out["note"]


def test_a_pure_read_says_so_in_its_dry_run(gate_tree, monkeypatch):
    """alpha, beta and epsilon take no slot, and the answer states it.

    Gating the TOOL rather than the gate keeps one name for the act — which is
    what `agent/policy.py`'s interrupt keys on — so the dry-run answer is where a
    caller learns which of the four actually contends for a licence.
    """
    _install_runner(monkeypatch, lambda argv: (0, ""))
    for name in ("alpha", "beta", "epsilon"):
        out = qleap.qleap_coupling_gate(gate=name)
        assert out["takes_comsol_slot"] is False, name
        assert "mph session" not in out["note"], name


# ---------------------------------------------------------------------------
# what it refuses, before any subprocess
# ---------------------------------------------------------------------------


def test_delta_is_refused_and_names_its_own_driver(gate_tree, monkeypatch):
    """One act, one entry point. delta's driver solves and seals three roles."""
    calls = _install_runner(monkeypatch, lambda argv: (0, ""))
    out = qleap.qleap_coupling_gate(gate="delta", dry_run=False)
    assert out["ok"] is False
    assert "measure_coupling_delta.py" in out["error"]
    assert "dialled_" in out["error"]
    assert "delta" not in out["gates_available"]
    assert not calls, "a refused gate must never reach argparse"


def test_an_unknown_gate_is_refused_with_the_list(gate_tree, monkeypatch):
    calls = _install_runner(monkeypatch, lambda argv: (0, ""))
    out = qleap.qleap_coupling_gate(gate="omega", dry_run=False)
    assert out["ok"] is False and "omega" in out["error"]
    assert out["gates_available"] == ["alpha", "beta", "epsilon", "gamma"]
    assert not calls


# ---------------------------------------------------------------------------
# the verdict comes from the report FILE (I4)
# ---------------------------------------------------------------------------


def test_the_verdict_is_read_from_the_report_file_not_the_stdout(
    gate_tree, monkeypatch, tmp_path
):
    """rc 1 means the gate reported FAIL — a measured answer, not a tool failure.

    The runner prints the report document too, and reading THAT would be reading
    the tool's narration. The wrapper takes the path off the tool's own
    `report: <path>` line and reads the file.
    """
    report = tmp_path / "reports" / "D2_coupling_gamma" / "U0_R0" / "coupling_gamma.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"pass": False, "problems": ["a real finding"],
                                  "step_id": "coupling_gamma"}), encoding="utf-8")
    log = (
        "gate gamma at U0_R0: FAIL\n"
        "  problem: a real finding\n"
        f"report: {report}\n"
        + json.dumps({"pass": True, "note": "stdout copy that must NOT be read"})
        + "\n"
    )
    calls = _install_runner(monkeypatch, lambda argv: (1, log))
    out = qleap.qleap_coupling_gate(gate="gamma", dry_run=False, tile="U0_R0")

    assert calls, "a real read must reach run_command"
    assert out["returncode"] == 1
    assert out["ok"] is False, "ok is the PROCESS's health"
    assert out["report_path"] == str(report)
    assert out["parsed"]["pass"] is False, "the FILE's verdict, not the stdout's"
    assert out["parsed"]["problems"] == ["a real finding"]
    assert out["parse_error"] is None
    assert out["log_tail"] is None, "the tail is only offered when parsing failed"


def test_a_missing_report_line_is_a_stated_refusal_never_a_clean_answer(
    gate_tree, monkeypatch
):
    _install_runner(monkeypatch, lambda argv: (0, "gate alpha at chip: PASS\n"))
    out = qleap.qleap_coupling_gate(gate="alpha", dry_run=False)
    assert out["parsed"] is None
    assert "no 'report: <path>' line" in out["parse_error"]
    assert out["log_tail"], "the log must be offered when the report cannot be found"


def test_seal_is_passed_through_for_alpha_and_the_cli_enforces_it(
    gate_tree, monkeypatch
):
    """The wrapper does not re-implement the alpha-only rule; it forwards it.

    A second copy of "seal is alpha only" here would be the declaration that goes
    stale — the CLI already refuses it for the others and says why.
    """
    calls = _install_runner(monkeypatch, lambda argv: (0, "report: /x.json\n"))
    qleap.qleap_coupling_gate(gate="alpha", dry_run=False, seal=True,
                              run_id="ChipReconstruction005")
    argv = calls[0]["argv"]
    assert "--seal" in argv
    assert "--run-id" in argv and "ChipReconstruction005" in argv
    assert calls[0]["timeout_s"] == qleap._COUPLING_GATE_TIMEOUT_S

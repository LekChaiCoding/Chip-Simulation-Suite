"""`qleap_line_status` — the NEW line's read tool, tested without a subprocess.

Three properties, each pinned because its violation was measured somewhere in
this repo rather than imagined:

* argv is `[sys.executable, script, ...]`, never `uv run` — uv's environment
  resolution over SMB added 140+ s to a 37 s script and got the factory status
  tool SIGKILLed on every call (the measurement on `_cli_argv`);
* the parse reads the COMPLETE log, never a tail — `extract_trailing_json` on
  a truncated document returns a NESTED FRAGMENT with `parse_error=None` (the
  measurement on `_read_line_report`), so a tail parse can hand the agent a
  fragment that reads as a clean answer;
* `$CHIPPY_LINE_HOME` is honoured when the operator declared it and derived
  from `simulations/_line/<design>` — the landing place `execute_line.py`
  anchors runs at — when they did not, with the design id resolved by the
  script's own `--resolve-design`, never by a second copy of the precedence.

`run_command` is monkeypatched throughout: these are contract tests for what
the wrapper SENDS and how it READS, not for line_status.py itself (which has
its own suite under QubitDesignPipeline/NewPipeline/tests/).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from comsol_suite.tools import qleap_factory as qleap


class _FakeResult:
    """The three attributes the wrapper reads off a CommandResult."""

    def __init__(self, returncode: int, log_path: Path):
        self.returncode = returncode
        self.ok = returncode == 0
        self._log_path = log_path

    def log_tail(self, n: int) -> str:
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])


@pytest.fixture
def line_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake repo root with the script present, logs redirected into tmp."""
    script = tmp_path / "QubitDesignPipeline" / "NewPipeline" / "tools" / "line_status.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in; never executed — run_command is faked\n")
    monkeypatch.setattr(qleap, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(qleap, "_factory_home", lambda: tmp_path / "_factory")
    monkeypatch.delenv("CHIPPY_LINE_HOME", raising=False)
    return tmp_path


def _install_runner(monkeypatch, responder):
    """Fake run_command: `responder(argv) -> (returncode, log_text)`."""
    calls: list[dict] = []

    def fake_run_command(argv, log_path, *, cwd=None, env=None,
                         timeout_s=None, **kwargs):
        returncode, log_text = responder(list(argv))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(log_text, encoding="utf-8")
        calls.append({"argv": [str(a) for a in argv], "env": env,
                      "timeout_s": timeout_s, "cwd": cwd})
        return _FakeResult(returncode, Path(log_path))

    monkeypatch.setattr(qleap, "run_command", fake_run_command)
    return calls


def test_argv_is_this_interpreter_never_uv(line_tree, monkeypatch):
    calls = _install_runner(monkeypatch, lambda argv: (0, '{"phases": []}\n'))
    out = qleap.qleap_line_status(what="status", design="hex_low_freq_v2")
    assert out["ok"] is True, out
    (call,) = calls
    assert call["argv"][0] == sys.executable
    assert call["argv"][1].endswith("line_status.py")
    assert "uv" not in Path(call["argv"][0]).name
    assert "--what" in call["argv"] and "status" in call["argv"]
    assert "--json" in call["argv"]  # states the stdout contract explicitly
    assert "--design" in call["argv"] and "hex_low_freq_v2" in call["argv"]


def test_an_unknown_what_is_refused_before_any_subprocess(line_tree, monkeypatch):
    calls = _install_runner(monkeypatch, lambda argv: (0, "{}"))
    out = qleap.qleap_line_status(what="everything")
    assert out["ok"] is False
    assert "everything" in out["error"] and "status" in out["error"]
    assert not calls, "a refused value must never reach argparse"


def test_a_malformed_design_never_reaches_the_filesystem(line_tree, monkeypatch):
    """Same containment rule as qleap_factory_record's scope: the id is joined
    into a path (`simulations/_line/<design>`), and Qwen has sent `"tile
    U0_R0"`-shaped segments in 2 of 3 live runs."""
    calls = _install_runner(monkeypatch, lambda argv: (0, "{}"))
    for bad in ("design hex", "../_factory", "a/b"):
        out = qleap.qleap_line_status(design=bad)
        assert out["ok"] is False, bad
        assert "not a single record-tree name" in out["error"], bad
    assert not calls


def test_declared_line_home_is_inherited_not_overridden(line_tree, monkeypatch):
    monkeypatch.setenv("CHIPPY_LINE_HOME", "/declared/by/operator")
    calls = _install_runner(monkeypatch, lambda argv: (0, "{}"))
    out = qleap.qleap_line_status(design="hex_low_freq_v2")
    assert out["ok"] is True
    (call,) = calls
    assert call["env"] is None, "env=None inherits — the declaration wins"


def test_line_home_is_derived_from_the_named_design(line_tree, monkeypatch):
    calls = _install_runner(monkeypatch, lambda argv: (0, "{}"))
    qleap.qleap_line_status(design="hex_low_freq_v2")
    (call,) = calls  # a named design needs no --resolve-design subprocess
    expected = str(line_tree / "simulations" / "_line" / "hex_low_freq_v2")
    assert call["env"]["CHIPPY_LINE_HOME"] == expected


def test_the_active_design_is_resolved_by_the_script_itself(line_tree, monkeypatch):
    """No design and no $CHIPPY_LINE_HOME: the wrapper must ask
    `--resolve-design` — the ONE implementation of the precedence — and derive
    the tree from the answer."""

    def responder(argv):
        if "--resolve-design" in argv:
            return 0, '{"design_id": "hex_low_freq_v2"}\n'
        return 0, '{"phases": []}\n'

    calls = _install_runner(monkeypatch, responder)
    out = qleap.qleap_line_status()
    assert out["ok"] is True, out
    resolve, main = calls
    assert "--resolve-design" in resolve["argv"]
    expected = str(line_tree / "simulations" / "_line" / "hex_low_freq_v2")
    assert main["env"]["CHIPPY_LINE_HOME"] == expected
    assert "--design" not in main["argv"]  # the script re-resolves identically


def test_a_failed_resolve_is_a_stated_refusal_not_a_guess(line_tree, monkeypatch):
    _install_runner(monkeypatch, lambda argv: (1, "traceback, no JSON at all\n"))
    out = qleap.qleap_line_status()
    assert out["ok"] is False
    assert "CHIPPY_LINE_HOME" in out["error"] and "design" in out["error"]


def test_the_parse_reads_the_whole_log_not_a_tail(line_tree, monkeypatch):
    """A payload whose opening brace sits more than `_PARSE_TAIL_LINES` lines
    above the end. `_run_json`'s tail parse hands back a nested fragment with
    `parse_error=None` on exactly this shape; the whole-log read must return
    the complete document."""
    payload = {"steps": [{"i": i, "verdict": "pass"} for i in range(2500)]}
    text = json.dumps(payload, indent=2) + "\n"
    assert len(text.splitlines()) > qleap._PARSE_TAIL_LINES
    _install_runner(monkeypatch, lambda argv: (0, text))
    out = qleap.qleap_line_status(design="hex_low_freq_v2")
    assert out["parse_error"] is None
    assert out["parsed"] == payload, "must be the document, not a fragment"


def test_a_stated_refusal_arrives_parsed_with_ok_false(line_tree, monkeypatch):
    refusal = {"code": "not_yet_served", "message": "design has no contract"}
    _install_runner(monkeypatch, lambda argv: (1, json.dumps(refusal) + "\n"))
    out = qleap.qleap_line_status(design="hex_low_freq_v2")
    assert out["ok"] is False and out["returncode"] == 1
    assert out["parsed"] == refusal, "the refusal is evidence, not noise"


def test_pure_read_exposes_no_gate_argument():
    """Mirrors test_tool_gate_surface's doctrine from inside the suite: a
    status read must never grow dry_run/plan_only — gating a check trains
    people to click through."""
    import inspect

    params = inspect.signature(qleap.qleap_line_status).parameters
    assert "dry_run" not in params and "plan_only" not in params

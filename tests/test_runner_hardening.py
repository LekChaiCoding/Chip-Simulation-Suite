"""Regression tests for the subprocess runner and the synchronous wrappers.

Four defects, each of which cost something real and each of which is invisible
to a test that only checks the happy path:

* **the timeout killed the wrong process.** Campaign tools launch as
  ``uv run --no-project ... python script.py``, so the direct child is ``uv``
  and the COMSOL-touching interpreter is a grandchild. ``proc.kill()`` reaped
  ``uv``, the job reported failed, and the grandchild kept running with a
  COMSOL slot and a licence in hand. PLAYBOOK section 2 says wall-clock
  timeouts kill the whole process group; they did not.
* **the gate verdict was parsed from the first ``{``** to end-of-text, so a
  script that printed anything after its report handed the caller
  ``parsed=null`` — with no hint that a report existed and was dropped.
* **one log file per tool, opened ``"w"``.** Two concurrent calls to the same
  tool truncated each other's only record, and the verdict is read back out of
  that file, so a call could return its neighbour's gate report as its own.
* **``verify_cad`` reported ``passed=False`` when it could not check at all** —
  a missing checker script looked exactly like a bad mask.

…and then a review of that work found five more, four of which are here:

* **the staging file for ``<tool>_last.log`` was named by pid alone**, while the
  concurrency it defends against is two calls to one tool inside ONE server
  process. Same pid, same staging path, two ``copyfile``s at overlapping
  offsets: the copy that survived was half of each.
* **per-invocation log names fixed clobbering by growing without bound.**
  Nothing pruned them, so a fleet rollout left a permanent pile on the SMB
  share.
* **``verify_cad``'s "could not check" outcomes returned five of the nine keys
  the tool documents**, so a caller reading ``n_failures`` got a ``KeyError``
  rather than the tri-state it was promised.
* **``qleap_factory`` was running its own older copies of the log-name and
  JSON-parse code** that the other two wrappers had already had fixed.

The process tests spawn real processes on purpose: a mocked ``Popen`` cannot
demonstrate anything about a grandchild that outlives its parent, which is the
whole claim.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest

from comsol_suite import runner
from comsol_suite.runner import run_command
from comsol_suite.tools import cad
from comsol_suite.tools import qleap_chipconstruction as chipcon


@pytest.fixture(autouse=True)
def _ledger_off_the_real_factory(tmp_path, monkeypatch):
    """Keep test runs out of ``simulations/_factory/timings.jsonl``.

    ``run_command`` appends a telemetry line per run. Real, and useful — but a
    test suite's durations are not observations of the factory, and that file
    is read by the web UI.
    """
    monkeypatch.setattr(runner, "_timings_ledger_path",
                        lambda: tmp_path / "timings.jsonl")


def _py(body: str) -> List[str]:
    """argv running ``body`` under this interpreter (no shell, as in prod)."""
    return [sys.executable, "-c", textwrap.dedent(body)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours
        return True
    return True


# ─────────────────────────────────────────────────────────────────────────────
# (1) wall-clock timeout must take the whole tree, not just the direct child
# ─────────────────────────────────────────────────────────────────────────────
def test_timeout_kills_the_grandchild_not_just_the_direct_child(tmp_path):
    """The ``uv -> python -> COMSOL`` shape, minus COMSOL.

    The direct child spawns a grandchild and then sleeps; the grandchild
    records its pid and sleeps far past the timeout. Before the fix the
    grandchild was still alive after ``run_command`` returned ``timed_out``.
    """
    pidfile = tmp_path / "grandchild.pid"
    grandchild = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(120)"
    )
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        "print('spawned', flush=True);"
        "time.sleep(120)"
    )

    res = run_command([sys.executable, "-c", parent],
                      log_path=tmp_path / "run.log",
                      timeout_s=3.0, record_timing=False)
    assert res.timed_out
    assert not res.ok

    deadline = time.time() + 10
    while not pidfile.is_file() and time.time() < deadline:
        time.sleep(0.1)
    assert pidfile.is_file(), "grandchild never started; test proves nothing"
    pid = int(pidfile.read_text())

    try:
        # The kill is a signal, not a promise of instant death; give the
        # scheduler a moment before declaring the tree cleaned up.
        deadline = time.time() + 10
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not _pid_alive(pid), (
            f"grandchild {pid} survived the wall-clock timeout — it is still "
            f"holding whatever the killed job was declared to have released")
    finally:
        if _pid_alive(pid):  # never leave a stray sleeper behind
            os.kill(pid, 9)

    assert "TIMEOUT after 3.0s" in (tmp_path / "run.log").read_text()


def test_normal_run_is_unaffected_by_the_new_session(tmp_path):
    """The non-timeout path: still streams stdout+stderr, still reports rc."""
    argv = _py("""
        import sys
        print("to stdout")
        print("to stderr", file=sys.stderr)
        sys.exit(3)
    """)
    log = tmp_path / "normal.log"
    res = run_command(argv, log_path=log, timeout_s=60, record_timing=False,
                      debug=True)
    assert res.returncode == 3
    assert not res.timed_out and not res.ok
    text = log.read_text()
    assert "to stdout" in text and "to stderr" in text
    assert "[runner] argv" in text  # debug header still lands first


def test_timeout_does_not_kill_the_calling_process(tmp_path):
    """The group kill must never come home.

    ``killpg`` on a group we are ourselves a member of would take the MCP
    server (or the detached job worker) down with the child. The guard for
    that is a comparison against ``os.getpgrp()``; this test would die
    outright (SIGKILL, no traceback) if it regressed.
    """
    before = os.getpgrp()
    res = run_command(_py("import time; time.sleep(60)"),
                      log_path=tmp_path / "self.log",
                      timeout_s=1.0, record_timing=False)
    assert res.timed_out
    # Reached at all == the group kill did not include us; the pgrp check also
    # catches a child that somehow moved us into its own group.
    assert os.getpgrp() == before


# ─────────────────────────────────────────────────────────────────────────────
# (2) the gate report must survive anything printed around it
# ─────────────────────────────────────────────────────────────────────────────
def test_report_followed_by_prose_still_parses():
    """The killer case: a script that says one more thing after its verdict.

    Old behaviour: ``json.loads`` from the first ``{`` to end-of-text raised
    "Extra data", every later ``{`` was an inner brace, and the caller got
    ``parsed=None`` for a run that had printed a perfectly good gate report.
    """
    text = ('[12:00:01] checking block\n'
            '{\n  "pass": false,\n  "problems": ["missing JJ on U0_R0-C"]\n}\n'
            'wrote OptimizedModels/jj_manifest.json\n')
    parsed, error = chipcon._extract_trailing_json(text)
    assert error is None
    assert parsed == {"pass": False, "problems": ["missing JJ on U0_R0-C"]}


def test_last_of_several_json_objects_wins():
    """Chained scripts print one report each; the last one is the verdict."""
    text = (json.dumps({"stage": "validity", "pass": True}) + "\n"
            + "note: 2 cells flattened\n"
            + json.dumps({"stage": "block_checker", "pass": False}) + "\n")
    parsed, error = chipcon._extract_trailing_json(text)
    assert error is None
    assert parsed == {"stage": "block_checker", "pass": False}


def test_brace_bearing_prose_before_the_report_is_skipped():
    text = ("loaded config {'tile': 'U0_R0', 'letters': 'ABCD'}\n"
            '{"pass": true, "problems": []}\n')
    parsed, error = chipcon._extract_trailing_json(text)
    assert error is None
    assert parsed == {"pass": True, "problems": []}


def test_unparseable_output_explains_itself():
    """A null verdict must never arrive unexplained."""
    parsed, error = chipcon._extract_trailing_json("Traceback (most recent call last):\n")
    assert parsed is None
    assert error and "no JSON object" in error

    parsed, error = chipcon._extract_trailing_json("")
    assert parsed is None
    assert error and "no output" in error


def test_run_sync_surfaces_the_parse_failure(tmp_path, monkeypatch):
    """The wrapper's contract: ``parsed`` may be null, but never silently."""
    monkeypatch.setattr(chipcon, "_chipcon_dir", lambda: tmp_path)
    out = chipcon._run_sync("noisy_tool", _py("print('no json here')"),
                            timeout_s=60)
    assert out["ok"]
    assert out["parsed"] is None
    assert out["parse_error"], "a dropped verdict with no explanation"

    out = chipcon._run_sync(
        "reporting_tool",
        _py("""
            import json
            print(json.dumps({"pass": True, "problems": []}, indent=2))
            print("done.")
        """),
        timeout_s=60)
    assert out["parsed"] == {"pass": True, "problems": []}
    assert out["parse_error"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (3) two concurrent calls to one tool must not share a log
# ─────────────────────────────────────────────────────────────────────────────
def test_concurrent_calls_to_one_tool_keep_their_own_verdict(tmp_path, monkeypatch):
    """Same tool, twice, overlapping — as a fleet rollout does it.

    Old behaviour: both calls logged to ``logs/<tool>_last.log`` opened ``"w"``.
    The second call truncated the first's log while it was still running, so
    the first call read the second's output back and returned it as its own
    gate report.
    """
    monkeypatch.setattr(chipcon, "_chipcon_dir", lambda: tmp_path)
    started = threading.Event()

    slow = _py("""
        import json, sys, time
        print(json.dumps({"who": "A"}))
        sys.stdout.flush()
        time.sleep(2.5)
    """)
    fast = _py("""
        import json
        print(json.dumps({"who": "B"}))
    """)

    results: Dict[str, dict] = {}

    def call(name: str, argv: List[str], delay: float) -> None:
        if delay:
            time.sleep(delay)
        else:
            started.set()
        results[name] = chipcon._run_sync("same_tool", argv, timeout_s=60)

    threads = [threading.Thread(target=call, args=("A", slow, 0.0)),
               threading.Thread(target=call, args=("B", fast, 0.8))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert started.is_set() and not any(t.is_alive() for t in threads)

    assert results["B"]["parsed"] == {"who": "B"}
    assert results["A"]["parsed"] == {"who": "A"}, (
        "the long-running call read back the other call's output — they shared "
        "one log file")
    assert results["A"]["log_path"] != results["B"]["log_path"]
    # Kept for humans, and it is one of the two logs rather than a mash of both.
    last = tmp_path / "logs" / "same_tool_last.log"
    assert json.loads(last.read_text().strip().splitlines()[-1])["who"] in ("A", "B")


def test_last_log_copy_survives_two_finishers_in_one_process(tmp_path):
    """The staging name must be unique per CALL, not per process.

    Two calls to one tool inside one MCP server process share a pid, so a
    pid-only staging name is one name used twice: both ``copyfile``s truncate
    and write it, ``os.replace`` moves whatever mixture exists at that instant,
    and the loser raises FileNotFoundError on a path the winner renamed away.

    The sizes are what make it deterministic — 8 MB against 800 KB is far more
    than one buffered write, so the short copy finishes and renames while the
    long one is still streaming. Against the pid-only name this leaves a
    ``<tool>_last.log`` that is neither input: the head is one log's bytes and
    the tail is the other's.
    """
    big = tmp_path / "long_run.log"
    small = tmp_path / "short_run.log"
    big.write_bytes(b"A" * 8_000_000)
    small.write_bytes(b"B" * 800_000)

    barrier = threading.Barrier(2)

    def copy(src: Path) -> None:
        barrier.wait()
        runner.update_last_log_pointer(src, "same_tool")

    threads = [threading.Thread(target=copy, args=(big,)),
               threading.Thread(target=copy, args=(small,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    kept = (tmp_path / "same_tool_last.log").read_bytes()
    assert kept in (big.read_bytes(), small.read_bytes()), (
        f"the convenience copy is a mash of both runs: {len(kept)} bytes, "
        f"{kept.count(b'A')} from one and {kept.count(b'B')} from the other")
    # And no staging file is left lying about — it is not a ``*.log``, so
    # nothing ignores it and it would surface in ``git status``.
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_run_logs_do_not_accumulate_without_bound(tmp_path):
    """Per-invocation naming must not trade clobbering for a growing pile.

    ``new_log_path`` sweeps its own older logs for the same tool. What it must
    NOT sweep is everything else in a campaign's ``logs/``: those directories
    hold git-tracked ``.out``/``.err`` transcripts from earlier mask work, the
    ``<tool>_last.log`` copy, and other tools' logs.
    """
    keep = runner._LOG_RETENTION_PER_TOOL
    bystanders = [tmp_path / "export_U0_R0.out",      # git-tracked in the repo
                  tmp_path / "_seal_seam_chip.err",   # git-tracked in the repo
                  tmp_path / "mytool_last.log",       # the convenience copy
                  tmp_path / "othertool_20260802T120000Z_1_abcdef.log"]
    for path in bystanders:
        path.write_text("not mine to delete", encoding="utf-8")

    # Explicit, strictly increasing mtimes: "which of these is oldest" is the
    # sweep's whole decision, and leaving it to same-second timestamps would
    # make this test's verdict depend on the filesystem's clock resolution.
    minted = []
    base = time.time() - 10_000
    for index in range(keep + 12):
        path = runner.new_log_path(tmp_path, "mytool")
        path.write_text("run output", encoding="utf-8")
        os.utime(path, (base + index, base + index))
        minted.append(path)

    mine = {p.name for p in tmp_path.iterdir()
            if runner._run_log_pattern("mytool").match(p.name)}
    # keep + 1, not keep: the sweep runs before the new name is minted, so the
    # newest log has never faced one. Bounded is the claim, not exact.
    assert len(mine) <= keep + 1, (
        f"{len(mine)} run logs for one tool after {len(minted)} calls — "
        f"nothing prunes them")
    assert mine == {p.name for p in minted[-(keep + 1):]}, (
        "the survivors are not the newest runs")
    for path in bystanders:
        assert path.is_file(), f"the sweep deleted {path.name}, which it did not write"


def test_cs002_sync_wrapper_also_gets_its_own_log(tmp_path):
    """CS002 carries its own copy of the same helper, and had the same bug.

    Sequential here rather than concurrent, which is enough to show the cost:
    under the fixed name the second run overwrote the first run's log, so the
    ``log_path`` the first caller was handed no longer held the first run.
    """
    from comsol_suite.tools import qleap_cs002

    first = qleap_cs002._run_sync("corr", _py("print('one')"), cwd=tmp_path,
                                  timeout_s=60)
    second = qleap_cs002._run_sync("corr", _py("print('two')"), cwd=tmp_path,
                                   timeout_s=60)
    assert first["log_path"] != second["log_path"]
    assert Path(first["log_path"]).read_text().strip() == "one"
    assert Path(second["log_path"]).read_text().strip() == "two"


# ─────────────────────────────────────────────────────────────────────────────
# (4) "could not check" is not "the check failed"
# ─────────────────────────────────────────────────────────────────────────────
def _checker(tmp_path: Path, rc: int, name: str = "checker.py") -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(f"""
        GDS_PATH = "/nonexistent/default.gds"

        def main() -> int:
            if {rc}:
                print("[FAIL] corner radius out of spec")
            print("checked", GDS_PATH)
            return {rc}
    """), encoding="utf-8")
    return str(path)


def test_missing_gds_is_not_a_geometry_verdict(tmp_path):
    out = cad.verify_cad(gds_path=str(tmp_path / "absent.gds"),
                         checker_script=_checker(tmp_path, 0),
                         gds_var="GDS_PATH")
    assert out.get("passed") is None, (
        "an absent GDS was reported as a failed geometry check")
    assert out["ran"] is False
    assert out["error_kind"] == "gds_missing"
    assert out["returncode"] is None


def test_missing_checker_script_is_not_a_geometry_verdict(tmp_path):
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    out = cad.verify_cad(gds_path=str(gds),
                         checker_script=str(tmp_path / "no_such_checker.py"),
                         gds_var="GDS_PATH")
    assert out.get("passed") is None, (
        "a missing checker script was indistinguishable from a bad mask")
    assert out["ran"] is False
    assert out["error_kind"] == "checker_missing"


def test_checker_without_the_gds_constant_is_not_a_geometry_verdict(tmp_path):
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    out = cad.verify_cad(gds_path=str(gds),
                         checker_script=_checker(tmp_path, 0),
                         gds_var="WRONG_NAME")
    assert out.get("passed") is None, (
        "a checker that never ran was reported as a failed geometry check")
    assert out["ran"] is False
    assert out["error_kind"] == "checker_interface"


def test_a_real_failure_still_reads_as_a_failure(tmp_path):
    """The other half: the distinction must not blunt a genuine FAIL."""
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    out = cad.verify_cad(gds_path=str(gds),
                         checker_script=_checker(tmp_path, 1),
                         gds_var="GDS_PATH", debug=True)
    assert out["ran"] is True
    assert out["passed"] is False
    assert out["error_kind"] is None
    assert out["returncode"] == 1
    assert out["n_failures"] == 1
    assert "[FAIL]" in out["report"]


def test_a_checker_that_raises_reached_no_verdict(tmp_path):
    """A checker can blow up BECAUSE the mask is bad — gdstk on a corrupt GDS.

    Which is exactly why this is not ``passed=False``: from out here the two
    are indistinguishable, and ``passed=False`` would assert a geometry defect
    no checker ever named. What the caller gets instead is "nobody reached a
    verdict", the exception, and whatever the checker printed before it died —
    enough to tell the two apart by hand.
    """
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    checker = tmp_path / "exploding_checker.py"
    checker.write_text(textwrap.dedent("""
        GDS_PATH = "/nonexistent/default.gds"

        def main() -> int:
            print("reading", GDS_PATH)
            raise RuntimeError("gdstk: unexpected record type 0x7f")
    """), encoding="utf-8")

    out = cad.verify_cad(gds_path=str(gds), checker_script=str(checker),
                         gds_var="GDS_PATH")
    assert out["passed"] is None
    assert out["ran"] is False
    assert out["error_kind"] == "checker_raised"
    assert out["returncode"] is None
    assert "gdstk: unexpected record type" in out["error"]
    assert "reading" in out["report"], (
        "the checker's own output before it died is the evidence a human needs")


@pytest.mark.parametrize("kind,kwargs", [
    ("gds_missing", {"gds_path": "absent.gds", "gds_var": "GDS_PATH"}),
    ("checker_missing", {"checker_script": "no_such_checker.py",
                         "gds_var": "GDS_PATH"}),
    ("checker_interface", {"gds_var": "WRONG_NAME"}),
])
def test_could_not_check_still_answers_every_documented_key(tmp_path, kind, kwargs):
    """One shape for both outcomes, or the tri-state just moves the crash.

    ``verify_cad`` documents nine keys. A caller told ``passed`` may be null
    goes on to read ``n_failures`` or ``report`` to find out what happened — and
    on the paths where the check never ran, those keys were simply absent, so
    the honest answer arrived as a ``KeyError``. Absent is not a value; null is.
    """
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    call = {"gds_path": str(gds), "checker_script": _checker(tmp_path, 0)}
    for key, value in kwargs.items():
        call[key] = str(tmp_path / value) if key != "gds_var" else value

    out = cad.verify_cad(**call)
    assert out["error_kind"] == kind
    missing = [k for k in ("passed", "ran", "error_kind", "returncode",
                           "gds_path", "checker_script", "n_failures",
                           "report", "error") if k not in out]
    assert not missing, f"{kind} omits documented keys: {missing}"
    assert out["n_failures"] is None, "a failure count nobody counted"


def test_the_tool_surface_says_the_verdict_can_be_null():
    """The contract the driving agent reads is the docstring, not cad.py.

    A tri-state shipped to a reader that has been told ``passed`` is boolean
    delivers nothing: the agent still reads a null as falsy and reports a bad
    mask. ``@mcp.tool()`` serves the docstring verbatim as the tool description,
    so that text IS the contract and it has to carry the third state.
    """
    import asyncio

    from comsol_suite import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    description = tools["verify_cad"].description or ""
    for token in ("null", "ran", "error_kind", "checker_missing"):
        assert token in description, (
            f"the served verify_cad contract never mentions {token!r}")
    assert "could not look" in description


def test_a_real_pass_still_reads_as_a_pass(tmp_path):
    gds = tmp_path / "device.gds"
    gds.write_bytes(b"not really a gds")
    out = cad.verify_cad(gds_path=str(gds),
                         checker_script=_checker(tmp_path, 0),
                         gds_var="GDS_PATH")
    assert out["ran"] is True
    assert out["passed"] is True
    assert out["returncode"] == 0
    assert out["n_failures"] == 0
    assert out["error"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (5) the factory control-plane wrapper had its own older copy of both fixes
# ─────────────────────────────────────────────────────────────────────────────
def test_factory_wrapper_parses_a_report_followed_by_prose(tmp_path, monkeypatch):
    """``qleap_factory_status`` on a CLI that says one more thing at the end.

    Same defect as (2), in the module that was left behind: json.loads from the
    FIRST ``{`` to end-of-text raised "Extra data" on the trailing line, every
    later ``{`` was an inner brace, and the tool returned ``parsed=null`` for a
    factory whose whole record chain had just been printed. That null is what
    a model reads as "the factory has not been initialised".
    """
    from comsol_suite.tools import qleap_factory

    monkeypatch.setattr(qleap_factory, "_factory_home", lambda: tmp_path)
    monkeypatch.setattr(qleap_factory, "_repo_root", lambda: tmp_path)

    out = qleap_factory._run_json("qleap_factory_status", _py("""
        import json
        print(json.dumps({"phases": {"F0": ["chip"]}, "andons": []}))
        print("wrote simulations/_factory/logs/status.txt")
    """))
    assert out["parsed"] == {"phases": {"F0": ["chip"]}, "andons": []}
    assert out["parse_error"] is None


def test_factory_wrapper_gets_a_log_per_invocation(tmp_path, monkeypatch):
    """Two clients asking for factory status at once must not share a file.

    The fixed ``logs/<tool>_last.log`` was opened ``"w"``, and the answer is
    read back out of it, so the slower reader got the faster one's document.
    """
    from comsol_suite.tools import qleap_factory

    monkeypatch.setattr(qleap_factory, "_factory_home", lambda: tmp_path)
    monkeypatch.setattr(qleap_factory, "_repo_root", lambda: tmp_path)

    first = qleap_factory._run_json("qleap_factory_line",
                                    _py("import json; print(json.dumps({'n': 1}))"))
    second = qleap_factory._run_json("qleap_factory_line",
                                     _py("import json; print(json.dumps({'n': 2}))"))
    assert first["log_path"] != second["log_path"]
    assert first["parsed"] == {"n": 1}
    assert second["parsed"] == {"n": 2}
    assert Path(first["log_path"]).read_text().strip().endswith('{"n": 1}')


def test_factory_wrapper_explains_a_null_document(tmp_path, monkeypatch):
    from comsol_suite.tools import qleap_factory

    monkeypatch.setattr(qleap_factory, "_factory_home", lambda: tmp_path)
    monkeypatch.setattr(qleap_factory, "_repo_root", lambda: tmp_path)

    out = qleap_factory._run_json("qleap_factory_status",
                                  _py("print('Traceback (most recent call last):')"))
    assert out["parsed"] is None
    assert out["parse_error"] and "no JSON object" in out["parse_error"]
    assert out["log_tail"]

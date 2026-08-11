"""qleap factory control-plane tools — read-only views of the F0-F3 record chain.

The agent could see individual campaigns (``qleap_*_status``) but had no way to
read the *factory* state: which scopes hold a signed acceptance record, what the
open andons are, what the line's own plan says comes next. Asked "how many scopes
are accepted in F2 and is F3 sealed?", a model therefore fell back to generic
filesystem tools, guessed at paths, and answered "the factory has not been
initialised" while `simulations/_factory/records/` held signed F0-F3 records.

That is a tool gap, not a model failure, and these three tools close it:

    qleap_factory_status   the whole record chain: per-phase accepted scopes,
                           andons, embargoes, ledger chain state
    qleap_factory_line     the line definition (phases, stations, gates, human
                           checkpoints) parsed from FACTORY.md's floor plan
    qleap_factory_record   one scope's full acceptance record

Those three are pure reads, so they carry no ``dry_run`` argument by design (see
``qleap_chipconstruction._preflight_sync`` for why mutating tools must). They
are also design-agnostic: the scope grammar comes from the active design in
``simulations/_designs/``, never from a literal in here.

    qleap_f2_gauntlet      RUN F2's S2.1-S2.5 for one tile

That fourth one closes a bigger gap than the first three, found on 2026-08-04 by
enumerating what the agent can actually reach. Of 67 MCP tools it had:

  * ``qleap_factory_line`` to read what F2's stations are, and
  * ``qleap_factory_status`` to read whether they are accepted,

and **nothing that runs them**. The whole F2 gauntlet — the eigen g_QR solve, the
frequency retune, the notch tune, the cable gamma walk, the merge-and-verify —
was reachable only by a human typing ``run_gauntlet.py`` at a shell. So an agent
asked to take a tile through F2 could describe the work in detail and then had no
way to do it; the individual campaign tools it *does* hold
(``qleap_run_eigen_gqr``, ``qleap_nt2_*``, ``qleap_cct001_*``) are the raw
campaign drivers, which is precisely NOT the same thing as the gauntlet: they
carry none of its chaining, staleness checks, SPC gates or report writing.

That is a tool gap of the same species as the one this module was created for,
and it is the thing standing between "Qwen can talk about the line" and "Qwen can
drive the line".
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from ..config import load_config
from ..runner import (extract_trailing_json, new_log_path, run_command,
                      update_last_log_pointer)


def _repo_root() -> Path:
    """The qleap repo, from the config's own marker-based resolution.

    NOT ``chip_sim_root.parent``: that read the containment root as if it were
    the legacy assets dir, so every campaign path landed one level ABOVE the
    checkout — where a status call silently created an empty shadow
    ``simulations/ChipConstruction/`` tree outside the repo.
    """
    return Path(load_config().repo_root)


def _factory_home() -> Path:
    return _repo_root() / "simulations" / "_factory"


def _factory_tools() -> Path:
    return _factory_home() / "tools"


#: One path segment of the record tree: letters, digits, underscore, hyphen.
#: Deliberately NOT the scope grammar itself — the tiles and letters come from
#: the active design and must not be hardcoded here. This is the containment
#: check, which is a different question from "is this a scope that exists": a
#: name that gets past it and does not exist still yields an ordinary not-found.
_SEGMENT_OK = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _safe_segment(kind: str, value: str) -> str:
    """One model-supplied path segment, or a refusal.

    `phase` and `scope` are joined straight into a filesystem path, and they come
    from a language model. Measured against Qwen/Qwen3.6-27B-FP8: asked about
    tile U0_R0 it sent ``scope="tile U0_R0"`` in 2 of 3 runs, so a malformed
    segment is the ordinary case rather than an adversarial one.

    Two things this stops. A segment containing ``..`` or a separator escapes the
    record tree entirely; and a segment that escaped the REPO root then made the
    error branch itself raise, because ``Path.relative_to`` throws ValueError on
    a path outside its argument — so a typo returned an uncaught traceback
    instead of an error dict.
    """
    text = str(value)
    if not text or not set(text) <= _SEGMENT_OK:
        raise _ScopeRefused(
            f"{kind} {value!r} is not a single record-tree name. Expected one "
            f"segment of letters, digits, '_' or '-' — e.g. phase 'F2', scope "
            f"'chip', a tile 'U0_R0' or a qubit 'U0_R0_A'. Call "
            f"qleap_factory_status to see which scopes exist."
        )
    return text


class _ScopeRefused(ValueError):
    """A model-supplied phase/scope that must not reach the filesystem."""


def _display(path: Path) -> str:
    """Repo-relative when it can be, absolute when it cannot.

    `Path.relative_to` raises ValueError for anything outside its argument, and
    this is called from an ERROR branch — so the old unconditional call turned a
    bad path into an uncaught traceback while trying to report it.
    """
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


def _cli_argv(script: Path, args: list[str]) -> list[str]:
    """Run a control-plane CLI with THIS interpreter, not through ``uv run``.

    Both callers here — ``factory_status.py`` and ``line_spec.py`` — need only an
    interpreter: they import the standard library plus ``qleapsim``, which their
    own ``_bootstrap`` puts on the path. That is why this function never passed a
    single ``--with`` flag, unlike ``qleap_chipconstruction``'s namesake, which
    genuinely needs ``gdstk``/``mph`` and must keep using uv.

    So the ``uv run`` wrapper bought nothing and cost the tool its own timeout.
    Measured on this SMB checkout 2026-08-04:

        factory_status.py --json, this interpreter : 36.7 s
        factory_status.py --json, via `uv run`     : >180 s -> KILLED

    uv's environment resolution over SMB was adding 140+ seconds to a 37-second
    script, so ``qleap_factory_status`` timed out on every call. Found by letting
    Qwen drive the line: it asked for the line state, the tool was SIGKILLed at
    the 180 s ceiling, and the agent had to work around its own primary status
    tool by fetching each phase record individually.
    """
    return [sys.executable, str(script), *args]


#: How much of the log the parse below is allowed to look at. ``factory_status
#: --json`` prints the whole record chain, which is far longer than a gate
#: report, so this is generous where ``qleap_chipconstruction``'s is not.
_PARSE_TAIL_LINES = 4000


def _run_json(tool: str, argv: list[str], timeout_s: float = 180) -> Dict[str, Any]:
    """Run one of the offline control-plane CLIs and return its parsed JSON.

    These print a JSON document on stdout with ``--json``; `run_command` captures
    to a log, so parse the log tail rather than assuming a stdout pipe.

    Both halves of that — the log name and the parse — used to be this module's
    own older copies of what the rest of the suite fixed, and both were wrong in
    ways a read-only status tool still feels. The fixed ``logs/<tool>_last.log``
    was opened ``"w"``, so two clients asking for factory status at once read
    each other's answer; the parse json.loads'd from the FIRST ``{`` to
    end-of-text, so one trailing "wrote ..." line from the CLI turned the whole
    record chain into ``parsed=null`` and the agent reported an uninitialised
    factory. Both now come from :mod:`comsol_suite.runner`, which is the point
    of that module.
    """
    log_path = new_log_path(_factory_home() / "logs", tool)
    res = run_command(argv, log_path=log_path, cwd=_repo_root(), timeout_s=timeout_s)
    update_last_log_pointer(log_path, tool)
    parsed, parse_error = extract_trailing_json(res.log_tail(_PARSE_TAIL_LINES),
                                                tail_lines=_PARSE_TAIL_LINES)
    return {
        "ok": res.ok,
        "returncode": res.returncode,
        "log_path": str(log_path),
        "parsed": parsed,
        # A null verdict never arrives unexplained: these tools promise the
        # caller a document, and "the factory has not been initialised" is
        # exactly the wrong conclusion to let a model draw from silence.
        "parse_error": parse_error,
        "log_tail": None if parsed is not None else res.log_tail(40),
    }


def qleap_factory_status() -> Dict[str, Any]:
    """Where every artifact stands on the F0-F3 line.

    Returns, per phase: the scopes holding a signed acceptance record (with the
    record hash, when it was created, how many caveats it carries and how many
    prior records it superseded), plus the count of quarantined attempts. Also
    the open andons (line stops), the embargo count, and whether the append-only
    ledger's hash chain still verifies.

    This is the tool to answer "is F3 sealed?", "what is accepted in F2?", "is
    anything stopping the line?" — do not try to infer it by listing directories.
    """
    argv = _cli_argv(_factory_tools() / "factory_status.py", ["--json"])
    return _run_json("qleap_factory_status", argv)


def qleap_factory_line() -> Dict[str, Any]:
    """The line's own plan: phases in order, the stations inside each, the gate
    every station must clear, the subagent that owns it, its MCP tools, and the
    human checkpoints.

    Parsed from ``QubitDesignPipeline/FACTORY.md``'s floor-plan table, so it is
    the same definition the web UI renders and a human edits — not a second copy.
    Use it to work out what comes next and what has to hold before it may start;
    ``unresolved`` lists any row the parser could not make sense of.
    """
    argv = _cli_argv(_factory_tools() / "line_spec.py", ["--json"])
    return _run_json("qleap_factory_line", argv)


def qleap_factory_record(phase: str, scope: str) -> Dict[str, Any]:
    """One scope's full acceptance record (``records/<phase>/<scope>/accepted.json``).

    ``phase`` is F0/F1/F2/F3; ``scope`` is ``chip``, a tile (``U0_R0``) or a
    qubit (``U0_R0_A``) — whatever ``qleap_factory_status`` listed. The record
    carries the achieved values, the artifacts with their sha256s, the gate
    reports, any caveats, the human sign-offs, and the record hash that chains it
    to its work order.
    """
    try:
        phase = _safe_segment("phase", phase)
        scope = _safe_segment("scope", scope)
    except _ScopeRefused as exc:
        return {"ok": False, "error": str(exc)}

    path = _factory_home() / "records" / phase / scope / "accepted.json"
    if not path.is_file():
        return {
            "ok": False,
            "error": f"no acceptance record at {_display(path)}",
            "hint": "call qleap_factory_status first to see which scopes have one",
        }
    try:
        return {"ok": True, "path": str(path), "record": json.loads(path.read_text())}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"record is not valid JSON: {exc}"}


def _f2_tools() -> Path:
    return _repo_root() / "simulations" / "F2_UnitCell001" / "tools"


def _f2_vocabulary() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(tiles, step keys) read from F2's own module, never copied.

    Imported rather than duplicated so a new step or a design with different
    tiles cannot leave this tool advertising a stale vocabulary — the failure
    species this repo keeps finding, where a declaration and the thing it
    describes drift apart and nobody checks.

    Via ``import_module`` rather than ``module_from_spec`` + ``exec_module``: the
    latter leaves the module absent from ``sys.modules`` while its body runs, and
    ``f2_commands`` defines dataclasses. ``@dataclass`` looks its own class's
    module up in ``sys.modules`` to resolve annotations, finds ``None``, and dies
    on ``AttributeError: 'NoneType' object has no attribute '__dict__'`` — an
    error that names dataclasses and says nothing about the real cause. Measured
    here on 2026-08-04.

    The two names live in DIFFERENT modules, mirroring ``run_gauntlet``'s own
    imports: ``STEPS`` in ``f2_commands``, ``TILES`` in ``f2_unitcell001lib``.
    Reading both from ``f2_commands`` raised ``AttributeError`` — and an earlier
    blanket ``except Exception`` here turned that into a silently empty
    vocabulary, so every tile and step validated as fine. Hence the narrow
    excepts: a missing checkout is tolerated, a wrong attribute is not.

    Returns empty tuples if F2 is not in this checkout. An unknown vocabulary
    must not stop the tool — it only means names go unvalidated here and the
    runner's own argparse refuses them instead, one layer later.
    """
    import importlib

    tools = str(_f2_tools())
    added = tools not in sys.path
    if added:
        sys.path.insert(0, tools)
    try:
        steps = tuple(importlib.import_module("f2_commands").STEPS)
        tiles = tuple(importlib.import_module("f2_unitcell001lib").TILES)
        return tiles, steps
    except (ImportError, ModuleNotFoundError):   # pragma: no cover - thin checkout
        return (), ()
    finally:
        if added:
            try:
                sys.path.remove(tools)
            except ValueError:               # pragma: no cover
                pass


#: A gauntlet solve is measured in hours, not minutes: S2.3's notch driver alone
#: is given 12 h by its own Command spec. This is the ceiling for the whole run.
_GAUNTLET_SOLVE_TIMEOUT_S = 20 * 3600


def qleap_f2_gauntlet(tile: str, only: str | None = None, letters: str | None = None,
                      dry_run: bool = True, solve: bool = False,
                      force: bool = False) -> Dict[str, Any]:
    """Run F2's S2.1-S2.5 gauntlet for one unit-cell tile.

    This is the station runner, not a raw campaign driver: it chains the steps,
    refuses to certify an artifact older than its own input, applies the SPC gate
    for each step and writes a hash-chained report. Prefer it over calling
    ``qleap_run_eigen_gqr`` / ``qleap_nt2_*`` / ``qleap_cct001_*`` by hand — those
    are the underlying campaigns and carry none of that.

    ``tile``    a unit cell, e.g. ``U0_R0``.
    ``only``    one step key (``S2.1_eigen_gqr`` … ``S2.5_merge_verify``) to run
                just that step; omit to run the whole gauntlet.
    ``letters`` which qubits, e.g. ``"AB"``; omit for all four.
    ``dry_run`` DEFAULT TRUE: plan only, touching nothing. Read the plan and
                check the command list before spending COMSOL hours.
    ``solve``   actually run the solves. Requires ``dry_run=False``, so the
                expensive path needs two explicit arguments rather than one.
    ``force``   re-run commands whose outputs already look current. Without it a
                completed step REPLAYS from disk instead of re-deriving, which is
                usually what you want and is always what the verdict's own
                source path will tell you.

    ``--rederive`` IS DELIBERATELY NOT EXPOSED. On a whole-chain run it discards
    RCS002's solve-budget boundary, which is days of accepted work, and no agent
    should be able to reach that through a tool call. A human runs it at a shell.

    A non-zero ``returncode`` with ``ok=false`` usually means a gate REFUSED.
    That is the tool working, not a malfunction: read the verdicts for the
    quantity that failed, and do not retry without changing something.

    ON THE RETURN SHAPE, stated plainly because it is a real limitation rather
    than an oversight: ``run_gauntlet`` has no ``--json`` flag, so ``parsed`` is
    ``null`` and the human-readable run log arrives in ``log_tail`` (with the
    whole thing at ``log_path``). The STRUCTURED verdicts do exist — each step
    writes ``<TILE>/Data/<step>_report_<ts>.json``, hash-chained through
    ``gauntlet_chain.json``, and the log names the path it wrote. Read that file
    for machine-checkable gate results; the log tail is for seeing what happened.
    """
    tiles, steps = _f2_vocabulary()

    if tiles and tile not in tiles:
        return {"ok": False,
                "error": f"tile {tile!r} is not one of this design's tiles: "
                         f"{list(tiles)}"}
    if only is not None and steps and only not in steps:
        return {"ok": False,
                "error": f"step {only!r} is not an F2 step. Choose one of: "
                         f"{list(steps)}"}
    if letters is not None and not (letters.isalpha() and letters.isupper()):
        return {"ok": False,
                "error": f"letters {letters!r} should be upper-case qubit letters "
                         f'like "AB" or "ABCD"'}
    if solve and dry_run:
        return {
            "ok": False,
            "error": "solve=true contradicts dry_run=true. To spend COMSOL hours "
                     "pass BOTH dry_run=false AND solve=true; to see the plan, "
                     "leave both at their defaults.",
        }

    script = _f2_tools() / "run_gauntlet.py"
    if not script.is_file():
        return {"ok": False, "error": f"no gauntlet runner at {_display(script)}"}

    args = ["--tile", tile]
    if only:
        args += ["--only", only]
    if letters:
        args += ["--letters", letters]
    if dry_run:
        args.append("--dry-run")
    if solve:
        args.append("--solve")
    if force:
        args.append("--force")

    # This interpreter, not `uv run`: the runner needs only the stdlib plus
    # qleapsim (its own `_bootstrap` handles the path) and measures 0.73 s here,
    # where uv's resolution over SMB adds 140+ s — the exact trap documented on
    # `_cli_argv`. Each SOLVE command the runner spawns builds its own uv
    # environment with the --with flags that command needs, so nothing is lost.
    return _run_json(
        "qleap_f2_gauntlet", _cli_argv(script, args),
        timeout_s=_GAUNTLET_SOLVE_TIMEOUT_S if solve else 600)


# ─────────────────────────────────────────────────────────────────────────────
# The NEW line (QubitDesignPipeline/NewPipeline): plan it, and launch it
# ─────────────────────────────────────────────────────────────────────────────

def _new_line_tools() -> Path:
    return _repo_root() / "QubitDesignPipeline" / "NewPipeline" / "tools"


#: An executing line walks seventeen blocks, several of which take a COMSOL slot
#: and solve. The same ceiling the F2 gauntlet books, for the same reason: below
#: it a legitimate overnight run gets SIGKILLed and reports as a tool failure.
_LINE_EXECUTE_TIMEOUT_S = _GAUNTLET_SOLVE_TIMEOUT_S

#: A dry run resolves the whole plan — every role, every tolerance, every
#: adapter — over SMB. The tool's own declared budget is 60 s; this is the
#: ceiling, generous against a cold share.
_LINE_PLAN_TIMEOUT_S = 900

#: What ``execute_line.py`` stamps on the document it writes. Read back so this
#: wrapper can never present some other JSON file as an execute report.
_LINE_REPORT_SCHEMA = "line-execute-report-1.0"


def _line_report_dir() -> Path:
    return _factory_home() / "line_runs"


def _read_line_report(path: Path, existed_ns: int | None):
    """The execute report, read from the FILE the driver wrote.

    Returns ``(parsed, parse_error)``.

    NOT from the log tail, and that difference is the whole point of this
    function. Every other tool in this module parses ``res.log_tail(4000)`` with
    :func:`extract_trailing_json`, which is right for documents that fit. This
    one does not: a DRY RUN of ``execute_line.py --json`` for the active design
    already prints 3048 lines, 2489 of them the plan, and an EXECUTING report
    adds the runner's own report — which carries every step record twice
    (``steps`` and ``by_block``) with inputs, tolerances, notes and problems.

    Truncation there does not raise. Measured on the real output cut to its last
    2000 lines, ``extract_trailing_json`` returned ``({'letter': 'A', 'tile':
    'U0_R0'}, None)`` — a NESTED FRAGMENT with ``parse_error=None``. The tool's
    own docstring tells the reader to trust ``parsed.sealed``,
    ``parsed.confessions`` and ``parsed.reached_runner``; on a fragment all
    three are simply absent, which reads as "nothing confessed". That is a pill
    that looks right while being untrue.

    The driver already writes the report atomically to a path this wrapper
    chooses, so the document is read whole from disk instead. Three refusals,
    each returning a stated ``parse_error`` rather than a silent ``None``:

    * the file is not there — the driver died before writing it;
    * the file is there but is the one that was already there (unchanged
      ``st_mtime_ns``), so it is a STALE report from an earlier run and not
      evidence about this one;
    * the document does not declare ``schema_version`` ``line-execute-report-1.0``,
      so it is not an execute report at all.
    """
    if not path.is_file():
        return None, (
            f"the driver wrote no execute report at {_display(path)} — "
            f"read the log for why it stopped")
    stat_ns = path.stat().st_mtime_ns
    if existed_ns is not None and stat_ns == existed_ns:
        return None, (
            f"{_display(path)} is unchanged since before this call: it is a "
            f"STALE report from an earlier run, not evidence about this one")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"could not read {_display(path)}: {error}"
    if not isinstance(parsed, dict):
        return None, (
            f"{_display(path)} holds a {type(parsed).__name__}, not an execute "
            f"report object")
    declared = parsed.get("schema_version")
    if declared != _LINE_REPORT_SCHEMA:
        return None, (
            f"{_display(path)} declares schema_version {declared!r}, not "
            f"{_LINE_REPORT_SCHEMA!r} — this is not an execute report")
    return parsed, None


def qleap_line_execute(dry_run: bool = True, design: str | None = None,
                       tile: str | None = None, letter: str | None = None,
                       force: bool = False,
                       run_id: str | None = None,
                       out: str | None = None) -> Dict[str, Any]:
    """Drive the NEW line: resolve the plan and, unless ``dry_run``, RUN it.

    Wraps ``QubitDesignPipeline/NewPipeline/tools/execute_line.py``, which is the
    only caller of ``linespec.runner.Runner.run``. See that module's docstring
    for the refusal rule; the short version is that a plan with any blocking
    problem is refused and the runner is never reached, which today is the
    ordinary outcome for the active design.

    ``dry_run`` DEFAULTS TO TRUE and is the FIRST argument deliberately: it is
    the argument ``agent/policy.py``'s ``_leaves_dry_run`` reads, and a tool that
    can spend solver time without it would be launchable from the chat pane with
    no human approval at all.

    ``parsed`` is read from the report FILE, never from the log tail — see
    :func:`_read_line_report` for the measurement that forced that.

    ``run_id`` names the run (a campaign name like ``ChipReconstruction002``);
    it is passed through as ``--run-id`` and changes nothing about what runs.
    A refused or dry run still reports ``run_id`` null — the name is not
    evidence the run happened.
    """
    script = _new_line_tools() / "execute_line.py"
    if not script.is_file():
        return {"ok": False, "error": f"no line driver at {_display(script)}"}

    # The report path is always ours to name, whether or not the caller gave
    # one: the document is what this tool returns, so it may not depend on the
    # driver's default landing place being guessable from here.
    if out:
        report_path = Path(out)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        report_path = _line_report_dir() / f"execute_{stamp}_{os.getpid()}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existed_ns = report_path.stat().st_mtime_ns if report_path.is_file() else None

    args = ["--out", str(report_path)]
    args.append("--dry-run" if dry_run else "--execute")
    if design:
        args += ["--design", design]
    if tile:
        args += ["--tile", tile]
    if letter:
        args += ["--letter", letter]
    if force:
        args.append("--force")
    if run_id:
        args += ["--run-id", run_id]

    # This interpreter, not `uv run`: this venv already carries mph and numpy,
    # and uv's environment resolution over SMB added 140+ s to a 37 s script
    # here once already (see `_cli_argv`). The blocks the runner dispatches
    # build their own environments where they need one.
    timeout_s = _LINE_PLAN_TIMEOUT_S if dry_run else _LINE_EXECUTE_TIMEOUT_S
    log_path = new_log_path(_factory_home() / "logs", "qleap_line_execute")
    res = run_command(_cli_argv(script, args), log_path=log_path,
                      cwd=_repo_root(), timeout_s=timeout_s)
    update_last_log_pointer(log_path, "qleap_line_execute")
    parsed, parse_error = _read_line_report(report_path, existed_ns)
    return {
        # `ok` is the PROCESS's health, not the line's verdict. rc is advisory
        # in both directions here (I4); `parsed.sealed` is the verdict.
        "ok": res.ok,
        "returncode": res.returncode,
        "log_path": str(log_path),
        "report_path": str(report_path),
        "parsed": parsed,
        "parse_error": parse_error,
        "log_tail": None if parsed is not None else res.log_tail(40),
    }


# ─────────────────────────────────────────────────────────────────────────────
# The NEW line, read back: the record tree qleap_line_execute writes into
# ─────────────────────────────────────────────────────────────────────────────

#: The payload shapes ``line_status.py`` emits, mirrored from that script's own
#: ``WHAT`` tuple (stated once there so its help, choices and dispatch cannot
#: drift apart). This copy exists only to refuse a bad value with a usable
#: message BEFORE spawning a subprocess whose argparse would print usage text
#: instead of JSON — and if it goes stale the failure is a stated refusal
#: naming both lists, never a silent wrong answer.
_LINE_STATUS_WHAT = ("status", "line", "steps")

#: A status read walks the record tree over SMB and solves nothing. Measured on
#: the live hex_low_freq_v2 tree 2026-08-12: ``--what status`` 1.9 s, ``--what
#: steps`` 1.3 s. The ceiling is generous against a cold share for the same
#: reason ``_LINE_PLAN_TIMEOUT_S`` is, and sits far below it: nothing here
#: resolves a plan, let alone runs one.
_LINE_STATUS_TIMEOUT_S = 300

#: The variable ``linespec.serve.line_home()`` reads. Spelled here rather than
#: imported: pulling ``linespec`` (and through it ``qleapsim``) into the server
#: process for one string buys nothing, and a drift here cannot be silent — a
#: wrongly-named variable leaves the child's own refusal in force, and that
#: refusal names the variable it actually wanted.
_LINE_HOME_ENV = "CHIPPY_LINE_HOME"


def _run_whole_log_json(tool: str, argv: list[str], timeout_s: float,
                        env: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Run one of the new line's CLIs and parse its JSON from the WHOLE log.

    Like :func:`_run_json` except for where the parse looks. ``_run_json``
    parses ``res.log_tail(4000)``, which is right for the factory-status
    documents it serves and wrong here for the reason :func:`_read_line_report`
    measures: a truncated tail does not raise, it returns a NESTED FRAGMENT
    with ``parse_error=None`` — a pill that looks right while being untrue.
    ``line_status.py`` has no ``--out``, so unlike the execute report there is
    no separate file to read back whole; the complete document is recovered by
    reading the complete log instead. The ``steps`` payload is 619 lines on the
    live tree today and grows with every block the walk records, so "the tail
    is surely enough" is exactly the declaration nobody would re-check.

    One shape ``_run_json`` never sees: a STATED REFUSAL. ``line_status.py``
    exits 1 with ``{code, message}`` on stderr (merged into the log by
    ``run_command``) when it cannot serve — ``$CHIPPY_LINE_HOME`` undeclared,
    the design has no contract. That document parses fine and is returned in
    ``parsed``; ``returncode`` 0 is what says ``parsed`` is a payload rather
    than a refusal, and ``ok`` is already false on the refusal path.
    """
    log_path = new_log_path(_factory_home() / "logs", tool)
    res = run_command(argv, log_path=log_path, cwd=_repo_root(),
                      timeout_s=timeout_s, env=env)
    update_last_log_pointer(log_path, tool)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {
            "ok": False,
            "returncode": res.returncode,
            "log_path": str(log_path),
            "parsed": None,
            "parse_error": f"could not read {_display(log_path)}: {error}",
            "log_tail": None,
        }
    parsed, parse_error = extract_trailing_json(text)
    return {
        "ok": res.ok,
        "returncode": res.returncode,
        "log_path": str(log_path),
        "parsed": parsed,
        "parse_error": parse_error,
        "log_tail": None if parsed is not None else res.log_tail(40),
    }


def _line_home_env(design: str | None, script: Path):
    """The child's environment, with ``$CHIPPY_LINE_HOME`` filled in when the
    server's own is empty.

    Returns ``(env, refusal)``: ``env`` of ``None`` means inherit the parent's
    environment unchanged; a non-``None`` ``refusal`` is a finished tool result
    to return as-is.

    ``linespec.serve.line_home()`` refuses to default on purpose — guessing a
    directory would be a declaration with nothing behind it. This wrapper is
    not guessing: its sibling ``qleap_line_execute`` launches runs whose
    records land at ``simulations/_line/<design_id>`` — the landing place
    ``execute_line.py`` itself anchors to (``linespec.runner.line_state_root``)
    — so pointing the read at the tree the launch surface writes is reading a
    declaration back. An operator who set ``$CHIPPY_LINE_HOME`` has declared a
    tree explicitly and is honoured over the derivation, unconditionally.

    When no design was named, the id is resolved by ``line_status.py
    --resolve-design`` in a subprocess — the ONE implementation of the
    ``$CHIPPY_ACTIVE_DESIGN`` → ``active.txt`` → registry precedence — never by
    a second copy of that precedence here. The script's own docstring records
    where two implementations of it already disagreed once.
    """
    if os.environ.get(_LINE_HOME_ENV, "").strip():
        return None, None  # the operator declared the tree; inherit it

    if design is None:
        res = _run_whole_log_json(
            "qleap_line_status", _cli_argv(script, ["--resolve-design"]),
            timeout_s=120)
        parsed = res.get("parsed")
        resolved = parsed.get("design_id") if isinstance(parsed, dict) else None
        if not (isinstance(resolved, str) and resolved):
            res["ok"] = False
            res["error"] = (
                "could not resolve the active design (line_status.py "
                "--resolve-design), so the record tree cannot be derived — "
                f"set ${_LINE_HOME_ENV} or pass design explicitly")
            return None, res
        design = resolved

    # The resolved id is joined into a filesystem path, so it passes the same
    # containment check a model-supplied one does. active.txt is hand-edited;
    # a malformed line must refuse here, not escape the _line tree.
    try:
        design = _safe_segment("design", design)
    except _ScopeRefused as exc:
        return None, {"ok": False, "error": str(exc)}

    env = dict(os.environ)
    env[_LINE_HOME_ENV] = str(_repo_root() / "simulations" / "_line" / design)
    return env, None


def qleap_line_status(what: str = "status", design: str | None = None) -> Dict[str, Any]:
    """The NEW line's state, read from the record tree the line writes.

    Wraps ``QubitDesignPipeline/NewPipeline/tools/line_status.py`` — the same
    ``linespec.serve`` adapters the web UI renders, emitting one frozen
    ``/api/factory/*`` payload as JSON. This is the read half of
    ``qleap_line_execute``: that tool spends solver time on the new line, this
    one answers how it went, and until it existed an agent that had just
    launched the line had NO tool that could read the result —
    ``qleap_factory_status`` reads the OLD control plane at
    ``simulations/_factory/``, while the new line records into
    ``simulations/_line/<design>/``, so the launcher's own status question came
    back "nothing happened". The exact tool-gap species this module's docstring
    opens with, one line over.

    ``what``   which payload: ``"status"`` (cross-phase rollup: accepted
               scopes, andons, embargoes), ``"line"`` (the discovered block DAG
               — reads no records, so it answers before a first run), or
               ``"steps"`` (the DAG joined against the records: per-step
               verdicts, gate reports, artifacts, durations).
    ``design`` a design id like ``hex_low_freq_v2``; omit for the active design
               (``$CHIPPY_ACTIVE_DESIGN``, then ``active.txt``, then the
               registry default — resolved by the script, the one
               implementation of that precedence).

    The record tree is ``$CHIPPY_LINE_HOME`` when this server's environment
    declares it; otherwise ``simulations/_line/<design>`` — the landing place
    ``execute_line.py`` anchors runs at. See :func:`_line_home_env`.

    Pure read: no solve, no COMSOL slot, and no ``dry_run`` by design. Exit
    codes are process health, never a physics verdict (I4): with ``returncode``
    0 ``parsed`` is the payload; with 1 it is the script's stated refusal
    (``{code, message}``); ``parsed`` null never means "clean" — read
    ``parse_error`` for why. The parse reads the COMPLETE log, never a tail
    (:func:`_run_whole_log_json`).
    """
    script = _new_line_tools() / "line_status.py"
    if not script.is_file():
        return {"ok": False, "error": f"no line status tool at {_display(script)}"}

    if what not in _LINE_STATUS_WHAT:
        return {"ok": False,
                "error": f"what {what!r} is not a payload line_status.py "
                         f"emits. Choose one of: {list(_LINE_STATUS_WHAT)}"}
    if design is not None:
        try:
            design = _safe_segment("design", design)
        except _ScopeRefused as exc:
            return {"ok": False, "error": str(exc)}

    env, refusal = _line_home_env(design, script)
    if refusal is not None:
        return refusal

    # `--json` is already the default and the only stdout format; passed anyway
    # because the flag exists precisely so a caller can state the contract it
    # depends on (line_status.py's own help says so).
    args = ["--what", what, "--json"]
    if design:
        args += ["--design", design]

    # This interpreter, not `uv run` — the measured trap on `_cli_argv`. The
    # script needs only the stdlib plus linespec/qleapsim, which its own
    # `_bootstrap` puts on the path.
    return _run_whole_log_json(
        "qleap_line_status", _cli_argv(script, args),
        timeout_s=_LINE_STATUS_TIMEOUT_S, env=env)

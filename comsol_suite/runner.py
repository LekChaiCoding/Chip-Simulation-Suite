"""Subprocess execution helpers shared by all tool modules.

Three responsibilities live here:

1. :func:`patch_script` — produce a *temporary, path-redirected copy* of one of
   the upstream pipeline scripts. Several of those scripts hard-code absolute
   paths (e.g. a Linux ``/mnt/smb/...`` mount, or output folders inside the
   tracked ``JosephsonCircuit`` tree). Rather than modify the originals — which
   we treat as read-only, vertex-validated source of truth — we copy them and
   surgically rewrite only the specific assignment lines that point at those
   paths. The physics/geometry logic is byte-for-byte identical; only the I/O
   destinations change.

2. :func:`run_command` — a single hardened wrapper around
   :class:`subprocess.Popen` that streams combined stdout/stderr to a log file,
   enforces a timeout (against the child's whole process group, because the
   direct child is usually only ``uv`` — see :func:`kill_process_tree`), and
   never uses a shell string (args list only). :func:`new_log_path` names a log
   that belongs to one invocation, so two concurrent calls to the same tool
   cannot overwrite each other's only record, and prunes that tool's older run
   logs so per-invocation naming does not turn into an unbounded pile.
   :func:`extract_trailing_json` is the shared way a synchronous wrapper reads
   a machine-readable verdict back out of such a log.

3. The **timings ledger** (:func:`_append_timing` and its derivation helpers) —
   one appended JSONL line per completed run recording how long it took.

Keeping all process spawning in one place means there is exactly one code path
to audit for safety, logging, and Windows/POSIX quirks. It is also why the
timings ledger lives here and nowhere else: every station of the F0-F3 factory
(CAD, COMSOL, fitting, ChipConstruction) reaches its real work through
:func:`run_command`, so this is the only place where "how long did that step
take" can be measured once instead of being re-implemented — or, worse, faked —
per tool. The web UI's Assembly pane shows a duration per step; before this
ledger existed the factory recorded no timings at all, so there was nothing
honest to show.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Script patching
# ─────────────────────────────────────────────────────────────────────────────
def patch_script(
    src: Path,
    dest: Path,
    replacements: Dict[str, str],
    *,
    require_all: bool = True,
) -> Path:
    """Copy ``src`` to ``dest`` with line-level regex substitutions applied.

    Parameters
    ----------
    src
        Path to the original (untouched) upstream script.
    dest
        Where to write the patched copy. Parent dirs are created.
    replacements
        Mapping of ``regex pattern`` -> ``replacement line``. Each pattern is
        matched against whole lines (``re.MULTILINE``); the *entire matched
        line* is replaced by the replacement string. Patterns should be anchored
        enough to be unambiguous (e.g. ``r"^OUT_GDS\\s*=.*$"``).
    require_all
        If True (default), raise :class:`ValueError` when any pattern fails to
        match — this turns "the upstream script was refactored and our patch no
        longer applies" into a loud, immediate error instead of a silent wrong
        result.

    Returns
    -------
    Path
        ``dest`` (for convenient chaining).
    """
    text = src.read_text(encoding="utf-8")
    unmatched: List[str] = []

    for pattern, replacement in replacements.items():
        new_text, n = re.subn(pattern, lambda _m, r=replacement: r,
                              text, flags=re.MULTILINE)
        if n == 0:
            unmatched.append(pattern)
        text = new_text

    if require_all and unmatched:
        raise ValueError(
            f"patch_script: these patterns did not match anything in {src.name} "
            f"(did the upstream script change?): {unmatched}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# Command result
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CommandResult:
    """Outcome of a :func:`run_command` call."""

    returncode: int
    log_path: Path
    duration_s: float
    timed_out: bool
    argv: List[str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def log_tail(self, n: int = 40) -> str:
        """Return the last ``n`` lines of the captured log (best effort)."""
        try:
            lines = self.log_path.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])


# ─────────────────────────────────────────────────────────────────────────────
# Timings ledger  (telemetry — NOT a signed factory record)
# ─────────────────────────────────────────────────────────────────────────────
# Deliberately unlike ``simulations/_factory/records/**/accepted.json`` and the
# factory event ledger: there is NO hash chain and no write-once rule here.
# A duration is an observation, not an assertion anybody signs, so paying for
# tamper-evidence would buy nothing and would make a concurrent append (see
# below) either impossible or fragile. Nothing in the factory may make a gate
# decision from this file.
_TIMINGS_LEDGER_RELPATH = ("simulations", "_factory", "timings.jsonl")

# Only these keys are written, and every consumer (``/api/factory/timings``)
# reads exactly them. Keeping the tuple next to the writer makes the contract
# greppable from both ends.
TIMING_FIELDS = ("ts", "tool", "script", "scope", "duration_s", "returncode")

# argv entries with one of these suffixes are "the script". Checked in argv
# order because every launch pattern in this suite puts the script before its
# own arguments — including the ``uv run --with mph <script.py> --flag`` form,
# where argv[0] is the launcher and the script is somewhere in the middle.
_SCRIPT_SUFFIXES = (".py", ".jl", ".m", ".sh")

# Directory of the ``comsol_suite`` package. The stack walk below only trusts
# frames from inside it: an outer frame belongs to FastMCP/uvicorn/threading,
# not to a tool.
_PACKAGE_DIR = Path(__file__).resolve().parent


def _timings_ledger_path() -> Path:
    """Absolute path of the timings ledger.

    Repo root comes from :func:`comsol_suite.config.load_config` — the suite's
    one and only path-resolution scheme (env > ``config/paths.toml`` > the
    ``.mcp.json`` marker walk-up), the same ``repo_root`` every campaign tool
    already derives its run dirs from. Deriving it any other way is how a
    status call once created a shadow ``simulations/`` tree a level above the
    checkout (see ``config.load_config``'s docstring).

    Imported lazily rather than at module import time for two reasons: importing
    ``config`` runs ``load_config()`` eagerly (it mkdirs ``runs/``), and a
    misconfigured environment must not be able to break importing this module —
    the caller of the ledger swallows the failure, but only if the import
    happens inside its ``try``.
    """
    from .config import load_config  # noqa: PLC0415 — see docstring

    return load_config().repo_root.joinpath(*_TIMINGS_LEDGER_RELPATH)


def _derive_script(argv: Sequence[str]) -> str:
    """Best-effort "which script ran" label from the argv list."""
    for arg in argv:
        text = str(arg)
        # An option that happens to carry a script-looking value
        # (``--checker=cad_verify_gds.py``) is an argument, not the program.
        if text.startswith("-"):
            continue
        if Path(text).suffix.lower() in _SCRIPT_SUFFIXES:
            return Path(text).name
    # No script-looking argument (e.g. ``python -c ...``): the program itself is
    # the most specific thing we honestly know.
    return Path(str(argv[0])).name if argv else ""


def _derive_tool_from_job(log_path: Path) -> Optional[str]:
    """MCP tool name read from the ``job.json`` beside a background run's log.

    :class:`comsol_suite.jobs.JobRegistry` writes ``runs/<job_id>/job.json``
    (carrying the exact ``tool`` string it was submitted with) *before* it
    starts the worker thread, and every background worker logs to
    ``runs/<job_id>/run.log``. So for the long solves — the ones whose duration
    anybody actually cares about — the tool name is available from a real file
    and needs no guessing. This is also the only mechanism that works there:
    the worker runs on a fresh thread whose stack contains no tool frame at all.
    """
    try:
        data = json.loads((log_path.parent / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool = data.get("tool") if isinstance(data, dict) else None
    return tool if isinstance(tool, str) and tool else None


def _derive_tool_from_stack(package_dir: Path = _PACKAGE_DIR,
                            max_frames: int = 60) -> Optional[str]:
    """MCP tool name taken from the outermost in-package caller frame.

    For a synchronous tool call the outermost frame inside ``comsol_suite`` is
    the ``@mcp.tool()``-decorated function in ``server.py``, whose Python name
    *is* the MCP tool name — so the label is derived from the live call, never
    from a table that could drift out of date as tools are added.

    Two filters make that precise:

    * **module-level only** — a frame counts only if its own module's globals
      bind its exact code object. That rejects nested closures such as
      ``_launch.<locals>.worker``, which would otherwise report every
      background COMSOL solve as the tool ``"worker"``.
    * **public only** — private helpers (``_run_sync``, ``jobs._run``) are
      plumbing, not tools.

    ``package_dir`` is a parameter purely so the mechanism is testable from a
    module outside the package; production callers use the default.
    """
    package_dir = package_dir.resolve()
    frame: Optional[FrameType] = sys._getframe(1)
    found: Optional[str] = None
    for _ in range(max_frames):
        if frame is None:
            break
        code = frame.f_code
        name = code.co_name
        # This module's own frames (``run_command`` is public, module-level and
        # in-package) are not tools; without this the fallback would proudly
        # report every background solve as the tool "run_command".
        is_self = frame.f_globals.get("__name__") == __name__
        if not is_self and not name.startswith("_"):
            try:
                in_package = Path(code.co_filename).resolve().is_relative_to(package_dir)
            except (OSError, ValueError):
                in_package = False
            # Identity, not name equality: proves this frame is the module-level
            # function `name` and not a same-named nested/lambda/method body.
            if in_package and getattr(frame.f_globals.get(name), "__code__", None) is code:
                found = name  # keep walking; the OUTERMOST match is the tool
        frame = frame.f_back
    return found


def _derive_scope(argv: Sequence[str]) -> Optional[str]:
    """Scope string in ``records.scope_key`` grammar (``TILE`` / ``TILE_L``).

    Read off the actual command line: ``--tile``/``--letter`` are the spelling
    every campaign script in this suite uses (``--scope`` wins if a script
    states it outright). Other spellings exist and are deliberately NOT guessed
    at — ``qleap_cs002`` passes the *tile* as ``--unit`` while ``qleap.py``'s
    ``--unit``/``--row`` are two halves of one tile — because a wrong scope is
    worse than a null one. A caller that knows better passes ``scope=``.
    """
    flags: Dict[str, str] = {}
    wanted = ("--scope", "--tile", "--letter")
    argv = [str(a) for a in argv]
    for index, arg in enumerate(argv):
        for flag in wanted:
            if arg == flag and index + 1 < len(argv):
                flags.setdefault(flag, argv[index + 1])
            elif arg.startswith(flag + "="):
                flags.setdefault(flag, arg[len(flag) + 1:])
    if flags.get("--scope"):
        return flags["--scope"]
    tile = flags.get("--tile")
    if not tile or tile.startswith("-"):
        return None
    letter = flags.get("--letter")
    return f"{tile}_{letter}" if letter and not letter.startswith("-") else tile


#: Prefix for a run nobody could attribute to an MCP tool. A colon cannot appear
#: in a Python identifier, so this can never collide with a real `@mcp.tool()`
#: name — the ledger's reader joins on exact tool name, so an unattributed row
#: simply matches no station instead of being mistaken for one.
UNATTRIBUTED_PREFIX = "unattributed:"


def _attributed_tool(argv: Sequence[str], log_path: Path, tool: Optional[str]) -> str:
    """Always a string, so the reader never sees this row as corruption.

    The three real signals, in order of trustworthiness: an explicit `tool=`
    kwarg, the job registry's own record, then the outermost in-package caller
    frame. All three can legitimately come up empty — a script run straight from
    a shell has no MCP tool above it at all — and the reader's contract treats a
    non-string `tool` as a torn line and counts it in `skipped`. A run that
    genuinely had no tool is not corruption, so name it after its script and say
    plainly that nobody claimed it.
    """
    attributed = tool or _derive_tool_from_job(log_path) or _derive_tool_from_stack()
    if attributed:
        return attributed
    return f"{UNATTRIBUTED_PREFIX}{Path(_derive_script(argv)).stem or 'unknown'}"


def _append_timing(
    *,
    argv: Sequence[str],
    log_path: Path,
    duration_s: float,
    returncode: int,
    tool: Optional[str],
    scope: Optional[str],
) -> None:
    """Append one line to the timings ledger. Never raises. Never blocks a run.

    Failures (returncode != 0, and timeouts, which arrive here as the kill
    signal's negative returncode) are recorded exactly like successes: when a
    gate stops the line, "how long until it failed" is the number the human is
    looking for.
    """
    try:
        record: Dict[str, Any] = {
            # Completion instant, not start — the start is recoverable as
            # ts - duration_s, and "when did this finish" is what the UI orders
            # rows by. Milliseconds are enough for a step measured in seconds.
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": _attributed_tool(argv, log_path, tool),
            "script": _derive_script(argv),
            "scope": scope if scope else _derive_scope(argv),
            "duration_s": round(float(duration_s), 2),
            "returncode": int(returncode),
        }
        line = json.dumps({key: record[key] for key in TIMING_FIELDS},
                          ensure_ascii=True) + "\n"
        path = _timings_ledger_path()
        # This module is importable (and runnable) against a checkout whose
        # factory dir has not been created yet, e.g. a fresh clone or a test
        # root — so create the parent instead of dropping the measurement.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode + ONE write() of ONE complete line: several campaigns run
        # in parallel on this machine, and O_APPEND means each of them lands at
        # the current end of file. A single buffered write smaller than the
        # 8 KiB buffer is flushed as a single syscall, so a concurrent appender
        # cannot interleave half a record. (The repo lives on an SMB mount whose
        # append atomicity is weaker than a local fs; a torn line would show up
        # as one skipped line in /api/factory/timings, which is exactly why that
        # endpoint counts skips instead of failing.)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as error:  # noqa: BLE001 — telemetry must never fail a run
        # Unwritable dir, full disk, read-only mount, misconfigured repo root:
        # all of it is noted on stderr (which lands in the MCP server log) and
        # then dropped. A telemetry write that kills a 40-minute solve is a bug,
        # not a feature.
        print(f"[runner] WARNING: could not append to timings ledger: "
              f"{type(error).__name__}: {error}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Per-invocation log paths
# ─────────────────────────────────────────────────────────────────────────────
#: Human-facing "newest run of this tool" copy kept beside the real logs.
_LAST_SUFFIX = "_last.log"

#: How many run logs to keep per tool per directory. Per-invocation naming
#: trades "two calls clobber each other" for "the directory grows forever", and
#: only the second half of that trade is optional. Deep enough that a 24-letter
#: fleet rollout can still be read back afterwards, shallow enough that the
#: directory stays listable on the SMB share.
_LOG_RETENTION_PER_TOOL = 32


def _run_log_pattern(tool: str) -> re.Pattern[str]:
    """Matches ONLY the names :func:`new_log_path` mints for ``tool``.

    Anchored on the full stamp/pid/hex shape rather than a ``<tool>_*.log``
    glob, because the retention sweep deletes what this matches and the log
    directories are shared with files nobody here created:
    ``simulations/ChipConstruction/logs/`` holds git-TRACKED ``.out``/``.err``
    transcripts from the mask campaigns (29 of them today), plus the
    ``<tool>_last.log`` copy this module maintains. None of those can match
    this, so the sweep cannot reach them however the tool is named.
    """
    return re.compile(rf"^{re.escape(tool)}_\d{{8}}T\d{{6}}Z_\d+_[0-9a-f]{{6}}\.log$")


def _prune_run_logs(log_dir: Path, tool: str,
                    keep: int = _LOG_RETENTION_PER_TOOL) -> int:
    """Delete all but the ``keep`` newest run logs for ``tool``. Best effort.

    Newest by mtime, with the filename as the tie-break. Every failure mode
    here (a racing sweep in another server process that unlinked the same file
    first, a read-only mount, an SMB hiccup) is silently ignored — a log that
    could not be tidied away is not a reason to fail the run it belongs to, and
    the next call sweeps again anyway.
    """
    pattern = _run_log_pattern(tool)
    try:
        candidates = [p for p in log_dir.iterdir()
                      if p.is_file() and pattern.match(p.name)]
    except OSError:
        return 0  # directory not created yet, or unlistable: nothing to prune
    if len(candidates) <= keep:
        return 0

    def _age_key(path: Path) -> Any:
        try:
            mtime = path.stat().st_mtime
        except OSError:  # vanished under a concurrent sweep: sort it first
            mtime = 0.0
        # Name breaks mtime ties so the sort is total and a sweep is
        # reproducible. It does NOT break them chronologically: the stamp
        # leading the name is second-resolution, and what follows it is pid
        # then random hex, so two logs minted in the same second order by pid,
        # which is arbitrary. Fine for retention — one of two same-second logs
        # is as good a victim as the other — but do not read this ordering as
        # "oldest first" below one-second granularity.
        return (mtime, path.name)

    candidates.sort(key=_age_key)
    removed = 0
    for stale in candidates[:len(candidates) - keep]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def new_log_path(log_dir: Path, tool: str) -> Path:
    """A log path that belongs to THIS invocation and no other.

    The synchronous wrappers used to hand ``run_command`` one fixed path per
    tool (``logs/<tool>_last.log``), opened with mode ``"w"``. Two concurrent
    calls to the same tool therefore shared their only result channel: the
    second call truncated the first's log out from under it, and because these
    wrappers read their verdict back OUT of that file, the first call could
    return the second call's gate report as its own. Nothing else in the repo
    reads the fixed name (grepped: only the three ``_run_sync``-style helpers
    wrote it), so uniqueness costs nothing but a filename.

    UTC stamp for human ordering, pid + random suffix for actual uniqueness —
    the stamp alone collides when a fast tool is called twice in one second,
    and pid alone collides across the several MCP server processes (one per
    client) that share this tree.

    **Run logs stay beside the campaign they document** (``<campaign>/logs/``),
    which is where a human already looks and where the ``.out``/``.err``
    transcripts of previous campaigns live. That is only safe because they are
    version-control noise by rule, not by luck: the qleap repo's ``.gitignore``
    carries a bare ``*.log``, so ``git check-ignore`` answers rc=0 for every
    name minted here (checked for ``simulations/ChipConstruction/logs/`` and
    ``simulations/_factory/logs/``) and none of them can surface in the
    ``git status`` the push discipline reads. Keep that true if these ever move:
    a run log that is not ignored is a run log somebody has to explain before
    every push. What version control will NOT do is bound them, so this call
    also SWEEPS: see :func:`_prune_run_logs`, which is deliberately blind to
    anything it did not name itself.
    """
    _prune_run_logs(log_dir, tool)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{tool}_{stamp}_{os.getpid()}_{uuid.uuid4().hex[:6]}.log"


def update_last_log_pointer(log_path: Path, tool: str) -> Optional[Path]:
    """Refresh ``<tool>_last.log`` to be a copy of ``log_path``. Best effort.

    Purely a convenience for a human tailing a known filename — the returned
    result carries the real ``log_path``, and no code reads this copy. Written
    to a temp name and renamed so that two runs finishing together leave one
    intact log rather than two interleaved halves. Never raises: losing the
    convenience copy must not fail a run that actually completed.

    The staging name carries a uuid for the same reason :func:`new_log_path`
    does, and it is not optional here. The concurrency this function exists to
    survive is two calls to ONE tool inside ONE server process — same pid — so
    a pid-only staging name is the same name twice: both copies truncate and
    write it at overlapping offsets, ``os.replace`` moves whatever mixture
    exists at that instant, and the loser raises ``FileNotFoundError`` on a
    path the winner already renamed away. Measured, before the uuid: a 40 MB
    and a 4 MB log left a 40000001-byte ``<tool>_last.log`` holding 4 MB of one
    followed by 36 MB of the other.
    """
    target = log_path.parent / f"{tool}{_LAST_SUFFIX}"
    tmp = (log_path.parent /
           f".{tool}{_LAST_SUFFIX}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(log_path, tmp)  # streamed: a solve log can be large
        os.replace(tmp, target)
        return target
    except OSError as error:
        print(f"[runner] WARNING: could not update {tool}{_LAST_SUFFIX}: "
              f"{type(error).__name__}: {error}", file=sys.stderr)
        return None
    finally:
        # A half-written staging file must not outlive the attempt. Unlike the
        # run logs it is not a ``*.log``, so nothing ignores it and it WOULD
        # show up untracked in ``git status``.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Reading a verdict back out of a log
# ─────────────────────────────────────────────────────────────────────────────
def extract_trailing_json(
    text: str, *, tail_lines: Optional[int] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Parse the LAST complete top-level JSON value in ``text``.

    Returns ``(value, None)`` on success and ``(None, reason)`` when there is
    nothing parseable — the caller surfaces the reason so a missing verdict
    reads as "could not parse", not as "no verdict".

    The scan is forward, using :meth:`json.JSONDecoder.raw_decode` at each
    candidate ``{``/``[``: a decode that succeeds consumes the whole value, so
    the walk steps OVER nested braces and only ever records top-level objects,
    and trailing prose after the report no longer poisons it. Last-wins because
    the scripts that print several reports chain sub-steps, and the chain's
    verdict is the one at the end.

    It lives here, next to :func:`new_log_path`, because every synchronous
    wrapper in the suite gets its verdict the same way — the tool prints JSON,
    ``run_command`` captures it to a file, the wrapper parses the tail. Two
    copies of that parse existed and only one of them was ever fixed; the
    unfixed copy (``qleap_factory``) went on json.loads-ing from the FIRST
    ``{`` to end-of-text, so a script that printed one more line after its
    report parsed as "Extra data" and yielded a null verdict, and brace-bearing
    prose earlier in the log made it guess again from the wrong offset.

    ``tail_lines`` is only for the error text: it lets the message name how much
    of the log was actually looked at, which is the first thing someone asks.
    """
    where = (f"the last {tail_lines} log lines" if tail_lines is not None
             else "the log tail")
    decoder = json.JSONDecoder()
    found: Optional[Any] = None
    saw_candidate = False
    index = 0
    while index < len(text):
        start = min((p for p in (text.find("{", index), text.find("[", index))
                     if p != -1), default=-1)
        if start == -1:
            break
        saw_candidate = True
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1  # prose, a Python repr, a truncated head: skip it
            continue
        found = value  # keep walking; the LAST top-level value is the report
        index = end
    if found is not None:
        return found, None
    if not text.strip():
        return None, "script produced no output to parse"
    if saw_candidate:
        return None, (f"no complete JSON object in {where} "
                      f"(braces seen but none parsed) — see log_tail")
    return None, f"no JSON object found in {where} — see log_tail"


# ─────────────────────────────────────────────────────────────────────────────
# Command execution
# ─────────────────────────────────────────────────────────────────────────────
#: POSIX gives us sessions and process groups; Windows gives us neither, so
#: there the best available answer is still "kill the direct child".
_POSIX = os.name == "posix"

#: How long a timed-out tree gets to honour SIGTERM before SIGKILL. Long enough
#: for a campaign script's ``finally`` to disconnect its COMSOL client (which is
#: what actually returns the licence), short enough that a wall-clock timeout is
#: still a wall-clock timeout.
_TERM_GRACE_S = 5.0


def kill_process_tree(proc: "subprocess.Popen[Any]", *,
                      grace_s: float = _TERM_GRACE_S) -> str:
    """Kill ``proc``'s whole process GROUP. Returns a one-line summary.

    Not "everything it spawned", which is the stronger claim it is tempting to
    make: a descendant that calls ``setsid``/``setpgrp`` leaves the group and
    outlives this. Nothing ``run_command`` launches does — the COMSOL server is
    an external prerequisite started by hand, not a child (see
    ``qleap_chipconstruction.qleap_chipconstruction_mph_preflight``) — so the
    group is the whole tree in practice, but only in practice.

    Why the group and not ``proc.kill()``: every campaign tool is launched as
    ``uv run --no-project ... python <script>.py``, so the direct child is
    ``uv`` and the COMSOL-touching interpreter is a GRANDCHILD. Killing only
    the direct child declared the job failed while the grandchild lived on
    holding a COMSOL slot and a licence — the exact outcome
    ``simulations/_framework/PLAYBOOK.md`` section 2 rules out ("wall-clock
    timeouts kill the whole process group"). Slots are the scarce resource on
    this host; a phantom holder blocks every later solve until someone notices.

    SIGTERM first, so a script that traps it can close its COMSOL client and
    release the licence deliberately; SIGKILL after the grace period for
    whatever ignored it. The group gets the second signal even when the direct
    child has already exited, because ``uv`` exiting says nothing at all about
    the python running underneath it.
    """
    if not _POSIX:  # pragma: no cover - POSIX-only host
        proc.kill()
        proc.wait()
        return f"pid {proc.pid} (no POSIX process groups on this platform)"

    pgid: Optional[int] = None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None  # already reaped; only the direct-child signal is left
    if pgid is not None and pgid == os.getpgrp():
        # Refuse to signal our OWN group. If ``start_new_session`` ever stops
        # taking effect, killpg here would take down the MCP server (or the
        # detached job worker) together with the child it was policing.
        pgid = None

    def _send(sig: int) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except OSError:
                pass  # group already gone — fall through to the child itself
        try:
            proc.send_signal(sig)
        except OSError:  # ProcessLookupError and friends: it is already dead
            pass

    _send(signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    _send(signal.SIGKILL)
    proc.wait()
    return (f"process group {pgid} (SIGTERM, then SIGKILL)" if pgid is not None
            else f"pid {proc.pid} only (no separate process group)")


def run_command(
    argv: Sequence[str],
    log_path: Path,
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_s: Optional[float] = None,
    debug: bool = False,
    tool: Optional[str] = None,
    scope: Optional[str] = None,
    record_timing: bool = True,
) -> CommandResult:
    """Run ``argv`` to completion, streaming output to ``log_path``.

    The command is always invoked as an argument *list* (never a shell string),
    so there is no shell-injection surface and no cross-platform quoting mess.

    Parameters
    ----------
    argv
        Program and arguments, e.g. ``[python_bin, "script.py", "--flag"]``.
    log_path
        File to receive the merged stdout+stderr stream (created/overwritten).
    cwd
        Working directory for the child process.
    env
        Full environment for the child (``None`` inherits the parent's).
    timeout_s
        Kill the process after this many seconds; ``None`` waits indefinitely.
    debug
        When True, the exact argv and cwd are written to the top of the log.
    tool
        MCP tool name for the timings ledger. Optional: when omitted it is
        derived from the run's ``job.json`` or the calling stack (see
        :func:`_derive_tool_from_job` / :func:`_derive_tool_from_stack`), so no
        caller has to be edited for the ledger to start carrying real names.
        Pass it when the caller knows better than the derivation.
    scope
        Factory scope (``"chip"`` / ``"<TILE>"`` / ``"<TILE>_<L>"``) for the
        ledger; derived from ``argv`` when omitted.
    record_timing
        Set False to skip the ledger append for a run whose duration is
        meaningless (a dry-run probe, a health check).
    """
    argv = [str(a) for a in argv]
    log_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    timed_out = False

    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        if debug:
            log.write(f"[runner] cwd  = {cwd}\n")
            log.write(f"[runner] argv = {argv}\n")
            log.write("[runner] ---- begin output ----\n")
            log.flush()

        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            # Own session (POSIX) => own process group, so the timeout below
            # can take the whole ``uv -> python -> COMSOL`` tree down as one
            # unit. See :func:`kill_process_tree`. The child's inherited stdout
            # is unaffected, so log streaming is exactly as before.
            start_new_session=_POSIX,
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            killed = kill_process_tree(proc)
            log.write(f"\n[runner] TIMEOUT after {timeout_s}s — killed {killed}\n")
        except BaseException:
            # Anything that UNWINDS this frame while a child is running:
            # KeyboardInterrupt, SystemExit, a cancellation raised into this
            # thread. ``start_new_session`` above put the child in its own
            # session, so the terminal's SIGINT no longer reaches it and an
            # interrupted run would otherwise leave the grandchild — and its
            # COMSOL slot — behind. Tear it down, then let the exception
            # continue on its way.
            #
            # It is NOT a teardown guarantee for the process being killed.
            # Python's default SIGTERM disposition ends the interpreter without
            # raising, and this package installs no ``signal.signal`` handler
            # and no ``atexit`` hook, so a supervisor's ``kill -TERM`` runs none
            # of this. Worse, the same ``start_new_session`` that makes the
            # timeout able to kill the tree also takes the child OUT of the
            # server's process group, so a group-directed teardown no longer
            # reaches it either.
            #
            # So state the residual honestly rather than talk it away: if this
            # process is SIGKILLed, or SIGTERMed with the default disposition,
            # a COMSOL child started here is orphaned and keeps its licence
            # until its own deadline. That is not hypothetical for long runs —
            # ``jobs.job_runner`` calls this function for exactly those, so the
            # "only short gdstk scripts come through here" reassurance an
            # earlier version of this comment offered was simply untrue.
            #
            # What bounds the damage is the worker being a SEPARATE process
            # (``jobs.submit_detached``), so killing the MCP server does not
            # reach it at all, and every command carrying its own ``timeout_s``,
            # after which the group is killed. Nobody has closed the
            # kill -9-the-worker case, and a handler here could not:
            # ``signal.signal`` only works on the main thread and this runs on
            # job-worker threads.
            kill_process_tree(proc)
            raise

    result = CommandResult(
        returncode=proc.returncode,
        log_path=log_path,
        duration_s=time.time() - start,
        timed_out=timed_out,
        argv=argv,
    )
    # After the log handle is closed and the duration is final, so a ledger line
    # always describes a run that is genuinely over. A timeout is not special-
    # cased: the kill leaves a negative returncode, which reads as the failure it
    # is, and the duration is the timeout budget the human wanted to see.
    if record_timing:
        _append_timing(
            argv=argv,
            log_path=log_path,
            duration_s=result.duration_s,
            returncode=result.returncode,
            tool=tool,
            scope=scope,
        )
    return result

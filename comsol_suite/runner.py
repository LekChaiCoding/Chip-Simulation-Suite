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
   enforces a timeout, and never uses a shell string (args list only).

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
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Dict, List, Optional, Sequence


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
# Command execution
# ─────────────────────────────────────────────────────────────────────────────
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
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
            log.write(f"\n[runner] TIMEOUT after {timeout_s}s — process killed\n")

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

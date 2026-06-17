"""Subprocess execution helpers shared by all tool modules.

Two responsibilities live here:

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

Keeping all process spawning in one place means there is exactly one code path
to audit for safety, logging, and Windows/POSIX quirks.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


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
# Command execution
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


def run_command(
    argv: Sequence[str],
    log_path: Path,
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_s: Optional[float] = None,
    debug: bool = False,
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

    return CommandResult(
        returncode=proc.returncode,
        log_path=log_path,
        duration_s=time.time() - start,
        timed_out=timed_out,
        argv=argv,
    )

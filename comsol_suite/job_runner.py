"""Out-of-process worker for a background job.

Invoked as ``python -m comsol_suite.job_runner <run_dir>``, detached from the
MCP server that spawned it.

Why this exists
---------------
Background work used to run on a **daemon thread inside the MCP server**
(``JobRegistry.submit(background=True)``). That server is spawned *per MCP
client* and is short-lived, and a daemon thread dies with its process — so when
the client went away, the solve went with it. Measured on a preflight: 20 s in,
the record was stuck at ``"running"`` with an empty ``run.log``, and the next
poll reported ``interrupted: MCP server restarted mid-run``. An NT2 notch sweep
is hours; losing one at the end costs the whole solve.

The fix is to make the worker a real process in its own session, so nothing that
happens to the server can reach it. Polling already worked across processes —
``JobRegistry.get()`` falls back to reading ``job.json`` from disk — so the only
thing missing was a worker that outlives the server and keeps that file honest.

The contract, both directions
-----------------------------
The spawning side writes ``<run_dir>/spec.json`` and starts this module. This
module owns ``<run_dir>/job.json`` from that moment on and writes it exactly
twice: once on entry (``running``, with its own pid) and once on exit
(``completed``/``failed`` with the result). Every write is atomic, because a
half-written ``job.json`` on this SMB share is a job nobody can ever poll again.

Anything this process prints before it takes ownership of the log goes to
``<run_dir>/runner.err`` — an import error or a bad spec would otherwise be
invisible, which is the failure mode this whole module exists to remove.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

# Absolute imports: this module runs as ``python -m comsol_suite.job_runner`` in
# a fresh interpreter, not as part of the server's import graph.
from comsol_suite.jobs import read_job_json, write_job_json
from comsol_suite.runner import run_command

#: Spec keys this runner understands. A spec carrying anything else is refused
#: rather than silently ignored — a typo'd key would otherwise mean a collect
#: pattern or a timeout that quietly did not apply.
SPEC_KEYS = frozenset({
    "tool", "argv", "cwd", "timeout_s", "debug",
    "collect_dir", "collect_patterns", "extra_files",
    "ok_returncodes", "log_tail_lines",
    "post", "post_kwargs",
})


def apply_post(spec: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Hand the raw result to the launching tool's own post-processor.

    Several tools return more than "it ran": ``validate_geometry`` reports which
    checker ran and where its report landed, and ``run_parameter_inversion``
    root-finds over the sweep CSV the solve produced. That work is code, not
    configuration, so instead of growing the spec a hundred flags the spec names
    a ``"module:function"`` and the worker calls it here — in the detached
    process, where the artifacts actually are.

    A post-processor that raises does NOT lose the solve: the raw result is kept
    and the failure is reported alongside it. Hours of COMSOL time must never be
    thrown away by a formatting bug.
    """
    ref = spec.get("post")
    if not ref:
        return result
    try:
        mod_name, _, fn_name = ref.partition(":")
        module = importlib.import_module(mod_name)
        fn = getattr(module, fn_name)
        return fn(result, spec, **(spec.get("post_kwargs") or {}))
    except Exception as exc:  # noqa: BLE001
        result = dict(result)
        result["post_error"] = f"{ref} raised {type(exc).__name__}: {exc}"
        result["post_traceback"] = traceback.format_exc()
        return result


def load_spec(run_dir: Path) -> Dict[str, Any]:
    """Read and validate ``<run_dir>/spec.json``."""
    spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("spec.json is not an object")
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise ValueError(f"spec.json has unknown keys: {sorted(unknown)}")
    for required in ("tool", "argv"):
        if not spec.get(required):
            raise ValueError(f"spec.json is missing {required!r}")
    return spec


def collect_outputs(spec: Dict[str, Any]) -> List[str]:
    """Gather the artifact paths the launching tool asked for.

    Best effort by design: a missing collect dir means the run produced nothing
    to collect, which is information, not an error. Raising here would turn a
    completed solve into a failed job.
    """
    files: List[str] = []
    collect_dir = spec.get("collect_dir")
    if collect_dir:
        base = Path(collect_dir)
        if base.is_dir():
            for pat in spec.get("collect_patterns") or ():
                files.extend(str(p) for p in base.rglob(pat))
    for extra in spec.get("extra_files") or ():
        if Path(extra).is_file():
            files.append(str(extra))
    return sorted(set(files))


def execute(run_dir: Path) -> int:
    """Run the job described by ``run_dir/spec.json``. Returns a process exit code."""
    spec = load_spec(run_dir)
    tool = spec["tool"]
    job = read_job_json(run_dir / "job.json") or {}

    job.update(status="running", started_at=time.time(), pid=os.getpid())
    write_job_json(run_dir / "job.json", job)

    log_path = Path(job.get("log_path") or (run_dir / "run.log"))
    # Return codes that mean "ran to completion". Some gates carry a verdict in
    # the exit code (rc 2 = FAIL, rc 3 = unverified) rather than signalling a
    # crash, so the launching tool declares which ones are not failures.
    ok_rcs = tuple(spec.get("ok_returncodes") or (0,))

    res = run_command(
        spec["argv"],
        log_path=log_path,
        cwd=Path(spec["cwd"]) if spec.get("cwd") else None,
        timeout_s=spec.get("timeout_s"),
        debug=bool(spec.get("debug")),
        tool=tool,
    )
    ok = (res.returncode in ok_rcs) and not res.timed_out

    result = apply_post(spec, {
        "ok": ok,
        "returncode": res.returncode,
        # Surfaced separately from ``ok``: a wall-clock kill and a genuine
        # non-zero exit are different diagnoses, and the caller could not
        # previously tell them apart.
        "timed_out": res.timed_out,
        "duration_s": round(res.duration_s, 2),
        "output_files": collect_outputs(spec),
        "log_tail": res.log_tail(int(spec.get("log_tail_lines") or 30)),
        "summary": (f"{tool} timed out after {spec.get('timeout_s')}s"
                    if res.timed_out else f"{tool} finished rc={res.returncode}"),
        "error": None if ok else f"{tool} failed (see run.log)",
    })
    # A post-processor may downgrade the verdict (a solve that ran cleanly but
    # produced an unusable answer is not a success), but it may not upgrade one:
    # the process either ran to completion or it did not, and that is measured,
    # not opinion.
    ok = ok and bool(result.get("ok", True))
    result["ok"] = ok

    job.update(
        status="completed" if ok else "failed",
        finished_at=time.time(),
        result=result,
        error=result.get("error") if not ok else None,
    )
    write_job_json(run_dir / "job.json", job)
    return 0 if ok else 1


def main(argv: List[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m comsol_suite.job_runner <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(args[0])
    try:
        return execute(run_dir)
    except Exception as exc:  # noqa: BLE001 — the whole point is to record it
        # The job must never be left at "running" because this process blew up.
        # A stuck record is exactly what the daemon-thread bug produced, and it
        # is unrecoverable: nothing else knows the work is dead.
        detail = traceback.format_exc()
        try:
            (run_dir / "runner.err").write_text(detail, encoding="utf-8")
        except OSError:
            pass
        try:
            job = read_job_json(run_dir / "job.json") or {}
            job.update(status="failed", finished_at=time.time(),
                       error=f"job_runner: {type(exc).__name__}: {exc}")
            write_job_json(run_dir / "job.json", job)
        except Exception:  # noqa: BLE001 — nothing left to do but exit loudly
            pass
        print(detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

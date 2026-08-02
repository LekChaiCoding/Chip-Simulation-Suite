"""Background job registry for long-running pipeline stages.

COMSOL solves take hours and even a full ABCD fit takes a little while, so the
MCP tools that launch them must not block the server. The pattern is:

    tool call  ->  registry.submit_detached(...)  ->  {"job_id": ...} immediately
    (work runs in a DETACHED PROCESS, writing to runs/<job_id>/)
    later  ->  get_job_status(job_id) / get_job_result(job_id)

Each job owns a directory ``runs/<job_id>/`` containing:

    spec.json   - what to run (argv, cwd, timeout, what to collect)
    job.json    - serialised :class:`Job` metadata (status, timing, result, pid)
    run.log     - merged stdout/stderr of the wrapped subprocess
    runner.err  - only if the worker process itself failed to start

Because the metadata is persisted to ``job.json`` on every state change, the
registry rehydrates previous jobs on startup, so ``list_jobs`` / status queries
keep working across MCP-server restarts.

Threads vs processes
--------------------
``submit(background=True)`` runs the worker on a **daemon thread in this
server**, and this server is spawned *per MCP client* and is short-lived: when
the client goes away the thread dies with it, mid-solve, and the record is left
stuck at ``"running"`` forever. That is a real, measured failure, not a
hypothetical — see :mod:`comsol_suite.job_runner`.

So anything that can outlive a client must go through
:meth:`JobRegistry.submit_detached`, which starts a worker in its own session.
``submit`` is kept for short in-process work and for tests, which use
``background=False`` for determinism.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


# A job worker receives its own Job (for run_dir / log_path) and returns a
# JSON-serialisable result dict (typically {"summary": ..., "output_files": [...]}).
JobFn = Callable[["Job"], Dict[str, Any]]

#: Fields that survive a round trip through ``job.json``. Kept in one place
#: because three readers reconstruct a :class:`Job` from disk and a field
#: missing from any one of them is silently dropped.
JOB_FIELDS = ("job_id", "tool", "status", "created_at", "started_at",
              "finished_at", "run_dir", "log_path", "result", "error", "pid")

#: Written by the spawning process, read by everyone. Separate from job.json
#: because job.json has exactly one writer — the worker — from the spawn on.
WORKER_PID_FILE = "worker.pid"

#: How long a detached job may sit with no pid file before we call the spawn
#: dead. Generous: the parent writes the file microseconds after Popen returns,
#: so anything past this is a worker that never came up.
WORKER_STARTUP_GRACE_S = 60.0


def _read_pid_file(run_dir: Path) -> Optional[int]:
    try:
        return int((run_dir / WORKER_PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_job_json(path: Path, data: Dict[str, Any]) -> None:
    """Write ``job.json`` atomically: temp file in the same dir, then os.replace.

    A plain ``write_text`` here produced a ZERO-BYTE job.json on the SMB share
    this repo lives on, which then made the job unrecoverable in both
    directions: ``get()`` could not find it, and ``_rehydrate`` skipped it
    because empty text is not parseable JSON. That is the failure PLAYBOOK.md
    section 3 already warns about ("always atomic_json; plain writes are
    observed half-written by concurrent readers") — the job registry simply was
    not following it. ``os.replace`` is atomic, so a reader sees either the
    previous complete file or the new one, never a truncated one.

    Module-level rather than a method because the detached worker
    (:mod:`comsol_suite.job_runner`) owns the same file from another process and
    must write it exactly the same way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_job_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read ``job.json``, or None if it is absent, empty or unparseable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def worker_alive(pid: Optional[int], run_dir: str) -> bool:
    """Is the detached worker for ``run_dir`` still running under ``pid``?

    Identity, not just liveness. ``os.kill(pid, 0)`` alone would call a recycled
    pid alive and leave a dead job reported as running forever, so on Linux we
    confirm the process is actually *our* worker by looking for the run dir in
    its argv. The ``/proc`` read is the authority; the signal probe is only the
    fallback for a kernel without procfs.
    """
    if not pid:
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        argv = cmdline.read_bytes().decode("utf-8", "replace")
    except OSError:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    return "comsol_suite.job_runner" in argv and str(run_dir) in argv


@dataclass
class Job:
    """One unit of background work and its lifecycle metadata."""

    job_id: str
    tool: str
    status: str = "pending"            # pending | running | completed | failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    run_dir: str = ""
    log_path: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    # Detached-worker pid. None for in-process jobs. This is what lets a later
    # server tell "still solving" apart from "died without writing a result".
    pid: Optional[int] = None

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return round(end - start, 2)

    def to_public(self) -> Dict[str, Any]:
        """Trimmed, JSON-friendly view returned to MCP callers."""
        d = asdict(self)
        d["elapsed_s"] = self.elapsed_s
        return d


class JobRegistry:
    """Thread-safe registry that runs :class:`Job` workers in the background."""

    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = Path(runs_dir)
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._rehydrate()

    # -- persistence -----------------------------------------------------------
    def _job_json(self, job_id: str) -> Path:
        return self._runs_dir / job_id / "job.json"

    def _persist(self, job: Job) -> None:
        """Persist this job's metadata (see :func:`write_job_json`)."""
        write_job_json(self._job_json(job.job_id), job.to_public())

    @staticmethod
    def _reconcile_unfinished(data: Dict[str, Any], run_dir: Path, *,
                              at_startup: bool) -> Dict[str, Any]:
        """Decide what a record still marked ``pending``/``running`` really means.

        This used to be unconditional: every ``running`` record found at startup
        was rewritten to failed, on the reasoning that a job could not outlive
        the server that owned it. That reasoning was true of daemon threads and
        is false of detached workers — applied to one it would declare a live
        multi-hour solve dead and hand the caller a fabricated failure. So the
        worker's own liveness decides, and only a job that is provably gone
        fails.

        Three cases, in order:

        * no ``spec.json`` — a legacy in-process job. Whether that means "dead"
          depends entirely on ``at_startup``; see below.
        * a worker we can find — leave the record exactly as written.
        * a worker that is gone, or that never appeared within the startup
          grace — failed, and say which.

        ``at_startup`` is load-bearing, and getting it wrong was a real bug.
        "No spec.json, therefore the server that owned it is gone" is only true
        during :meth:`_rehydrate`, where by definition this process did not
        start the job. Applied on every poll it declares a *live* in-process job
        dead: the thread is still running, the record still says ``running``,
        and the caller is handed ``failed: interrupted: MCP server restarted
        mid-run`` — a failure that never happened, with a fabricated cause. The
        job's real result then lands on disk correctly while this process keeps
        serving the poisoned verdict from its cache. So on a live poll, an
        in-process record is reported exactly as written.
        """
        if data.get("status") not in ("pending", "running"):
            return data
        if not (run_dir / "spec.json").is_file():
            if data.get("status") != "running" or not at_startup:
                return data
            data = dict(data)
            data["status"] = "failed"
            data["error"] = "interrupted: MCP server restarted mid-run"
            return data

        pid = data.get("pid") or _read_pid_file(run_dir)
        if pid is None:
            # The parent writes the pid file immediately after spawning, so its
            # absence means either "spawned microseconds ago" or "the spawn
            # itself died". Age is the only thing that separates those.
            if time.time() - float(data.get("created_at") or 0) < WORKER_STARTUP_GRACE_S:
                return data
            data = dict(data)
            data["status"] = "failed"
            data["error"] = "worker process never started (no pid recorded)"
            return data

        if worker_alive(pid, str(run_dir)):
            data = dict(data)
            data["pid"] = pid
            return data

        data = dict(data)
        data["pid"] = pid
        data["status"] = "failed"
        data["error"] = "interrupted: worker process is gone and left no result"
        return data

    def _rehydrate(self) -> None:
        """Load prior jobs from disk so history survives a server restart."""
        for jdir in self._runs_dir.glob("*/"):
            jf = jdir / "job.json"
            if not jf.is_file():
                continue
            data = read_job_json(jf)
            if data is None or "job_id" not in data:
                # Unparseable or zero-byte (written before the atomic-write fix).
                # Skipping silently is how five of these accumulated and made
                # list_jobs misleading; record the corpse instead.
                self._adopt_unreadable(jdir)
                continue
            data = self._reconcile_unfinished(data, jdir, at_startup=True)
            job = Job(**{k: data[k] for k in JOB_FIELDS if k in data})
            self._jobs[job.job_id] = job

    def _adopt_unreadable(self, jdir: Path) -> None:
        """Surface a run directory whose job.json cannot be parsed.

        These are not hypothetical: two zero-byte ``job.json`` files predate the
        atomic-write fix and were skipped forever, so ``list_jobs`` simply did
        not mention runs that exist on disk. A job that cannot be read is a
        failed job, and saying so is more useful than pretending it is absent.
        """
        job_id = jdir.name.rstrip("/")
        if not job_id or job_id in self._jobs:
            return
        self._jobs[job_id] = Job(
            job_id=job_id,
            tool=job_id.rsplit("-", 1)[0],
            status="failed",
            run_dir=str(jdir),
            log_path=str(jdir / "run.log"),
            error="unreadable job.json (written before the atomic-write fix)",
        )

    # -- submission ------------------------------------------------------------
    def _new_job(self, tool: str) -> Job:
        job_id = f"{tool}-{uuid.uuid4().hex[:8]}"
        run_dir = self._runs_dir / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        job = Job(
            job_id=job_id,
            tool=tool,
            run_dir=str(run_dir),
            log_path=str(run_dir / "run.log"),
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def submit(self, tool: str, fn: JobFn, *, background: bool = True) -> Job:
        """Register a job and start its worker IN THIS PROCESS.

        Prefer :meth:`submit_detached` for anything that can outlive an MCP
        client — which is every COMSOL solve. A daemon thread dies with this
        server, and this server is per-client and short-lived.

        Parameters
        ----------
        tool
            Name of the tool launching the work (for display/filtering).
        fn
            Worker callable; receives the :class:`Job`, returns a result dict.
        background
            If True (default) run in a daemon thread and return immediately.
            If False, run synchronously (used by tests for determinism).
        """
        job = self._new_job(tool)
        self._persist(job)

        if background:
            threading.Thread(target=self._run, args=(job, fn),
                             name=job.job_id, daemon=True).start()
        else:
            self._run(job, fn)
        return job

    def reserve(self, tool: str) -> Job:
        """Create a job and its run directory WITHOUT starting anything.

        For tools that must prepare something inside ``run_dir`` before the
        worker exists — :func:`comsol_suite.runner.patch_script` writes a
        rewritten copy of the pipeline script there, and the argv then points at
        it. Preparation is cheap and touches no solver, so it belongs in the
        caller; only the solve needs to be detached. Follow with
        :meth:`start_detached`.
        """
        job = self._new_job(tool)
        self._persist(job)
        return job

    def submit_detached(self, tool: str, argv: Sequence[str], **kwargs: Any) -> Job:
        """Register a job and run it in a worker process that outlives us.

        One-shot form of :meth:`reserve` + :meth:`start_detached`; see the
        latter for the keyword arguments and for why detaching matters.
        """
        return self.start_detached(self.reserve(tool), argv, **kwargs)

    def start_detached(
        self,
        job: Job,
        argv: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        timeout_s: Optional[float] = None,
        debug: bool = False,
        collect_dir: Optional[Path] = None,
        collect_patterns: Sequence[str] = (),
        extra_files: Sequence[Path] = (),
        ok_returncodes: Sequence[int] = (0,),
        log_tail_lines: int = 30,
        post: Optional[str] = None,
        post_kwargs: Optional[Dict[str, Any]] = None,
        python_bin: Optional[str] = None,
    ) -> Job:
        """Start a reserved job's work in a process that outlives this server.

        The worker is :mod:`comsol_suite.job_runner`, started with
        ``start_new_session=True`` so it leads its own session: it has no
        controlling terminal in common with this server, and the SIGHUP/SIGINT
        that tears the server down when its MCP client disconnects cannot reach
        it. From the moment it starts it owns ``job.json``; this process only
        reads it.

        ``ok_returncodes`` lists exit codes that mean "ran to completion" —
        several gates carry a verdict in the exit code (rc 2 = FAIL, rc 3 =
        unverified) rather than signalling a crash.

        ``post`` names a ``"module:function"`` the worker calls with the raw
        result, for tools whose answer needs computing from the artifacts rather
        than just reporting.
        """
        tool = job.tool
        run_dir = Path(job.run_dir)
        spec = {
            "tool": tool,
            "argv": [str(a) for a in argv],
            "cwd": str(cwd) if cwd else None,
            "timeout_s": timeout_s,
            "debug": bool(debug),
            "collect_dir": str(collect_dir) if collect_dir else None,
            "collect_patterns": list(collect_patterns),
            "extra_files": [str(p) for p in extra_files],
            "ok_returncodes": list(ok_returncodes),
            "log_tail_lines": log_tail_lines,
            "post": post,
            "post_kwargs": post_kwargs or {},
        }
        # Spec first, then job.json: the worker reads the spec on entry, so a
        # spec written after the spawn would be a race we could lose.
        #
        # And this is the LAST time this process writes job.json for this job.
        # From the spawn onwards the worker owns it — a "helpful" update from
        # here would race a fast job and stamp `running` over a finished
        # result. The pid therefore goes in its own parent-owned file rather
        # than back into job.json.
        write_job_json(run_dir / "spec.json", spec)
        self._persist(job)

        # The worker imports comsol_suite, so it needs this package importable.
        # sys.executable is this server's interpreter, which by construction has
        # it — resolving anything else here would be guessing.
        env = dict(os.environ)
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            [pkg_parent, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [pkg_parent])
        err_path = run_dir / "runner.err"
        try:
            with err_path.open("wb") as err:
                proc = subprocess.Popen(
                    [python_bin or sys.executable, "-m", "comsol_suite.job_runner",
                     str(run_dir)],
                    stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True,
                )
        except OSError as exc:
            # Nothing was spawned, so nobody else owns job.json — safe to write.
            job.status = "failed"
            job.error = f"could not start worker process: {exc}"
            job.finished_at = time.time()
            self._persist(job)
            return job

        (run_dir / WORKER_PID_FILE).write_text(str(proc.pid), encoding="utf-8")
        # In-memory only, for this call's return value. The worker writes the
        # authoritative status.
        job.pid = proc.pid
        job.status = "running"
        job.started_at = time.time()
        return job

    def _run(self, job: Job, fn: JobFn) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._persist(job)
        try:
            result = fn(job) or {}
            job.result = result
            # A worker may signal logical failure via {"ok": False}.
            job.status = "completed" if result.get("ok", True) else "failed"
            if job.status == "failed":
                job.error = result.get("error", "worker reported ok=False")
        except Exception as exc:  # worker raised — capture full traceback
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            try:
                Path(job.log_path).open("a", encoding="utf-8").write(
                    "\n[jobs] worker raised:\n" + traceback.format_exc())
            except OSError:
                pass
        finally:
            job.finished_at = time.time()
            self._persist(job)

    # -- queries ---------------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        """Look the job up in memory, then fall back to its job.json on disk.

        The disk fallback is what makes an async job pollable AT ALL here: this
        server is spawned per MCP client, so a job launched by a station
        subagent's server is simply absent from the orchestrator's in-memory
        map, and ``_rehydrate`` only runs at construction — before that job
        existed. Without this, an agent that launches a background job and then
        asks after it gets "unknown job_id" for a job it just started, which is
        exactly what happened to a real run: the model then looped, hunting the
        filesystem for output that was being written under a job id it had been
        told did not exist.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None and job.status in ("completed", "failed"):
            return job
        # Not finished as far as this process knows — and for a detached job
        # this process will NEVER know, because the worker updates job.json and
        # nothing updates the object we are holding. Re-reading is what keeps a
        # poll from returning "running" forever on a job that finished an hour
        # ago. (For an in-process job the disk copy is written on every state
        # change too, so this is correct there as well, just redundant.)
        refreshed = self._load_from_disk(job_id)
        if refreshed is not None:
            with self._lock:
                self._jobs[job_id] = refreshed
            return refreshed
        if job is not None:
            return job
        loaded = self._load_from_disk(job_id)
        if loaded is not None:
            with self._lock:
                # Re-check: a concurrent caller may have rehydrated it first, and
                # that instance is the one any waiter already holds a reference to.
                job = self._jobs.get(job_id)
                if job is None:
                    self._jobs[job_id] = loaded
                    job = loaded
        return job

    def _load_from_disk(self, job_id: str) -> Optional[Job]:
        """Read one job's persisted metadata, or None if it is absent/unreadable."""
        # job_id becomes a path component, and it arrives from a tool argument —
        # i.e. ultimately from a model. Anything with a separator or a parent
        # reference is refused rather than resolved.
        if (not job_id or job_id in (".", "..")
                or "/" in job_id or "\\" in job_id or "\x00" in job_id):
            return None
        jf = self._job_json(job_id)
        data = read_job_json(jf)
        if data is None or "job_id" not in data:
            return None
        # A job still marked running/pending here was launched by a process we
        # cannot see, so ask whether that process is still alive rather than
        # either trusting the file or assuming the worst. Getting this wrong in
        # both directions has bitten: reporting a live solve as failed throws
        # away hours of work, and reporting a dead one as running makes an agent
        # poll forever.
        return Job(**{k: v for k, v in
                      self._reconcile_unfinished(data, jf.parent, at_startup=False).items()
                      if k in JOB_FIELDS})

    def list(self) -> List[Job]:
        with self._lock:
            return sorted(self._jobs.values(),
                         key=lambda j: j.created_at, reverse=True)

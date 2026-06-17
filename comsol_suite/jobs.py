"""Background job registry for long-running pipeline stages.

COMSOL solves take hours and even a full ABCD fit takes a little while, so the
MCP tools that launch them must not block the server. The pattern is:

    tool call  ->  registry.submit(...)  ->  returns {"job_id": ...} immediately
    (work runs in a background thread, writing to runs/<job_id>/)
    later  ->  get_job_status(job_id) / get_job_result(job_id)

Each job owns a directory ``runs/<job_id>/`` containing:

    job.json    - serialised :class:`Job` metadata (status, timing, result)
    run.log     - merged stdout/stderr of the wrapped subprocess

Because the metadata is persisted to ``job.json`` on every state change, the
registry rehydrates previous jobs on startup, so ``list_jobs`` / status queries
keep working across MCP-server restarts.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# A job worker receives its own Job (for run_dir / log_path) and returns a
# JSON-serialisable result dict (typically {"summary": ..., "output_files": [...]}).
JobFn = Callable[["Job"], Dict[str, Any]]


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
        path = self._job_json(job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job.to_public(), indent=2), encoding="utf-8")

    def _rehydrate(self) -> None:
        """Load prior jobs from disk so history survives a server restart."""
        for jdir in self._runs_dir.glob("*/"):
            jf = jdir / "job.json"
            if not jf.is_file():
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            # A job that was 'running' when the server died can never resume;
            # mark it as failed/interrupted so callers are not misled.
            if data.get("status") == "running":
                data["status"] = "failed"
                data["error"] = "interrupted: MCP server restarted mid-run"
            job = Job(**{k: data[k] for k in (
                "job_id", "tool", "status", "created_at", "started_at",
                "finished_at", "run_dir", "log_path", "result", "error")
                if k in data})
            self._jobs[job.job_id] = job

    # -- submission ------------------------------------------------------------
    def submit(self, tool: str, fn: JobFn, *, background: bool = True) -> Job:
        """Register a job and start its worker.

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
        self._persist(job)

        if background:
            threading.Thread(target=self._run, args=(job, fn),
                             name=job_id, daemon=True).start()
        else:
            self._run(job, fn)
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
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        with self._lock:
            return sorted(self._jobs.values(),
                         key=lambda j: j.created_at, reverse=True)

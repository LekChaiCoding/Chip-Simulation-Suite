"""Tests for the detached job worker — the fix for jobs dying with their server.

The bug these exist to prevent regressing: background work ran on a **daemon
thread inside the MCP server**, and that server is spawned per MCP client and is
short-lived. When the client went away the thread died mid-solve and the record
sat at ``"running"`` with an empty log forever. An NT2 notch sweep is hours, so
losing one at the end cost the whole solve.

The load-bearing assertions here are the two halves of getting that right:

* a job whose *launching process is gone* must still finish and still be
  readable (``test_job_survives_its_launcher``) — the bug itself;
* a job whose *worker is gone* must be reported failed rather than left running
  forever (``test_dead_worker_is_reported_failed``) — the over-correction, which
  would be just as wrong in the other direction.

These run real processes deliberately. A mocked ``Popen`` would prove nothing
about surviving a dead parent, which is the entire claim.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from comsol_suite.jobs import (WORKER_PID_FILE, JobRegistry, read_job_json,
                               worker_alive, write_job_json)

# Long enough that the launcher is provably gone before the work ends, short
# enough not to drag the suite out.
SLEEP_S = 6
POLL_TIMEOUT_S = 60


def _wait_for(registry_dir: Path, job_id: str, statuses, timeout_s=POLL_TIMEOUT_S):
    """Poll from a FRESH registry each time — a new server, as in real use."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        job = JobRegistry(registry_dir).get(job_id)
        last = job.status if job else None
        if last in statuses:
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} stuck at {last!r}, wanted one of {statuses}")


def test_job_survives_its_launcher(tmp_path: Path):
    """Kill the launcher; the work still completes and the result is readable.

    This is the exact sequence that failed before: launch, lose the server, poll
    from a new one.
    """
    runs = tmp_path / "runs"
    marker = tmp_path / "artifact.json"
    launcher = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from comsol_suite.jobs import JobRegistry
reg = JobRegistry(Path({str(runs)!r}))
job = reg.submit_detached(
    "survives_launcher",
    [sys.executable, "-c",
     "import time, pathlib, sys; time.sleep({SLEEP_S}); "
     "pathlib.Path(sys.argv[1]).write_text('{{}}')", {str(marker)!r}],
    collect_dir=Path({str(tmp_path)!r}), collect_patterns=["*.json"],
    timeout_s=120)
print(job.job_id)
"""
    proc = subprocess.run([sys.executable, "-c", launcher],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    job_id = proc.stdout.strip().splitlines()[-1]

    # The launcher has exited. Its child must not have. Either unfinished
    # status is fine — the parent writes `pending` and the worker flips it to
    # `running` a moment later, and which one a poll catches is a race. What
    # must never happen is `failed`, which is what the old code produced the
    # instant the launching server went away.
    job = JobRegistry(runs).get(job_id)
    assert job.status in {"pending", "running"}, \
        f"declared dead the moment its parent left: {job.status} / {job.error}"

    done = _wait_for(runs, job_id, {"completed", "failed"})
    assert done.status == "completed", done.error
    assert done.result["returncode"] == 0
    assert done.result["timed_out"] is False
    assert marker.exists(), "the work itself did not actually run"
    assert str(marker) in done.result["output_files"]


def test_dead_worker_is_reported_failed(tmp_path: Path):
    """A worker killed mid-run is failed, not "running" forever.

    The pid check is what separates this from the previous test; without it,
    making jobs survive their launcher would make dead jobs immortal.
    """
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    job = reg.submit_detached("killed", [sys.executable, "-c", "import time; time.sleep(300)"],
                              timeout_s=600)
    assert job.pid
    time.sleep(1.5)
    os.kill(job.pid, signal.SIGKILL)
    time.sleep(0.5)

    seen = JobRegistry(runs).get(job.job_id)
    assert seen.status == "failed"
    assert "gone" in (seen.error or "")


def test_running_record_is_left_alone_while_its_worker_lives(tmp_path: Path):
    """Rehydration must not fail a live job.

    The old ``_rehydrate`` rewrote every ``running`` record to failed at startup.
    Against a detached worker that is a fabricated failure — and the caller has
    no way to tell it from a real one.
    """
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    job = reg.submit_detached("still_going", [sys.executable, "-c",
                                              f"import time; time.sleep({SLEEP_S})"])
    time.sleep(1.0)
    for _ in range(3):                       # several "server restarts" in a row
        again = JobRegistry(runs).get(job.job_id)
        assert again.status == "running", f"a live worker was declared {again.status}"
    _wait_for(runs, job.job_id, {"completed", "failed"})


def test_nonzero_exit_is_failed_and_timeout_is_distinguishable(tmp_path: Path):
    """``ok`` is not the whole story: a wall-clock kill and a crash differ."""
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)

    crashed = reg.submit_detached("crasher", [sys.executable, "-c", "raise SystemExit(3)"])
    done = _wait_for(runs, crashed.job_id, {"completed", "failed"})
    assert done.status == "failed"
    assert done.result["returncode"] == 3
    assert done.result["timed_out"] is False

    slow = reg.submit_detached("slowpoke", [sys.executable, "-c", "import time; time.sleep(30)"],
                               timeout_s=2)
    done = _wait_for(runs, slow.job_id, {"completed", "failed"})
    assert done.status == "failed"
    assert done.result["timed_out"] is True, "a timeout is indistinguishable from a crash"


def test_verdict_carrying_exit_code_is_not_a_failure(tmp_path: Path):
    """Some gates carry their verdict in the exit code (rc 2 = FAIL, 3 =
    unverified) rather than crashing. Those runs completed."""
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    job = reg.submit_detached("gate", [sys.executable, "-c", "raise SystemExit(2)"],
                              ok_returncodes=(0, 2))
    done = _wait_for(runs, job.job_id, {"completed", "failed"})
    assert done.status == "completed", done.error
    assert done.result["returncode"] == 2


def test_post_processor_runs_in_the_worker(tmp_path: Path):
    """The spec's ``post`` hook shapes the result where the artifacts are."""
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    (tmp_path / "model.mph").write_text("not really a model")
    job = reg.submit_detached(
        "with_post", [sys.executable, "-c", "pass"],
        collect_dir=tmp_path, collect_patterns=["*.mph"],
        post="comsol_suite.tools.comsol:post_surface_mph")
    done = _wait_for(runs, job.job_id, {"completed", "failed"})
    assert done.status == "completed", done.error
    assert done.result["mph_paths"] == [str(tmp_path / "model.mph")]
    assert "1 MPH file(s) saved" in done.result["summary"]


def test_broken_post_processor_does_not_lose_the_run(tmp_path: Path):
    """A formatting bug must not discard a solve that actually completed."""
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    job = reg.submit_detached("bad_post", [sys.executable, "-c", "pass"],
                              post="comsol_suite.tools.comsol:no_such_function")
    done = _wait_for(runs, job.job_id, {"completed", "failed"})
    assert done.status == "completed", done.error
    assert done.result["returncode"] == 0
    assert "no_such_function" in done.result["post_error"]


def test_unreadable_job_json_is_surfaced_not_skipped(tmp_path: Path):
    """A zero-byte job.json is a failed job, not an absent one.

    Two of these accumulated on disk from before the atomic-write fix and were
    skipped forever, so ``list_jobs`` simply did not mention runs that exist.
    """
    runs = tmp_path / "runs"
    (runs / "ghost_tool-deadbeef").mkdir(parents=True)
    (runs / "ghost_tool-deadbeef" / "job.json").write_text("")
    listed = {j.job_id: j for j in JobRegistry(runs).list()}
    assert "ghost_tool-deadbeef" in listed
    assert listed["ghost_tool-deadbeef"].status == "failed"
    assert listed["ghost_tool-deadbeef"].tool == "ghost_tool"


def test_legacy_running_record_without_a_spec_is_still_failed(tmp_path: Path):
    """An in-process job really could not survive its server; keep saying so."""
    runs = tmp_path / "runs"
    jdir = runs / "legacy-00000000"
    jdir.mkdir(parents=True)
    write_job_json(jdir / "job.json", {
        "job_id": "legacy-00000000", "tool": "legacy", "status": "running",
        "created_at": time.time() - 3600, "run_dir": str(jdir),
    })
    job = JobRegistry(runs).get("legacy-00000000")
    assert job.status == "failed"
    assert "MCP server restarted" in job.error


def test_worker_alive_rejects_a_recycled_pid(tmp_path: Path):
    """Liveness alone is not identity: pid 1 is alive and is not our worker."""
    assert worker_alive(None, str(tmp_path)) is False
    assert worker_alive(1, str(tmp_path)) is False
    assert worker_alive(os.getpid(), str(tmp_path)) is False


def test_spec_with_an_unknown_key_is_refused(tmp_path: Path):
    """A typo'd spec key would silently not apply; fail loudly instead."""
    from comsol_suite.job_runner import load_spec
    run_dir = tmp_path / "job"
    run_dir.mkdir()
    write_job_json(run_dir / "spec.json",
                   {"tool": "t", "argv": ["true"], "timeoutsecs": 5})
    with pytest.raises(ValueError, match="unknown keys"):
        load_spec(run_dir)


def test_parent_writes_the_pid_file_and_never_job_json_after_spawn(tmp_path: Path):
    """job.json has exactly one writer once the worker starts.

    Two writers would race a fast job: the parent's "helpfully" stamping
    ``running`` after the worker already wrote ``completed`` loses the result.
    """
    runs = tmp_path / "runs"
    reg = JobRegistry(runs)
    job = reg.submit_detached("fast", [sys.executable, "-c", "pass"])
    run_dir = Path(job.run_dir)
    assert (run_dir / WORKER_PID_FILE).is_file()
    assert int((run_dir / WORKER_PID_FILE).read_text()) == job.pid

    done = _wait_for(runs, job.job_id, {"completed", "failed"})
    assert done.status == "completed", done.error
    # The on-disk record is the worker's, not a later overwrite by the parent.
    on_disk = read_job_json(run_dir / "job.json")
    assert on_disk["status"] == "completed"
    assert on_disk["pid"] == job.pid
    assert json.loads((run_dir / "spec.json").read_text())["tool"] == "fast"

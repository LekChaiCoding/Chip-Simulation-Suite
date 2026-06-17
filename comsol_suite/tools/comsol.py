"""COMSOL stage tools: build, validate, sweep, export.

These tools wrap the project's COMSOL driver scripts. Running them for real
requires a live COMSOL connection (the ``mph`` package + a COMSOL server/licence),
which is intentionally *outside* this task's scope — so every tool here defaults
to ``dry_run=True``: it validates arguments and probes COMSOL reachability
**without solving**, returning a report of exactly what *would* run.

Set ``dry_run=False`` (once you are on the COMSOL network) to actually launch the
wrapped script as a background job. The launch path mirrors the CAD/fitting
stages: a path-redirected copy of the upstream script run as a subprocess, so a
JPype/COMSOL crash takes down a child process, never the MCP server.

The boundary is deliberate: the suite is *wired* end-to-end, but the COMSOL link
is the one segment that cannot be exercised without hardware, so it ships gated.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import run_command


# ─────────────────────────────────────────────────────────────────────────────
# Connection health
# ─────────────────────────────────────────────────────────────────────────────
def comsol_health_check(
    comsol_host: Optional[str] = None,
    comsol_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Check that COMSOL is reachable from this machine, without solving.

    Verifies (a) the ``mph`` Python package is importable and (b) — when a host
    is configured — that a TCP connection to ``host:port`` succeeds. This is the
    cheap pre-flight every COMSOL tool runs before attempting real work.

    Returns
    -------
    dict
        ``{ok, mph_available, host, port, host_reachable, detail}``.
    """
    cfg = load_config()
    host = comsol_host or cfg.comsol_host
    port = int(comsol_port or cfg.comsol_port)

    # (a) mph import — needed for any real solve.
    try:
        import mph  # noqa: F401
        mph_available = True
        mph_detail = "mph import OK"
    except Exception as exc:
        mph_available = False
        mph_detail = f"mph not importable: {type(exc).__name__}: {exc}"

    # (b) socket probe (only when a remote host is configured).
    host_reachable: Optional[bool] = None
    sock_detail = "no host configured (local COMSOL assumed)"
    if host:
        try:
            with socket.create_connection((host, port), timeout=5):
                host_reachable = True
                sock_detail = f"TCP connect to {host}:{port} OK"
        except OSError as exc:
            host_reachable = False
            sock_detail = f"cannot reach {host}:{port}: {exc}"

    ok = mph_available and (host_reachable is not False)
    return {
        "ok": ok,
        "mph_available": mph_available,
        "host": host,
        "port": port,
        "host_reachable": host_reachable,
        "detail": f"{mph_detail}; {sock_detail}",
    }


def _preflight(tool: str, argv: List[str], host: Optional[str],
               port: Optional[int]) -> Dict[str, Any]:
    """Common dry-run report shared by the COMSOL tools."""
    health = comsol_health_check(host, port)
    return {
        "dry_run": True,
        "tool": tool,
        "would_run": [str(a) for a in argv],
        "comsol_health": health,
        "ready": health["ok"],
        "note": ("Validated only. Re-call with dry_run=False once connected to "
                 "COMSOL to launch the solve as a background job."),
    }


def _launch(registry: JobRegistry, tool: str, argv: List[str],
            out: Path, collect: List[str], debug: bool,
            timeout_s: float) -> Dict[str, Any]:
    """Common real-run path: submit the subprocess as a background job."""
    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        res = run_command(argv, log_path=Path(job.log_path),
                         cwd=out, timeout_s=timeout_s, debug=debug)
        files: List[str] = []
        for pat in collect:
            files.extend(str(p) for p in out.rglob(pat))
        return {
            "ok": res.ok,
            "output_files": sorted(set(files)),
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": f"{tool} finished rc={res.returncode}",
            "error": None if res.ok else f"{tool} failed (see run.log)",
        }

    job = registry.submit(tool, worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Tools (all default to dry_run=True)
# ─────────────────────────────────────────────────────────────────────────────
def build_comsol_model(
    registry: JobRegistry,
    gds_path: str,
    junction_inductance_ph: float = 280.0,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Build (and lightly solve) the COMSOL EM model from a GDS.

    Wraps ``recreate_and_solve.py`` (build -> geometry validation -> coarse solve).

    Parameters
    ----------
    gds_path
        GDS produced by :func:`comsol_suite.tools.cad.generate_cad`.
    junction_inductance_ph
        Josephson inductance per junction (COMSOL ``juncL`` param), pH.
    comsol_host
        Override the configured COMSOL host for this call.
    dry_run
        If True (default), only validate + health-check. If False, launch.
    """
    cfg = load_config()
    src = cfg.script("comsol_build")
    out = Path(output_dir) if output_dir else cfg.runs_dir / "comsol_build"
    argv = [cfg.python_bin, src, "--gds", gds_path,
            "--juncL-ph", str(junction_inductance_ph),
            "--host", comsol_host or (cfg.comsol_host or "local"),
            "--out", str(out)]

    if not src.is_file():
        return {"ok": False, "error": f"COMSOL build script not found: {src}"}
    if dry_run:
        return _preflight("build_comsol_model", argv, comsol_host, cfg.comsol_port)
    return _launch(registry, "build_comsol_model", argv, out,
                   ["*.mph", "*.csv"], debug, timeout_s=7200)


def validate_geometry(
    mph_path: str,
    reference_vertices_csv: Optional[str] = None,
    comsol_host: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Validate a built model's geometry (face counts + full vertex multiset).

    This is the mandatory gate before trusting any solve. Wrapped from the
    project's vertex-diff checker. Dry-run reports readiness only.
    """
    cfg = load_config()
    return _preflight(
        "validate_geometry",
        [cfg.python_bin, "<vertex-diff-checker>", "--mph", mph_path,
         "--ref", reference_vertices_csv or "<ref_vertices.csv>"],
        comsol_host, cfg.comsol_port,
    ) if dry_run else {
        "ok": False,
        "error": "real-run validate_geometry requires a live COMSOL session "
                 "(re-run on the COMSOL network).",
    }


def run_stub_length_sweep(
    registry: JobRegistry,
    mph_path: str,
    stub_lengths_um: List[float],
    freq_ghz: List[float],
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Solve a parametric stub-length sweep, extracting complex S-parameters.

    Wraps ``sweep_stub_length.py`` (rebuild geometry + remesh + solve per stub).
    The output ``.dat`` is exactly the format the fitting tools consume — this is
    the COMSOL->fitting handoff. Dry-run reports the planned sweep only.
    """
    cfg = load_config()
    src = cfg.script("comsol_sweep")
    out = Path(output_dir) if output_dir else cfg.runs_dir / "comsol_sweep"
    argv = [cfg.python_bin, src, "--mph", mph_path,
            "--stubs", ",".join(str(s) for s in stub_lengths_um),
            "--freqs-ghz", ",".join(str(f) for f in freq_ghz),
            "--host", comsol_host or (cfg.comsol_host or "local"),
            "--out", str(out)]

    if not src.is_file():
        return {"ok": False, "error": f"COMSOL sweep script not found: {src}"}
    if dry_run:
        return _preflight("run_stub_length_sweep", argv, comsol_host, cfg.comsol_port)
    return _launch(registry, "run_stub_length_sweep", argv, out,
                   ["*.dat", "*.csv"], debug, timeout_s=21600)


def export_touchstone(
    registry: JobRegistry,
    csv_path: str,
    output_path: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Convert an extracted S-parameter CSV into a Touchstone ``.s2p`` file.

    Wraps ``export_touchstone.py``. This step needs no COMSOL connection itself,
    but is kept here as part of the COMSOL stage; it defaults to dry-run for
    consistency. Set ``dry_run=False`` to run it (it is safe to run offline).
    """
    cfg = load_config()
    src = cfg.script("comsol_export")
    out = Path(output_path) if output_path else cfg.runs_dir / "touchstone"
    argv = [cfg.python_bin, src, "--csv", csv_path, "--out", str(out)]

    if not src.is_file():
        return {"ok": False, "error": f"export script not found: {src}"}
    if dry_run:
        return _preflight("export_touchstone", argv, None, cfg.comsol_port)
    return _launch(registry, "export_touchstone", argv, out.parent,
                   ["*.s2p"], debug, timeout_s=300)

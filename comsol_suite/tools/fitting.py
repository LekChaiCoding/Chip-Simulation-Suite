"""Fitting stage tools: extract the lumped circuit from S-parameters.

Three tools live here:

  * :func:`run_abcd_fit`     — the Python ABCD/Bloch fitter (``abcd_fit.py``).
    Pure ``numpy``/``scipy``; this is the suite's primary, always-runnable proof
    that the COMSOL output flows through to a circuit extraction.
  * :func:`fit_stub_sweep`   — the Julia single-Cg fitter (``fit_stub_sweep.jl``).
  * :func:`analyze_dispersion` — the Julia Bloch dispersion / delta-k analysis
    (``dispersion_analysis.jl``).

The Julia tools require a Julia install plus the project's Julia environment
(``JosephsonCircuits.jl`` etc.); they are launched as subprocesses and reported
as background jobs. The Python fit is the dependency-light path used for
end-to-end verification.

As in the CAD stage, the upstream scripts hard-code their input/output paths, so
each is run as a *path-redirected copy* whose only change is where it reads the
sweep data and writes its results — keeping the originals (and the tracked
result folders) untouched.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import patch_script, run_command


def _collect(out_dir: Path, patterns: List[str]) -> List[str]:
    """Return sorted file paths under ``out_dir`` matching any glob pattern."""
    found: List[str] = []
    for pat in patterns:
        found.extend(str(p) for p in out_dir.rglob(pat))
    return sorted(set(found))


# ─────────────────────────────────────────────────────────────────────────────
# Python ABCD fit  (primary, dependency-light)
# ─────────────────────────────────────────────────────────────────────────────
def run_abcd_fit(
    registry: JobRegistry,
    data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Launch the Python ABCD/Bloch fit over a stub-length sweep.

    Tests 3 chain topologies x 5 fit objectives per stub and writes a results
    CSV plus per-stub comparison CSVs and figures.

    Parameters
    ----------
    registry
        The shared :class:`~comsol_suite.jobs.JobRegistry`.
    data_path
        ``stub_length_sweep.dat`` to fit. Defaults to the bundled bridge/003
        sweep.
    output_dir
        Where to write results. Defaults to a per-job folder under ``runs/``.
    background
        Run asynchronously (default) and return a ``job_id`` immediately.
    debug
        Echo the command line into the job log.

    Returns
    -------
    dict
        ``{job_id, status}`` (poll with ``get_job_status`` / ``get_job_result``).
    """
    cfg = load_config()
    src = cfg.script("abcd_fit")
    data = Path(data_path) if data_path else cfg.datum("bridge003_sweep")

    if not src.is_file():
        return {"ok": False, "error": f"abcd_fit.py not found: {src}"}
    if not data.is_file():
        return {"ok": False, "error": f"sweep data not found: {data}"}

    def worker(job: Job) -> Dict[str, Any]:
        out = Path(output_dir) if output_dir else Path(job.run_dir) / "abcd_out"
        out.mkdir(parents=True, exist_ok=True)

        # Redirect DAT_PATH (input) and OUT_BASE (output root). DATA_DIR/FIG_DIR
        # are derived from OUT_BASE inside the script, so they follow along.
        patched = patch_script(
            src,
            Path(job.run_dir) / "_abcd_fit_patched.py",
            {
                r"^DAT_PATH\s*=.*$": f'DAT_PATH = r"{data.as_posix()}"',
                r"^OUT_BASE\s*=.*$": f'OUT_BASE = r"{out.as_posix()}"',
            },
        )
        res = run_command(
            [cfg.python_bin, patched],
            log_path=Path(job.log_path),
            cwd=out,
            timeout_s=900,
            debug=debug,
        )
        results = _collect(out, ["*.csv"])
        ok = res.ok and any(p.endswith("abcd_fit_results.csv") for p in results)
        return {
            "ok": ok,
            "output_files": results,
            "figures": _collect(out, ["*.png"]),
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": (f"{len(results)} CSV(s) written"
                        if ok else "no results CSV produced (see run.log)"),
            "error": None if ok else "ABCD fit did not produce results CSV",
        }

    job = registry.submit("abcd_fit", worker, background=background)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Julia fits  (optional — need a Julia env with JosephsonCircuits.jl)
# ─────────────────────────────────────────────────────────────────────────────
def _run_julia(
    registry: JobRegistry,
    script_key: str,
    tool_name: str,
    path_patches: Dict[str, str],
    extra_sources: List[str],
    background: bool,
    debug: bool,
) -> Dict[str, Any]:
    """Shared launcher for the two Julia tools.

    The Julia scripts ``include`` ``common_chain_model.jl`` relatively and build
    their data/output paths from ``@__DIR__``. We copy the script *and* its
    siblings into the job dir, then patch the directory constants to absolute
    paths so nothing reads or writes inside the tracked tree.
    """
    cfg = load_config()
    src = cfg.script(script_key)
    if not src.is_file():
        return {"ok": False, "error": f"{src.name} not found: {src}"}

    def worker(job: Job) -> Dict[str, Any]:
        work = Path(job.run_dir) / "julia"
        work.mkdir(parents=True, exist_ok=True)
        out = work / "Data"
        out.mkdir(exist_ok=True)

        # Copy sibling sources (e.g. common_chain_model.jl) verbatim so the
        # relative `include(...)` still resolves.
        for sib in extra_sources:
            sib_path = src.parent / sib
            if sib_path.is_file():
                shutil.copy2(sib_path, work / sib)

        patches = {pat: rep.format(bridge=cfg.chip_sim_root.as_posix(),
                                   out=out.as_posix())
                   for pat, rep in path_patches.items()}
        patched = patch_script(src, work / src.name, patches, require_all=False)

        res = run_command(
            [cfg.julia_bin, patched],
            log_path=Path(job.log_path),
            cwd=work,
            timeout_s=1800,
            debug=debug,
        )
        results = _collect(out, ["*.csv"])
        ok = res.ok and bool(results)
        return {
            "ok": ok,
            "output_files": results,
            "figures": _collect(work, ["*.png"]),
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": (f"{len(results)} CSV(s) written" if ok
                        else "no CSV produced — is Julia + JosephsonCircuits.jl "
                             "installed? (see run.log)"),
            "error": None if ok else "Julia fit produced no results",
        }

    job = registry.submit(tool_name, worker, background=background)
    return {"job_id": job.job_id, "status": job.status}


def fit_stub_sweep(
    registry: JobRegistry,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Launch the Julia single-Cg fitter (``fit_stub_sweep.jl``).

    Requires Julia + the project's Julia environment. Returns a ``job_id``.
    """
    return _run_julia(
        registry,
        script_key="julia_fit",
        tool_name="julia_fit",
        # BRIDGE_DIR -> the real bridge/003 data; DATA_DIR -> our job output.
        path_patches={
            r"^const BRIDGE_DIR\s*=.*$":
                'const BRIDGE_DIR = "{bridge}/JosephsonCircuit/bridge/003"',
            r"^const DATA_DIR\s*=.*$": 'const DATA_DIR = "{out}"',
        },
        extra_sources=["common_chain_model.jl"],
        background=background,
        debug=debug,
    )


def analyze_dispersion(
    registry: JobRegistry,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Launch the Julia Bloch dispersion / delta-k analysis.

    Requires the fit-results CSV from :func:`fit_stub_sweep` to exist. Returns a
    ``job_id``.
    """
    return _run_julia(
        registry,
        script_key="julia_disp",
        tool_name="julia_disp",
        path_patches={
            r"^const DATA_DIR\s*=.*$": 'const DATA_DIR = "{out}"',
        },
        extra_sources=["common_chain_model.jl"],
        background=background,
        debug=debug,
    )

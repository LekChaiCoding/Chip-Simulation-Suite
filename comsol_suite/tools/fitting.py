"""Fitting stage tools: extract the lumped circuit from S-parameters.

Tools live here:

  * :func:`run_abcd_fit`          — the Python ABCD/Bloch fitter (``abcd_fit.py``).
    Pure ``numpy``/``scipy``; always runnable (no COMSOL / Julia required).
    Accepts an optional ``stub_filter_um`` to restrict to a single stub length,
    enabling the parallel sweep below.
  * :func:`run_abcd_fit_parallel` — submits one background job per stub, runs
    them concurrently via a ThreadPoolExecutor supervisor, then merges the result
    CSVs into a single ``abcd_fit_results_merged.csv``. Typically 4-6× faster
    than the sequential version on multi-core machines.
  * :func:`run_generic_fit`       — runs *any* user-supplied fitting script with
    path-redirection (data-in / output-base). Accepts ``extra_patches`` for
    additional variable substitutions, so scripts written for different devices or
    circuit models plug in without modification.
  * :func:`fit_stub_sweep`        — the Julia single-Cg fitter (``fit_stub_sweep.jl``).
  * :func:`analyze_dispersion`    — the Julia Bloch dispersion / delta-k analysis
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

import concurrent.futures
import re
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


def _get_stubs_from_dat(data_path: Path) -> List[float]:
    """Return sorted unique stub-length values from a stub_length_sweep.dat file.

    Reads only the first column (stub_length_um) without importing any pipeline
    script. Skips blank lines and ``#``-comments. Skips the header row (which
    starts with a letter, not a digit or '-').
    """
    stubs: set[float] = set()
    with open(data_path) as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                val = float(stripped.split(",")[0])
                stubs.add(val)
            except (ValueError, IndexError):
                pass  # header row or malformed line
    return sorted(stubs)


# ─────────────────────────────────────────────────────────────────────────────
# Python ABCD fit  (primary, dependency-light)
# ─────────────────────────────────────────────────────────────────────────────
def run_abcd_fit(
    registry: JobRegistry,
    data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    stub_filter_um: Optional[float] = None,
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
    stub_filter_um
        When set, restrict the fit to a single stub length (in µm). Used
        internally by :func:`run_abcd_fit_parallel`; can also be called
        directly to re-fit a single stub.
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

        # Build the DAT_PATH replacement, optionally injecting STUB_FILTER_UM.
        dat_line = f'DAT_PATH = r"{data.as_posix()}"'
        if stub_filter_um is not None:
            dat_line += f"\nSTUB_FILTER_UM = {float(stub_filter_um)}"

        patches: Dict[str, str] = {
            r"^DAT_PATH\s*=.*$": dat_line,
            r"^OUT_BASE\s*=.*$": f'OUT_BASE = r"{out.as_posix()}"',
        }
        # When filtering to one stub, also patch the stubs-list line so that
        # only that stub is processed (avoids loading all stubs unnecessarily).
        if stub_filter_um is not None:
            patches[r"^    stubs = sorted\(data\.keys\(\)\)$"] = (
                "    stubs = [s for s in sorted(data.keys()) "
                "if abs(s - STUB_FILTER_UM) < 0.5]"
            )

        patched = patch_script(
            src,
            Path(job.run_dir) / "_abcd_fit_patched.py",
            patches,
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
# Parallel ABCD fit  (one subprocess per stub, concurrent via ThreadPoolExecutor)
# ─────────────────────────────────────────────────────────────────────────────
def run_abcd_fit_parallel(
    registry: JobRegistry,
    data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Parallel ABCD fit: run one subprocess per stub, merge results.

    Discovers stubs from the ``.dat`` file, spawns ``min(n_stubs, max_workers)``
    concurrent child processes (one per stub), then merges their individual
    ``abcd_fit_results.csv`` files into a single
    ``abcd_fit_results_merged.csv`` in the output directory.

    This is typically 4-6× faster than :func:`run_abcd_fit` on a multi-core
    machine (each stub's 3×5 matrix of fits is embarrassingly parallel).

    Parameters
    ----------
    registry
        Shared :class:`~comsol_suite.jobs.JobRegistry`.
    data_path
        ``stub_length_sweep.dat`` to fit. Defaults to the bundled bridge/003
        sweep.
    output_dir
        Root output directory. Each stub gets a ``stub_<N>um/`` subdirectory.
    max_workers
        Maximum parallel processes. Defaults to ``len(stubs)`` (fully parallel).
    background
        Submit as a background job (default) and return a ``job_id`` immediately.
    debug
        Log command lines into each stub's log file.

    Returns
    -------
    dict
        ``{job_id, status, n_stubs}`` — poll with ``get_job_result``.
        Completed result includes ``merged_csv``, ``output_files``, and a per-stub
        ``stubs_ok`` dict showing which stubs succeeded.
    """
    cfg = load_config()
    src = cfg.script("abcd_fit")
    data = Path(data_path) if data_path else cfg.datum("bridge003_sweep")

    if not src.is_file():
        return {"ok": False, "error": f"abcd_fit.py not found: {src}"}
    if not data.is_file():
        return {"ok": False, "error": f"sweep data not found: {data}"}

    stubs = _get_stubs_from_dat(data)
    if not stubs:
        return {"ok": False, "error": "no stubs found in data file"}

    n_workers = min(len(stubs), max_workers or len(stubs))

    def supervisor(job: Job) -> Dict[str, Any]:
        out_root = Path(output_dir) if output_dir else Path(job.run_dir) / "abcd_out"
        out_root.mkdir(parents=True, exist_ok=True)

        # Build one patched script per stub.
        stub_tasks: Dict[float, tuple] = {}
        for stub_um in stubs:
            stub_out = out_root / f"stub_{int(stub_um)}um"
            stub_out.mkdir(parents=True, exist_ok=True)

            dat_line = (
                f'DAT_PATH = r"{data.as_posix()}"\n'
                f"STUB_FILTER_UM = {float(stub_um)}"
            )
            patched = patch_script(
                src,
                Path(job.run_dir) / f"_abcd_fit_stub{int(stub_um)}.py",
                {
                    r"^DAT_PATH\s*=.*$": dat_line,
                    r"^OUT_BASE\s*=.*$": f'OUT_BASE = r"{stub_out.as_posix()}"',
                    r"^    stubs = sorted\(data\.keys\(\)\)$": (
                        "    stubs = [s for s in sorted(data.keys()) "
                        "if abs(s - STUB_FILTER_UM) < 0.5]"
                    ),
                },
            )
            stub_log = Path(job.run_dir) / f"stub_{int(stub_um)}.log"
            stub_tasks[stub_um] = (patched, stub_log, stub_out)

        # Run all stubs concurrently — each in its own subprocess (no JPype,
        # pure numpy/scipy, so N concurrent processes are safe and fast).
        stubs_ok: Dict[str, bool] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(
                    run_command,
                    [cfg.python_bin, str(patched)],
                    stub_log,
                    cwd=stub_out,
                    timeout_s=300,
                    debug=debug,
                ): stub_um
                for stub_um, (patched, stub_log, stub_out) in stub_tasks.items()
            }
            for future in concurrent.futures.as_completed(futures):
                stub_um = futures[future]
                try:
                    res = future.result()
                    stubs_ok[str(int(stub_um))] = res.ok
                except Exception as exc:
                    stubs_ok[str(int(stub_um))] = False
                    # Append error to the supervisor log.
                    with open(job.log_path, "a") as lf:
                        lf.write(f"\n[parallel] stub {stub_um} raised: {exc}\n")

        # Merge individual abcd_fit_results.csv files into one.
        merged_csv = out_root / "abcd_fit_results_merged.csv"
        header: Optional[str] = None
        data_rows: List[str] = []
        for stub_um in sorted(stubs):
            _, _, stub_out = stub_tasks[stub_um]
            stub_csv = stub_out / "Data" / "abcd_fit_results.csv"
            if stub_csv.is_file():
                with open(stub_csv) as fh:
                    lines = fh.readlines()
                if lines:
                    if header is None:
                        header = lines[0]
                    data_rows.extend(lines[1:])

        if header and data_rows:
            with open(merged_csv, "w") as fh:
                fh.write(header)
                fh.writelines(data_rows)

        all_csvs = _collect(out_root, ["*.csv"])
        all_figs = _collect(out_root, ["*.png"])
        ok = all(stubs_ok.values()) and merged_csv.is_file()
        return {
            "ok": ok,
            "stubs_ok": stubs_ok,
            "merged_csv": str(merged_csv) if merged_csv.is_file() else None,
            "output_files": all_csvs,
            "figures": all_figs,
            "summary": (
                f"Parallel fit: {sum(stubs_ok.values())}/{len(stubs)} stubs OK; "
                f"{len(data_rows)} result rows merged"
            ),
            "error": None if ok else "one or more stub fits failed (see stub_*.log)",
        }

    job = registry.submit("abcd_fit_parallel", supervisor, background=background)
    return {"job_id": job.job_id, "status": job.status, "n_stubs": len(stubs)}


# ─────────────────────────────────────────────────────────────────────────────
# Generic fitting tool  (any user-supplied Python fitting script)
# ─────────────────────────────────────────────────────────────────────────────
def run_generic_fit(
    registry: JobRegistry,
    fit_script: str,
    data_path: str,
    output_dir: Optional[str] = None,
    dat_path_var: str = "DAT_PATH",
    out_base_var: str = "OUT_BASE",
    extra_patches: Optional[Dict[str, str]] = None,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run a user-supplied fitting script with path redirection.

    Patches the user's script so it reads from ``data_path`` and writes to
    ``output_dir``, then launches it as a background job. This lets any script
    that follows the ``DAT_PATH`` / ``OUT_BASE`` convention (or uses custom
    variable names supplied via ``dat_path_var`` / ``out_base_var``) plug in
    without modification.

    For scripts that need additional substitutions (e.g. a different topology
    parameter, number of junctions, port impedance), pass ``extra_patches`` as
    a ``{regex_pattern: replacement_line}`` dict — the same format accepted by
    :func:`~comsol_suite.runner.patch_script`.

    Parameters
    ----------
    registry
        Shared :class:`~comsol_suite.jobs.JobRegistry`.
    fit_script
        Absolute path to the fitting Python script.
    data_path
        Absolute path to the S-parameter data file the script should read.
    output_dir
        Where to write results. Defaults to ``runs/<job_id>/fit_out/``.
    dat_path_var
        Variable name in the script that holds the data-file path.
        Defaults to ``DAT_PATH`` (the convention used by ``abcd_fit.py``).
    out_base_var
        Variable name in the script that holds the output root path.
        Defaults to ``OUT_BASE``.
    extra_patches
        Additional ``{regex_pattern: replacement}`` pairs forwarded to
        :func:`~comsol_suite.runner.patch_script` (``require_all=False``).
    background
        Submit as a background job (default) and return ``{job_id, status}``
        immediately.
    debug
        Echo the command line into the job log.

    Returns
    -------
    dict
        ``{job_id, status}`` — poll with ``get_job_result`` for ``output_files``
        and ``figures``.
    """
    cfg = load_config()
    src = Path(fit_script)
    data = Path(data_path)

    if not src.is_file():
        return {"ok": False, "error": f"fit script not found: {src}"}
    if not data.is_file():
        return {"ok": False, "error": f"data file not found: {data}"}

    def worker(job: Job) -> Dict[str, Any]:
        out = Path(output_dir) if output_dir else Path(job.run_dir) / "fit_out"
        out.mkdir(parents=True, exist_ok=True)

        patches: Dict[str, str] = {
            rf"^{re.escape(dat_path_var)}\s*=.*$": (
                f'{dat_path_var} = r"{data.as_posix()}"'
            ),
            rf"^{re.escape(out_base_var)}\s*=.*$": (
                f'{out_base_var} = r"{out.as_posix()}"'
            ),
        }
        if extra_patches:
            patches.update(extra_patches)

        patched = patch_script(
            src,
            Path(job.run_dir) / "_generic_fit_patched.py",
            patches,
            require_all=False,  # custom scripts may omit some vars
        )
        res = run_command(
            [cfg.python_bin, patched],
            log_path=Path(job.log_path),
            cwd=out,
            timeout_s=900,
            debug=debug,
        )
        results = _collect(out, ["*.csv"])
        return {
            "ok": res.ok,
            "output_files": results,
            "figures": _collect(out, ["*.png"]),
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": f"generic_fit finished rc={res.returncode}",
            "error": None if res.ok else "script returned non-zero (see run.log)",
        }

    job = registry.submit("generic_fit", worker, background=background)
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

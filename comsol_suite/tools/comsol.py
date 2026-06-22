"""COMSOL stage tools: build, validate, sweep, export.

These tools wrap the project's COMSOL driver scripts. Running them for real
requires a live COMSOL connection (the ``mph`` package + a COMSOL server/licence),
so every tool here defaults to ``dry_run=True``: it validates arguments and
probes COMSOL reachability **without solving**.

Real-run path (dry_run=False)
------------------------------
Two strategies are used, depending on the script:

  * **Path-patching** (CAD/fitting style): module-level path variables
    (``ROOT``, ``OUT_DIR``, ``BASE_MPH``, ``CSV_OUT``) are rewritten in a
    temporary copy so all output lands in the job's ``runs/<id>/`` directory and
    the originals are never touched. This is how ``build_comsol_model`` and
    ``run_stub_length_sweep`` work for the project's specific JTWPA scripts.

  * **Standard-interface convention** (``run_custom_comsol_build``): for
    *user-supplied* scripts that may target a different device entirely, the
    tool patches three well-known variables that the script is expected to
    define: ``OUT_DIR`` (output directory), ``PARAM_OVERRIDES`` (a dict of
    COMSOL parameter name → value string), and ``MATERIAL_OVERRIDES`` (a dict
    of ``comp.material.property`` → value string). This lets any script follow
    the same interface without being modified.

MPH files
---------
Every real-run tool explicitly collects and surfaces ``*.mph`` paths in the
returned result (key ``mph_paths``). These are the COMSOL model files the user
can open in the COMSOL GUI to inspect geometry, mesh, physics settings, and
S-parameter results for themselves.

General qubit chip workflow
---------------------------
This suite is not limited to JTWPA devices. For a general qubit chip
(transmon, fluxonium, resonator grid, etc.) the same three stages apply::

    1. run_custom_cad(my_device.py)            → GDS with qubit geometry
    2. run_custom_comsol_build(my_build.py,    → *.mph with EM model + solve
           geom_params = {"air_height": "1[mm]",
                          "sub_t": "525[um]"},
           material_params = {"eps_r": "11.7",
                              "loss_tan": "1e-6"})
    3. run_stub_length_sweep / run_abcd_fit    → fitted circuit parameters

For an eigenfrequency study (qubit frequency / dispersive shift extraction)
instead of an S-parameter sweep, write the COMSOL build script to set up an
Eigenfrequency study and call ``run_custom_comsol_build`` — the tool is
agnostic to study type.

See ``docs/ARCHITECTURE.md`` for the full explanation.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import patch_script, run_command


# ─────────────────────────────────────────────────────────────────────────────
# Connection health
# ─────────────────────────────────────────────────────────────────────────────
def comsol_health_check(
    comsol_host: Optional[str] = None,
    comsol_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Check that COMSOL is reachable from this machine, without solving.

    Verifies (a) the ``mph`` Python package is importable and (b) — when a host
    is configured — that a TCP connection to ``host:port`` succeeds.

    Returns
    -------
    dict
        ``{ok, mph_available, host, port, host_reachable, detail}``.
    """
    cfg = load_config()
    host = comsol_host or cfg.comsol_host
    port = int(comsol_port or cfg.comsol_port)

    try:
        import mph  # noqa: F401
        mph_available = True
        mph_detail = "mph import OK"
    except Exception as exc:
        mph_available = False
        mph_detail = f"mph not importable: {type(exc).__name__}: {exc}"

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


def _preflight(
    tool: str,
    argv: List[str],
    patches_applied: Optional[Dict[str, str]],
    comsol_host: Optional[str],
    comsol_port: Optional[int],
    mph_save_plan: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Dry-run report: what WOULD run, including patch plan and MPH save paths."""
    cfg = load_config()
    health = comsol_health_check(comsol_host, comsol_port or cfg.comsol_port)
    return {
        "dry_run": True,
        "tool": tool,
        "would_run": [str(a) for a in argv],
        "patches_applied": patches_applied or {},
        "mph_files_would_save": mph_save_plan or [],
        "comsol_health": health,
        "ready": health["ok"],
        "note": (
            "Validated only. Re-call with dry_run=False once connected to "
            "COMSOL to launch the solve as a background job. Inspect the "
            "mph_files_would_save paths in the COMSOL GUI to verify geometry, "
            "mesh, physics, and results."
        ),
    }


def _launch_with_script(
    registry: JobRegistry,
    tool: str,
    argv: List[str],
    out: Path,
    debug: bool,
    timeout_s: float,
) -> Dict[str, Any]:
    """Submit an already-built argv as a background job, collecting MPH + CSV."""
    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        res = run_command(argv, log_path=Path(job.log_path),
                         cwd=out, timeout_s=timeout_s, debug=debug)
        # Collect all outputs, with MPH files surfaced separately.
        all_files: List[str] = []
        for pat in ["*.mph", "*.csv", "*.dat", "*.s2p"]:
            all_files.extend(str(p) for p in out.rglob(pat))
        all_files = sorted(set(all_files))
        mph_paths = [f for f in all_files if f.endswith(".mph")]
        return {
            "ok": res.ok,
            "output_files": all_files,
            "mph_paths": mph_paths,  # COMSOL models the user can open and inspect
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": f"{tool} finished rc={res.returncode}; "
                       f"{len(mph_paths)} MPH file(s) saved",
            "error": None if res.ok else f"{tool} failed (see run.log)",
        }

    job = registry.submit(tool, worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Build COMSOL model  (JTWPA-specific wrapper around recreate_and_solve.py)
# ─────────────────────────────────────────────────────────────────────────────
def build_comsol_model(
    registry: JobRegistry,
    gds_path: str,
    junction_inductance_ph: float = 280.0,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    geom_params: Optional[Dict[str, str]] = None,
    material_params: Optional[Dict[str, str]] = None,
    comsol_cores: int = 4,
    build_only: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Build (and lightly solve) the COMSOL EM model from a GDS.

    Wraps ``recreate_and_solve.py`` (build → coarse solve → S-parameter extract).
    This is the **JTWPA-specific** tool; for a different device (transmon qubit,
    resonator chip, etc.) use ``run_custom_comsol_build`` instead.

    Geometry and material adjustments
    ----------------------------------
    Pass ``geom_params`` and ``material_params`` as ``{name: value_string}``
    dicts.  These are injected into the COMSOL model before the solve via
    ``m.param().set(name, value)`` and equivalent material-property calls.
    Value strings must be in COMSOL syntax, e.g. ``"500[um]"``, ``"11.7"``.

    Example::

        build_comsol_model(
            gds_path   = "...",
            geom_params = {"add_stub_length": "350[um]",
                           "air_box_height":  "1[mm]"},
            material_params = {"sub_eps_r": "11.9",   # silicon εr
                               "sub_loss_tan": "1e-6"},
            comsol_cores = 8,
        )

    The dry-run shows exactly which patches would be applied and where the
    ``.mph`` files would be saved; no solving occurs.

    MPH output
    ----------
    When ``dry_run=False`` and ``build_only=False`` the job saves two MPH
    files:
      * ``<output_dir>/model_built.mph``   — geometry + mesh (open before solve)
      * ``<output_dir>/model_solved.mph``  — includes solved S-parameter data

    Both paths are returned in ``result["mph_paths"]`` when the job completes
    so the user can open them directly in the COMSOL GUI.

    Parameters
    ----------
    gds_path
        GDS produced by ``generate_cad`` or ``run_custom_cad``.
    junction_inductance_ph
        Josephson inductance per junction (``juncL`` parameter), pH.
        Injected via ``geom_params`` if not already present there.
    comsol_host
        Override the configured COMSOL host for this call.
    geom_params
        COMSOL parameter overrides: ``{param_name: "value[unit]"}`` dict.
        Applied before the geometry rebuild runs, e.g.
        ``{"add_stub_length": "350[um]", "metal_t": "200[nm]"}``.
    material_params
        Material property overrides: ``{property_name: "value"}`` dict.
        Applied to material nodes, e.g.
        ``{"sub_eps_r": "11.7", "sub_loss_tan": "1e-7"}``.
    comsol_cores
        COMSOL solver thread count (default 4).
    build_only
        If True, stop after build (skip the solve).
        Useful to inspect the MPH before a long solve.
    dry_run
        If True (default), validate + health-check only. Set False to launch.
    """
    cfg = load_config()
    src = cfg.script("comsol_build")
    out = Path(output_dir) if output_dir else cfg.runs_dir / "comsol_build"

    if not src.is_file():
        return {"ok": False, "error": f"COMSOL build script not found: {src}"}

    # Merge junction inductance into geom_params so it's visible in dry-run.
    all_geom = dict(geom_params or {})
    all_geom.setdefault("juncL", f"{junction_inductance_ph}e-12[H]")

    # Patches applied to the script (read-only original is never touched).
    patches_plan = {
        r"^ROOT\s*=.*$": f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
        r"^OUT_DIR\s*=.*$": f'OUT_DIR = r"{out.as_posix()}"',
        r"^sys\.path\.insert.*$":
            f'sys.path.insert(0, r"{src.parent.as_posix()}")',
    }
    if all_geom:
        patches_plan["GEOM_PARAM_OVERRIDES (injected)"] = repr(all_geom)
    if material_params:
        patches_plan["MATERIAL_PARAM_OVERRIDES (injected)"] = repr(material_params)

    # The script accepts: --cores N  [--build-only]
    argv = [cfg.python_bin, src, "--cores", str(comsol_cores)]
    if build_only:
        argv.append("--build-only")

    mph_plan = [
        str(out / "model_built.mph"),
        *([] if build_only else [str(out / "model_solved.mph")]),
    ]

    if dry_run:
        return _preflight("build_comsol_model", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    # Real run: patch script and submit as background job.
    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src,
            Path(job.run_dir) / "_build_patched.py",
            {
                r"^ROOT\s*=.*$": f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^OUT_DIR\s*=.*$": f'OUT_DIR = r"{out.as_posix()}"',
                r"^sys\.path\.insert.*$":
                    f'sys.path.insert(0, r"{src.parent.as_posix()}")',
                # Inject param overrides as module-level dicts; the build
                # function reads these if present (see recreate_and_solve.py).
                r"^REF_CSV\s*=.*$": (
                    f'REF_CSV = r"{cfg.chip_sim_root.as_posix()}'
                    f'/java_outputs/sparams_clean.csv"\n'
                    f'GEOM_PARAM_OVERRIDES = {repr(all_geom)}\n'
                    f'MATERIAL_PARAM_OVERRIDES = {repr(material_params or {})}'
                ),
            },
        )
        real_argv = [cfg.python_bin, str(patched), "--cores", str(comsol_cores)]
        if build_only:
            real_argv.append("--build-only")
        return _run_and_collect(real_argv, Path(job.log_path), out, debug, 7200)

    job = registry.submit("build_comsol_model", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


def _run_and_collect(
    argv: List[str],
    log_path: Path,
    out: Path,
    debug: bool,
    timeout_s: float,
) -> Dict[str, Any]:
    """Run argv and collect output files, surfacing MPH paths separately."""
    res = run_command(argv, log_path=log_path, cwd=out,
                      timeout_s=timeout_s, debug=debug)
    all_files = sorted(set(
        str(p) for pat in ["*.mph", "*.csv", "*.dat", "*.s2p"]
        for p in out.rglob(pat)
    ))
    mph_paths = [f for f in all_files if f.endswith(".mph")]
    ok = res.ok
    return {
        "ok": ok,
        "output_files": all_files,
        "mph_paths": mph_paths,
        "returncode": res.returncode,
        "duration_s": round(res.duration_s, 2),
        "summary": f"finished rc={res.returncode}; {len(mph_paths)} MPH file(s) saved",
        "error": None if ok else "process failed (see run.log)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stub-length sweep  (JTWPA-specific wrapper around sweep_stub_length.py)
# ─────────────────────────────────────────────────────────────────────────────
def run_stub_length_sweep(
    registry: JobRegistry,
    mph_path: str,
    stub_lengths_um: List[float],
    freq_ghz: List[float],
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    port: str = "both",
    resume: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Parametric stub-length sweep: extract complex S-parameters per stub.

    For each stub length the script rebuilds the COMSOL geometry and mesh, solves
    the EM study, and extracts complex S11/S21 (and S12/S22 if ``port="both"``).
    The output ``.dat`` file is consumed directly by ``run_abcd_fit`` and
    ``run_abcd_fit_parallel``.

    Each stub is also saved as its own interim ``stub_<N>um.mph`` that you can
    open in the COMSOL GUI to inspect the mesh, physics, and per-stub solution.

    Parameters
    ----------
    mph_path
        Path to a solved ``.mph`` returned by ``build_comsol_model``.
        Must have the ``stdQ`` frequency study defined.
    stub_lengths_um
        Stub lengths to sweep in µm, e.g. ``[300, 320, 340, 360, 380, 400]``.
    freq_ghz
        Frequency points in GHz, e.g. ``[1, 2, 3, ..., 16]``.
    comsol_cores
        COMSOL solver threads (default 4).
    port
        Which port to excite: ``"1"``, ``"2"``, or ``"both"`` (default).
        Extracting both ports gives the full 2×2 S-matrix (S11/S21/S12/S22).
    resume
        If True, skip stub lengths whose rows are already in the output CSV —
        allows safe crash-and-resume for long sweeps.
    dry_run
        If True (default), validate + health-check only. Set False to launch.
    """
    cfg = load_config()
    src = cfg.script("comsol_sweep")
    out = Path(output_dir) if output_dir else cfg.runs_dir / "comsol_sweep"

    if not src.is_file():
        return {"ok": False, "error": f"COMSOL sweep script not found: {src}"}

    if port not in ("1", "2", "both"):
        return {"ok": False, "error": f"port must be '1', '2', or 'both'; got {port!r}"}

    csv_out = out / "stub_length_sweep.dat"

    # The real argv uses the script's actual argument names.
    argv = (
        [cfg.python_bin, src,
         "--cores", str(comsol_cores),
         "--stubs"] + [str(int(s)) for s in stub_lengths_um] +
        ["--freqs"] + [str(f) for f in freq_ghz] +
        ["--out", str(csv_out)]
    )
    if port != "both":
        argv += ["--port", port]
    if resume:
        argv.append("--resume")

    # Script patches: redirect BASE_MPH, OUT_DIR, CSV_OUT to our run directory.
    patches_plan = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$": f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$": f'CSV_OUT = r"{csv_out.as_posix()}"',
    }

    # Per-stub MPH files saved by the script (one per stub, plus the final CSV).
    mph_plan = [str(out / f"stub_sweep_{int(s)}um.mph") for s in stub_lengths_um]

    if dry_run:
        return _preflight("run_stub_length_sweep", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src,
            Path(job.run_dir) / "_sweep_patched.py",
            {
                r"^ROOT\s*=.*$": f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
                r"^OUT_DIR\s*=.*$": f'OUT_DIR = r"{out.as_posix()}"',
                r"^CSV_OUT\s*=.*$": f'CSV_OUT = r"{csv_out.as_posix()}"',
            },
        )
        real_argv = (
            [cfg.python_bin, str(patched),
             "--cores", str(comsol_cores),
             "--stubs"] + [str(int(s)) for s in stub_lengths_um] +
            ["--freqs"] + [str(f) for f in freq_ghz] +
            ["--out", str(csv_out)]
        )
        if port != "both":
            real_argv += ["--port", port]
        if resume:
            real_argv.append("--resume")
        return _run_and_collect(real_argv, Path(job.log_path), out, debug, 21600)

    job = registry.submit("run_stub_length_sweep", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Eigenfrequency study  (resonances + Q-factors without a frequency sweep)
# ─────────────────────────────────────────────────────────────────────────────
def run_eigenfrequency_study(
    registry: JobRegistry,
    mph_path: str,
    n_modes: int = 5,
    freq_start_ghz: float = 1.0,
    freq_stop_ghz: float = 20.0,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Find resonance frequencies and Q-factors via COMSOL eigenvalue solver.

    Uses the eigenfrequency_analysis.py script to add an Eigenfrequency study
    to an existing .mph, solve it, and extract complex eigenvalues:
      f_resonance = Re(λ)           [GHz]
      Q_factor    = Re(λ) / (2·|Im(λ)|)
      loss_rate   = |Im(λ)| · 2π   [MHz]

    Run this FIRST for any new device (~5 min) to locate resonances before
    committing to a full frequency sweep (~30 min+).

    Parameters
    ----------
    mph_path
        Path to a built .mph (from build_comsol_model or run_custom_comsol_build).
        Must have EMW physics with PEC boundaries.
    n_modes
        Number of eigenvalues to compute (1–20, default 5).
    freq_start_ghz
        Search window lower bound in GHz (default 1.0).
    freq_stop_ghz
        Search window upper bound in GHz (default 20.0).
    comsol_cores
        COMSOL solver threads (default 4).
    dry_run
        If True (default), validate + health-check only. Set False to launch.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, would_run, patches_applied, mph_files_would_save,
        comsol_health, ready}``
        *Real-run*: ``{job_id, status}`` — poll ``get_job_result`` for
        ``{mph_paths, output_files, eigenfrequencies_csv}``.
    """
    # Input validation BEFORE loading config (fast fail without hitting disk).
    if not 1 <= n_modes <= 20:
        return {"ok": False, "error": f"n_modes must be 1–20, got {n_modes}"}
    if freq_start_ghz >= freq_stop_ghz:
        return {"ok": False,
                "error": f"freq_start_ghz ({freq_start_ghz}) must be < "
                         f"freq_stop_ghz ({freq_stop_ghz})"}

    cfg = load_config()
    src = cfg.script("comsol_eigenfreq")
    out = Path(output_dir) if output_dir else cfg.runs_dir / "comsol_eigenfreq"
    csv_out = out / "eigenfrequencies.csv"

    argv = [
        cfg.python_bin, src,
        "--modes", str(n_modes),
        "--freq-start", str(freq_start_ghz),
        "--freq-stop", str(freq_stop_ghz),
        "--out", str(csv_out),
        "--cores", str(comsol_cores),
    ]
    if debug:
        argv.append("--debug")

    # Patches redirect module-level path constants in the script copy.
    patches_plan = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
    }
    mph_plan = [str(out / "eigenfrequency_result.mph")]

    if dry_run:
        return _preflight("run_eigenfrequency_study", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src,
            Path(job.run_dir) / "_eigenfreq_patched.py",
            {
                r"^ROOT\s*=.*$":     f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
                r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
                r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
            },
        )
        real_argv = [
            cfg.python_bin, str(patched),
            "--modes", str(n_modes),
            "--freq-start", str(freq_start_ghz),
            "--freq-stop", str(freq_stop_ghz),
            "--out", str(csv_out),
            "--cores", str(comsol_cores),
        ]
        if debug:
            real_argv.append("--debug")
        return _run_and_collect(real_argv, Path(job.log_path), out, debug, 3600)

    job = registry.submit("run_eigenfrequency_study", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Generic COMSOL build  (user-supplied script for any device / study type)
# ─────────────────────────────────────────────────────────────────────────────
def run_custom_comsol_build(
    registry: JobRegistry,
    build_script: str,
    output_dir: Optional[str] = None,
    out_dir_var: str = "OUT_DIR",
    geom_params: Optional[Dict[str, str]] = None,
    material_params: Optional[Dict[str, str]] = None,
    param_overrides_var: str = "PARAM_OVERRIDES",
    material_overrides_var: str = "MATERIAL_OVERRIDES",
    comsol_host: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run any user-supplied COMSOL build script, injecting geometry and material params.

    This is the **device-agnostic** COMSOL tool. Use it for any chip that is not
    the specific 21-junction JTWPA (transmon qubits, resonator grids, fluxonium,
    quantum memory chips, etc.).

    Standard interface convention
    ------------------------------
    Your build script should define these module-level variables (the tool
    patches them before running):

    .. code-block:: python

        # In your_build_script.py
        OUT_DIR            = "/default/output"   # → redirected by MCP tool
        PARAM_OVERRIDES    = {}                  # → dict injected by MCP tool
        MATERIAL_OVERRIDES = {}                  # → dict injected by MCP tool

    The script is then responsible for reading and applying these dicts::

        # Apply geometry parameters
        for name, val in PARAM_OVERRIDES.items():
            m.param().set(name, val)

        # Apply material overrides
        # (exact API depends on your material node structure)
        for path, val in MATERIAL_OVERRIDES.items():
            node, prop = path.rsplit(".", 1)
            m.component("comp1").material(node).propertyGroup("def").set(prop, val)

        # Save the built model so the user can open it
        pymodel.save(os.path.join(OUT_DIR, "model_built.mph"))

    Example: transmon qubit chip
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    .. code-block:: python

        run_custom_comsol_build(
            build_script  = "/path/to/transmon_build.py",
            geom_params   = {
                "pad_width":    "200[um]",   # transmon cross-pad width
                "pad_height":   "300[um]",   # transmon cross-pad height
                "res_length":   "5000[um]",  # readout resonator length
                "sub_t":        "525[um]",   # silicon substrate thickness
                "air_height":   "1[mm]",     # air-box above chip
            },
            material_params = {
                "sub_eps_r":    "11.7",      # silicon relative permittivity
                "sub_loss_tan": "1e-6",      # substrate loss tangent
                "metal_sigma":  "5.88e7",    # aluminum conductivity (S/m)
            },
            comsol_cores = 8,
            dry_run = True,   # set False when COMSOL is reachable
        )

    Parameters
    ----------
    build_script
        Absolute path to the COMSOL build Python script.
    out_dir_var
        Variable name in the script that holds the output directory path.
        Defaults to ``OUT_DIR`` (the convention used by the project scripts).
    geom_params
        COMSOL geometry parameter overrides: ``{param_name: "value[unit]"}``
        dict. Injected into the ``PARAM_OVERRIDES`` variable in the script.
        The script applies these via ``m.param().set(name, val)``.
    material_params
        Material property overrides: ``{property_name: "value"}`` dict.
        Injected into the ``MATERIAL_OVERRIDES`` variable. The script applies
        these to material nodes as appropriate for the model.
    param_overrides_var
        Variable name for the geometry-param dict (default ``PARAM_OVERRIDES``).
    material_overrides_var
        Variable name for the material-param dict (default ``MATERIAL_OVERRIDES``).
    comsol_host
        Override the configured COMSOL host for this call.
    comsol_cores
        COMSOL solver thread count (default 4).
    dry_run
        If True (default), health-check + patch plan only. Set False to solve.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, patches_applied, mph_files_would_save, comsol_health, ready}``
        *Real-run*: ``{job_id, status}`` — poll ``get_job_result`` for
        ``{mph_paths, output_files, summary}``.
    """
    cfg = load_config()
    src = Path(build_script)

    if not src.is_file():
        return {"ok": False, "error": f"build script not found: {src}"}

    out = Path(output_dir) if output_dir else (
        cfg.runs_dir / f"comsol_custom_{src.stem}"
    )

    patches_plan: Dict[str, str] = {
        rf"^{re.escape(out_dir_var)}\s*=.*$": (
            f'{out_dir_var} = r"{out.as_posix()}"'
        ),
        rf"^{re.escape(param_overrides_var)}\s*=.*$": (
            f'{param_overrides_var} = {repr(geom_params or {})}'
        ),
        rf"^{re.escape(material_overrides_var)}\s*=.*$": (
            f'{material_overrides_var} = {repr(material_params or {})}'
        ),
    }

    argv = [cfg.python_bin, src, "--cores", str(comsol_cores)]
    mph_plan = [str(out / "model_built.mph"), str(out / "model_solved.mph")]

    if dry_run:
        return _preflight("run_custom_comsol_build", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src,
            Path(job.run_dir) / f"_{src.stem}_patched.py",
            {
                rf"^{re.escape(out_dir_var)}\s*=.*$": (
                    f'{out_dir_var} = r"{out.as_posix()}"'
                ),
                rf"^{re.escape(param_overrides_var)}\s*=.*$": (
                    f'{param_overrides_var} = {repr(geom_params or {})}'
                ),
                rf"^{re.escape(material_overrides_var)}\s*=.*$": (
                    f'{material_overrides_var} = {repr(material_params or {})}'
                ),
            },
            require_all=False,  # scripts may define only some of these vars
        )
        real_argv = [cfg.python_bin, str(patched), "--cores", str(comsol_cores)]
        return _run_and_collect(real_argv, Path(job.log_path), out, debug, 14400)

    job = registry.submit("run_custom_comsol_build", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Touchstone export
# ─────────────────────────────────────────────────────────────────────────────
def export_touchstone(
    registry: JobRegistry,
    csv_path: str,
    output_path: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Convert an extracted S-parameter CSV into a Touchstone ``.s2p`` file.

    Wraps ``export_touchstone.py``. This step needs no COMSOL connection itself,
    but is kept in the COMSOL stage for consistency. Defaults to dry-run.
    Set ``dry_run=False`` to run it (it is safe to run offline).
    """
    cfg = load_config()
    src = cfg.script("comsol_export")
    out = Path(output_path) if output_path else cfg.runs_dir / "touchstone"
    argv = [cfg.python_bin, src, "--csv", csv_path, "--out", str(out)]

    if not src.is_file():
        return {"ok": False, "error": f"export script not found: {src}"}
    if dry_run:
        return _preflight("export_touchstone", argv, {"csv_path": csv_path},
                          None, cfg.comsol_port, [str(out / "sparams.s2p")])
    return _launch_with_script(registry, "export_touchstone", argv,
                               out.parent, debug, timeout_s=300)

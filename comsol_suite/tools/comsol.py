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
        result = _preflight("build_comsol_model", argv, patches_plan,
                            comsol_host, cfg.comsol_port, mph_plan)
        result["deprecation_notice"] = (
            "build_comsol_model is JTWPA-specific and deprecated. "
            "Use run_custom_comsol_build with your device's build script instead."
        )
        return result

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
        result = _preflight("run_stub_length_sweep", argv, patches_plan,
                            comsol_host, cfg.comsol_port, mph_plan)
        result["deprecation_notice"] = (
            "run_stub_length_sweep is deprecated. "
            "Use run_geometry_param_sweep(param_name='stub_length', "
            "study_type='frequency_domain', ...) for equivalent behavior."
        )
        return result

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
    extract_fields: bool = False,
    path_selections: Optional[List[str]] = None,
    node_groups: Optional[List[str]] = None,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Find resonance frequencies and Q-factors via COMSOL eigenvalue solver.

    Device-agnostic: works for any .mph with EMW physics.  Optionally extracts
    per-mode field energies and |E| path integrals for mode identification and
    coupling extraction.

    Base outputs (all modes):
      f_resonance = Re(λ)           [GHz]
      Q_factor    = Re(λ) / (2·|Im(λ)|)
      loss_rate   = |Im(λ)| · 2π   [MHz]

    Extended outputs (``extract_fields=True``):
      We_J, Wm_J          — electric / magnetic energy per mode [J]
      path_<sel_name>     — ∫|E| ds along each named selection [V]
      V_re/V_im_<ng_name> — complex voltage at each node group [V]

    Parameters
    ----------
    mph_path
        Built .mph with EMW physics + PEC boundaries.
    n_modes
        Number of eigenvalues to compute (1–20, default 5).
    freq_start_ghz / freq_stop_ghz
        Eigenvalue search window [GHz].
    extract_fields
        Enable per-mode field energy and path-integral extraction.
        Uses ``eigenfreq_with_fields.py`` when True.
    path_selections
        COMSOL edge selection names for |E| path integrals (used with
        ``extract_fields=True``).  E.g. ``["resonator_path", "qubit_path"]``.
    node_groups
        COMSOL selection names for voltage extraction (used with
        ``extract_fields=True``).  E.g. ``["JJ_node", "readout_port"]``.
    comsol_cores
        Solver threads (default 4).
    dry_run
        If True (default), validate + health-check only.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, would_run, patches_applied, comsol_health, ready}``
        *Real-run*: ``{job_id, status}`` — poll ``get_job_result`` for
        ``{mph_paths, output_files}``.
    """
    if not 1 <= n_modes <= 20:
        return {"ok": False, "error": f"n_modes must be 1–20, got {n_modes}"}
    if freq_start_ghz >= freq_stop_ghz:
        return {"ok": False,
                "error": f"freq_start_ghz ({freq_start_ghz}) must be < "
                         f"freq_stop_ghz ({freq_stop_ghz})"}

    cfg = load_config()
    # Choose base or extended script depending on field extraction request.
    script_key = "comsol_eigenfreq_fields" if extract_fields else "comsol_eigenfreq"
    src = cfg.script(script_key)
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
    if extract_fields:
        argv.append("--extract-fields")
        if path_selections:
            argv += ["--path-selections"] + list(path_selections)
        if node_groups:
            argv += ["--node-groups"] + list(node_groups)
    if debug:
        argv.append("--debug")

    patches_plan: Dict[str, str] = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
    }
    if extract_fields and path_selections:
        patches_plan[r"^PATH_SELECTIONS\s*=.*$"] = (
            f'PATH_SELECTIONS = {repr(list(path_selections))}'
        )
    if extract_fields and node_groups:
        patches_plan[r"^NODE_GROUPS\s*=.*$"] = (
            f'NODE_GROUPS = {repr(list(node_groups))}'
        )
    mph_plan = [str(out / "eigenfrequency_result.mph")]

    if dry_run:
        return _preflight("run_eigenfrequency_study", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patch_dict: Dict[str, str] = {
            r"^ROOT\s*=.*$":     f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
            r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
            r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
            r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
        }
        if extract_fields and path_selections:
            patch_dict[r"^PATH_SELECTIONS\s*=.*$"] = (
                f'PATH_SELECTIONS = {repr(list(path_selections))}'
            )
        if extract_fields and node_groups:
            patch_dict[r"^NODE_GROUPS\s*=.*$"] = (
                f'NODE_GROUPS = {repr(list(node_groups))}'
            )
        patched = patch_script(
            src, Path(job.run_dir) / "_eigenfreq_patched.py",
            patch_dict, require_all=False,
        )
        real_argv = [
            cfg.python_bin, str(patched),
            "--modes", str(n_modes),
            "--freq-start", str(freq_start_ghz),
            "--freq-stop", str(freq_stop_ghz),
            "--out", str(csv_out),
            "--cores", str(comsol_cores),
        ]
        if extract_fields:
            real_argv.append("--extract-fields")
            if path_selections:
                real_argv += ["--path-selections"] + list(path_selections)
            if node_groups:
                real_argv += ["--node-groups"] + list(node_groups)
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
def validate_geometry(
    registry: JobRegistry,
    mph_path: str,
    checker_script: str,
    mph_path_var: Optional[str] = "MPH_PATH",
    reference_vertices_csv: Optional[str] = None,
    reference_vertices_csv_var: Optional[str] = "REFERENCE_VERTICES_CSV",
    extra_args: Optional[List[str]] = None,
    comsol_host: Optional[str] = None,
    comsol_cores: int = 4,
    output_dir: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Validate a built .mph against any user-supplied checker script.

    Device-agnostic gate: the mandatory check before trusting a solve. Unlike
    ``verify_cad`` (which imports a GDS checker in-process — no COMSOL needed),
    this tool needs a live COMSOL/``mph`` connection to open the model, so it
    follows the COMSOL-stage convention: defaults to ``dry_run=True`` and
    launches real runs as a background job.

    Two calling conventions are supported, so existing checker scripts don't
    need to be rewritten:

    1. **Patchable-variable convention** (``mph_path_var`` set, the default
       ``"MPH_PATH"``): the checker script defines a module-level ``MPH_PATH``
       string constant (and optionally ``REFERENCE_VERTICES_CSV``), a
       ``main() -> int`` (0 = pass), and ends with ``sys.exit(main())``. This
       tool patches a temporary copy of the script exactly like
       ``run_custom_comsol_build`` patches ``OUT_DIR`` — the original is never
       touched.
    2. **Positional-argument convention** (``mph_path_var=None``): the checker
       already accepts the model path as its first CLI argument and exits 0 on
       pass — e.g. ``simulations/stitching/scripts/verify_metal_raster.py``.
       The script is run unmodified with ``mph_path`` prepended to
       ``extra_args``.

    Parameters
    ----------
    mph_path
        Built .mph to validate (e.g. from ``build_comsol_model`` or
        ``run_custom_comsol_build``).
    checker_script
        Absolute path to the checker script.
    mph_path_var
        Module-level variable name the checker uses for the model path.
        Set to ``None`` to use the positional-argument convention instead.
    reference_vertices_csv / reference_vertices_csv_var
        Optional reference-geometry CSV and the variable name the checker
        expects it under (patchable-variable convention only).
    extra_args
        Extra CLI arguments appended after any positional ``mph_path``
        (e.g. ``["--format", "arc"]`` for ``verify_metal_raster.py``).
    comsol_cores
        COMSOL solver thread count (default 4).
    dry_run
        If True (default), validate + health-check only. Set False to launch.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, would_run, patches_applied, comsol_health, ready}``
        *Real-run*: ``{job_id, status}`` — poll ``get_job_result`` for
        ``{ok, returncode, passed, report, log_tail}``.
    """
    cfg = load_config()
    src = Path(checker_script)
    if not src.is_file():
        return {"ok": False, "error": f"checker script not found: {src}"}
    if not Path(mph_path).is_file():
        return {"ok": False, "error": f"mph_path not found: {mph_path}"}

    out = Path(output_dir) if output_dir else cfg.runs_dir / f"validate_geometry_{src.stem}"
    extra = list(extra_args or [])

    patches_plan: Dict[str, str] = {}
    if mph_path_var is not None:
        patches_plan[rf"^{re.escape(mph_path_var)}\s*=.*$"] = (
            f'{mph_path_var} = r"{mph_path}"'
        )
        if reference_vertices_csv and reference_vertices_csv_var:
            patches_plan[rf"^{re.escape(reference_vertices_csv_var)}\s*=.*$"] = (
                f'{reference_vertices_csv_var} = r"{reference_vertices_csv}"'
            )
        argv = [cfg.python_bin, src] + extra
    else:
        argv = [cfg.python_bin, src, mph_path] + extra

    if dry_run:
        return _preflight("validate_geometry", argv, patches_plan,
                          comsol_host, cfg.comsol_port, [])

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        if mph_path_var is not None:
            patched = patch_script(
                src, Path(job.run_dir) / f"_{src.stem}_patched.py",
                patches_plan, require_all=False,
            )
            real_argv = [cfg.python_bin, str(patched)] + extra
        else:
            real_argv = [cfg.python_bin, str(src), mph_path] + extra
        res = run_command(real_argv, log_path=Path(job.log_path), cwd=out,
                          timeout_s=1800, debug=debug)
        reports = sorted(str(p) for p in out.rglob("*.json"))
        return {
            "ok": res.ok,
            "passed": res.returncode == 0,
            "returncode": res.returncode,
            "checker_script": str(src),
            "report": reports,
            "log_tail": res.log_tail(30),
            "error": None if res.ok else f"validate_geometry failed (see run.log)",
        }

    job = registry.submit("validate_geometry", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


def run_coupling_extraction(
    eigenfreq_csv: str,
    mode1_path_col: str,
    mode2_path_col: str,
    lumped_inductance_H: float,
    mode1_label: str = "mode1",
    mode2_label: str = "mode2",
) -> Dict[str, Any]:
    """Extract coupling g between two modes from eigenfrequency field data.

    Pure-Python post-processing — no COMSOL connection required.  Reads the
    CSV produced by ``run_eigenfrequency_study`` with ``extract_fields=True``
    and applies Jaynes-Cummings energy-partition analysis.

    Device-agnostic: works for qubit-resonator, resonator-filter, or any
    two-mode coupled system where one mode has a lumped inductive element.

    Parameters
    ----------
    eigenfreq_csv
        CSV path from ``run_eigenfrequency_study`` (must have ``We_J``,
        ``Wm_J``, and the two path-integral columns).
    mode1_path_col
        Column name for the |E| path integral of mode 1 (the "resonator-like"
        mode).  Typically ``"path_resonator_path"``.
    mode2_path_col
        Column name for the |E| path integral of mode 2 (the "qubit-like"
        mode).  Typically ``"path_qubit_path"``.
    lumped_inductance_H
        Inductance of the lumped element (Josephson junction, coupling
        inductor, etc.) in Henry.
    mode1_label / mode2_label
        Human-readable labels for the two modes in the return dict.

    Returns
    -------
    dict
        ``{g_Hz, chi_Hz, f_mode1_Hz, f_mode2_Hz, mode_labels,
        participation_ratio, error}``.
    """
    import csv as _csv
    import math

    try:
        from .circuit_physics import (
            extract_coupling_g, dispersive_shift,
            transmon_anharmonicity, inductance_to_josephson_energy,
            cap_to_charging_energy, calc_cap_from_eigenfreq,
        )
    except ImportError as exc:
        return {"ok": False, "error": f"circuit_physics not available: {exc}"}

    if not Path(eigenfreq_csv).is_file():
        return {"ok": False, "error": f"CSV not found: {eigenfreq_csv}"}

    with open(eigenfreq_csv, newline="") as fh:
        rows = list(_csv.DictReader(fh))

    if not rows:
        return {"ok": False, "error": "CSV is empty"}

    required = {"freq_ghz", "We_J", "Wm_J", mode1_path_col, mode2_path_col}
    missing = required - set(rows[0].keys())
    if missing:
        return {
            "ok": False,
            "error": (
                f"CSV missing columns: {sorted(missing)}. "
                f"Run run_eigenfrequency_study with extract_fields=True and "
                f"path_selections matching mode1_path_col / mode2_path_col."
            ),
        }

    def _float(row: dict, key: str) -> float:
        v = row.get(key, "")
        return float(v) if v not in ("", "None", "nan") else math.nan

    # Identify modes: the mode with larger path integral along path1 → mode1,
    # larger along path2 → mode2.
    mode1_row: Optional[Dict] = None
    mode2_row: Optional[Dict] = None
    for row in rows:
        p1 = abs(_float(row, mode1_path_col))
        p2 = abs(_float(row, mode2_path_col))
        if math.isnan(p1) or math.isnan(p2):
            continue
        if p1 >= p2:
            if mode1_row is None or p1 > abs(_float(mode1_row, mode1_path_col)):
                mode1_row = row
        else:
            if mode2_row is None or p2 > abs(_float(mode2_row, mode2_path_col)):
                mode2_row = row

    if mode1_row is None or mode2_row is None:
        return {
            "ok": False,
            "error": (
                "Could not identify two distinct modes from path integrals. "
                "Check path_selections names match the COMSOL model."
            ),
        }

    f1_Hz = _float(mode1_row, "freq_ghz") * 1e9
    We1 = _float(mode1_row, "We_J")
    Wm1 = _float(mode1_row, "Wm_J")
    f2_Hz = _float(mode2_row, "freq_ghz") * 1e9

    try:
        g_Hz = extract_coupling_g(f1_Hz, We1, Wm1, f2_Hz)
    except Exception as exc:
        return {"ok": False, "error": f"coupling extraction failed: {exc}"}

    # Estimate anharmonicity from inductance for dispersive shift calculation.
    try:
        f0_Hz = f2_Hz  # qubit-like mode frequency
        C_F = calc_cap_from_eigenfreq(lumped_inductance_H, f0_Hz)
        EC_Hz = cap_to_charging_energy(C_F)
        EJ_Hz = inductance_to_josephson_energy(lumped_inductance_H)
        anh_Hz = transmon_anharmonicity(EJ_Hz, EC_Hz)
        chi_Hz = dispersive_shift(f2_Hz, anh_Hz, f1_Hz, g_Hz)
    except Exception:
        anh_Hz = float("nan")
        chi_Hz = float("nan")

    r = math.sqrt(abs(We1 - Wm1) / Wm1) if Wm1 > 0 else float("nan")

    return {
        "ok": True,
        "g_Hz": g_Hz,
        "g_MHz": g_Hz / 1e6,
        "chi_Hz": chi_Hz,
        "chi_MHz": chi_Hz / 1e6,
        "anharmonicity_Hz": anh_Hz,
        "f_mode1_Hz": f1_Hz,
        "f_mode2_Hz": f2_Hz,
        "mode_labels": {mode1_label: f1_Hz, mode2_label: f2_Hz},
        "participation_ratio": r,
        "error": None,
    }


def run_geometry_param_sweep(
    registry: JobRegistry,
    mph_path: str,
    param_name: str,
    param_values: List[float],
    param_unit: str = "um",
    study_type: str = "eigenfrequency",
    n_modes: int = 5,
    freq_start_ghz: float = 1.0,
    freq_stop_ghz: float = 20.0,
    extract_fields: bool = False,
    path_selections: Optional[List[str]] = None,
    node_groups: Optional[List[str]] = None,
    freq_points_ghz: Optional[List[float]] = None,
    port: str = "both",
    resume: bool = False,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Parametric sweep over any COMSOL geometry parameter.

    Device-agnostic replacement for ``run_stub_length_sweep``.  Works for any
    named COMSOL parameter: slider length, coupler angle, gap width, junction
    radius, or any other geometry dimension.

    For each value the script:
      1. Sets ``param_name = val[param_unit]`` in the COMSOL parameter table.
      2. Rebuilds geometry and mesh.
      3. Runs the configured study (eigenfrequency or frequency-domain).
      4. Extracts results and appends a row to the output CSV.
      5. Saves an interim ``.mph`` for GUI inspection.

    Parameters
    ----------
    mph_path
        Built .mph with the target geometry parameter defined.
    param_name
        COMSOL parameter name to sweep (e.g. ``"l_slider_single"``,
        ``"delta_angle_coupler"``, ``"stub_length"``).
    param_values
        Values to sweep.  Units given by ``param_unit``.
    param_unit
        COMSOL unit string (default ``"um"``).  Use ``"deg"`` for angles,
        ``"H"`` for inductance, etc.
    study_type
        ``"eigenfrequency"`` (default) or ``"frequency_domain"``.
    n_modes
        Eigenvalue count for eigenfrequency sweeps (default 5).
    freq_start_ghz / freq_stop_ghz
        Search window for eigenfrequency sweeps.
    extract_fields
        Extract per-mode We, Wm, path integrals (eigenfrequency only).
    path_selections / node_groups
        Named COMSOL selections for field extraction (see
        ``run_eigenfrequency_study``).
    freq_points_ghz
        Frequency evaluation points for frequency-domain sweeps.
    port
        Port excitation for frequency-domain: ``"1"``, ``"2"``, ``"both"``.
    resume
        Skip parameter values already present in the output CSV.
    dry_run
        If True (default), validate + health-check only.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, would_run, patches_applied, comsol_health}``
        *Real-run*: ``{job_id, status}``
    """
    if not param_values:
        return {"ok": False, "error": "param_values must not be empty"}
    if study_type not in ("eigenfrequency", "frequency_domain"):
        return {"ok": False,
                "error": f"study_type must be 'eigenfrequency' or 'frequency_domain', "
                         f"got {study_type!r}"}
    if study_type == "frequency_domain" and not freq_points_ghz:
        return {"ok": False,
                "error": "freq_points_ghz required for frequency_domain sweep"}

    cfg = load_config()
    src = cfg.script("comsol_geometry_sweep")
    out = (Path(output_dir) if output_dir
           else cfg.runs_dir / f"geom_sweep_{param_name}")
    csv_out = out / f"{param_name}_sweep.csv"

    argv = [
        cfg.python_bin, src,
        "--param-name", param_name,
        "--param-values"] + [str(v) for v in param_values] + [
        "--param-unit", param_unit,
        "--study-type", study_type,
        "--cores", str(comsol_cores),
        "--out", str(csv_out),
    ]
    if study_type == "eigenfrequency":
        argv += ["--n-modes", str(n_modes),
                 "--freq-start", str(freq_start_ghz),
                 "--freq-stop", str(freq_stop_ghz)]
        if extract_fields:
            argv.append("--extract-fields")
            if path_selections:
                argv += ["--path-selections"] + list(path_selections)
            if node_groups:
                argv += ["--node-groups"] + list(node_groups)
    else:
        argv += ["--freq-points"] + [str(f) for f in (freq_points_ghz or [])]
        argv += ["--port", port]
    if resume:
        argv.append("--resume")
    if debug:
        argv.append("--debug")

    patches_plan = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
    }
    mph_plan = [str(out / f"{param_name}_{v}.mph") for v in param_values]

    if dry_run:
        return _preflight("run_geometry_param_sweep", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src, Path(job.run_dir) / "_geom_sweep_patched.py",
            {
                r"^ROOT\s*=.*$":     f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
                r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
                r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
            },
        )
        return _run_and_collect(
            [cfg.python_bin, str(patched)] + argv[2:],  # reuse argv after script
            Path(job.log_path), out, debug, 21600,
        )

    job = registry.submit("run_geometry_param_sweep", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


def run_decay_rate_sweep(
    registry: JobRegistry,
    mph_path: str,
    sweep_param: str,
    sweep_values: List[float],
    sweep_unit: str,
    junction_selection: str,
    port_selection: str,
    shunt_capacitance_F: float,
    freq_ghz: Optional[float] = None,
    Z0_Ohm: float = 50.0,
    resume: bool = False,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Sweep a lumped-element parameter and extract decay rate at each point.

    Device-agnostic: works for Purcell decay (sweep Josephson inductance),
    drive-port coupling (sweep spoke count), resonator external Q (sweep
    coupling gap), or any decay channel computable from a voltage ratio.

    For each sweep value the script:
      1. Sets ``sweep_param = val[sweep_unit]`` in COMSOL.
      2. Rebuilds geometry and mesh.
      3. Runs the frequency-domain study at ``freq_ghz`` (or the model default).
      4. Extracts ``V_junction`` and ``V_port`` at the named selections.
      5. Computes ``kappa = |V_port/V_junction|² / (Z0 · C)``  [rad/s].
      6. Computes ``T1 = 1/kappa``  [s].

    Parameters
    ----------
    mph_path
        Built .mph with the sweep parameter and both selections defined.
    sweep_param
        COMSOL parameter name to sweep.
    sweep_values
        Values to sweep; units given by ``sweep_unit``.
    sweep_unit
        COMSOL unit string (e.g. ``"H"``, ``"um"``, ``"1"``).
    junction_selection
        COMSOL selection name for the lumped element voltage node.
    port_selection
        COMSOL selection name for the output port voltage node.
    shunt_capacitance_F
        Shunt capacitance [F] for κ = |V_p/V_j|² / (Z0·C).
    freq_ghz
        Fixed drive frequency [GHz].  When ``None`` the model's first
        frequency study point is used.
    Z0_Ohm
        Port impedance [Ω] (default 50).
    resume
        Skip values already in the output CSV.
    dry_run
        If True (default), validate + health-check only.

    Returns
    -------
    dict
        *Dry-run*: ``{dry_run, would_run, patches_applied, comsol_health}``
        *Real-run*: ``{job_id, status}``
    """
    if not sweep_values:
        return {"ok": False, "error": "sweep_values must not be empty"}

    cfg = load_config()
    src = cfg.script("comsol_decay_sweep")
    out = (Path(output_dir) if output_dir
           else cfg.runs_dir / f"decay_sweep_{sweep_param}")
    csv_out = out / f"{sweep_param}_decay_sweep.csv"

    argv = [
        cfg.python_bin, src,
        "--sweep-param", sweep_param,
        "--sweep-values"] + [str(v) for v in sweep_values] + [
        "--sweep-unit", sweep_unit,
        "--junction-selection", junction_selection,
        "--port-selection", port_selection,
        "--shunt-cap-fF", str(shunt_capacitance_F * 1e15),
        "--z0", str(Z0_Ohm),
        "--cores", str(comsol_cores),
        "--out", str(csv_out),
    ]
    if freq_ghz is not None:
        argv += ["--freq-ghz", str(freq_ghz)]
    if resume:
        argv.append("--resume")
    if debug:
        argv.append("--debug")

    patches_plan = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
    }
    mph_plan = [str(out / f"{sweep_param}_{v}.mph") for v in sweep_values]

    if dry_run:
        return _preflight("run_decay_rate_sweep", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)

    def worker(job: Job) -> Dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src, Path(job.run_dir) / "_decay_sweep_patched.py",
            {
                r"^ROOT\s*=.*$":     f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
                r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
                r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
            },
        )
        return _run_and_collect(
            [cfg.python_bin, str(patched)] + argv[2:],
            Path(job.log_path), out, debug, 14400,
        )

    job = registry.submit("run_decay_rate_sweep", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


def run_parameter_inversion(
    registry: JobRegistry,
    mph_path: str,
    param_name: str,
    param_range: List[float],
    target_value: float,
    n_sweep_points: int = 9,
    param_unit: str = "um",
    mode_index: int = 1,
    post_physics: Optional[str] = None,
    lumped_inductance_H: Optional[float] = None,
    poly_degree: int = 3,
    freq_start_ghz: float = 1.0,
    freq_stop_ghz: float = 20.0,
    n_modes: int = 5,
    comsol_host: Optional[str] = None,
    output_dir: Optional[str] = None,
    comsol_cores: int = 4,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Sweep a geometry parameter and invert to find the value that hits a target.

    One-shot wrapper for the sweep → polynomial-fit → invert workflow:

      1. Runs ``geometry_param_sweep.py`` (eigenfrequency study) over
         ``n_sweep_points`` evenly-spaced values in ``param_range``.
      2. Reads the resulting CSV and extracts ``freq_ghz`` for ``mode_index``.
      3. Optionally converts the raw eigenfrequency to ``fge`` via transmon
         perturbation theory if ``post_physics="transmon"`` is set.
      4. Fits a degree-``poly_degree`` polynomial and inverts to find the
         ``param_name`` value where the observable ≈ ``target_value`` [GHz].

    The result is the recommended geometry dimension to target — no COMSOL
    iteration needed.

    Parameters
    ----------
    mph_path
        Built .mph with the target geometry parameter defined.
    param_name
        COMSOL parameter to invert (e.g. ``"l_slider_single"``, ``"d_q"``).
    param_range
        ``[min, max]`` in ``param_unit`` to sweep.
    target_value
        Target observable in GHz (raw eigenfrequency, or ``fge`` when
        ``post_physics="transmon"``).
    n_sweep_points
        Number of evenly-spaced sweep values (default 9; use 7–11 for a
        coarse calibration sweep).
    param_unit
        COMSOL unit string (default ``"um"``).  Use ``"deg"`` for angles,
        ``"nH"`` for inductance, etc.
    mode_index
        1-based eigenmode index to track (mode 1 = lowest in search window).
    post_physics
        ``"transmon"`` to convert eigenfrequency → ``fge`` before inverting.
        Requires ``lumped_inductance_H``.  Use ``None`` (default) for bare
        resonators where the eigenfrequency IS the target observable.
    lumped_inductance_H
        Josephson inductance [H] — required when ``post_physics="transmon"``.
    poly_degree
        Polynomial degree for the calibration fit (default 3).
    freq_start_ghz / freq_stop_ghz
        Eigenvalue search window.  Tune so the target mode stays inside.
    n_modes
        Number of eigenvalues per sweep point (default 5).
    dry_run
        True (default) = plan only.  False = launch background job.

    Returns
    -------
    dict
        *Dry-run*: sweep plan + ``inversion`` sub-dict describing post-processing.
        *Real-run*: ``{job_id, status}`` — poll ``get_job_result`` for
        ``{ok, recommended_value, param_unit, expected_freq_ghz, target_ghz,
        residual_ghz, calibration_csv, sweep_data, note}``.
    """
    # ── Validate ──────────────────────────────────────────────────────────────
    if len(param_range) != 2 or float(param_range[0]) >= float(param_range[1]):
        return {"ok": False,
                "error": "param_range must be [min, max] with min < max"}
    if n_sweep_points < 3:
        return {"ok": False, "error": "n_sweep_points must be ≥ 3"}
    if post_physics is not None and post_physics not in ("transmon",):
        return {"ok": False,
                "error": f"post_physics must be 'transmon' or None, got {post_physics!r}"}
    if post_physics == "transmon" and lumped_inductance_H is None:
        return {"ok": False,
                "error": "lumped_inductance_H is required when post_physics='transmon'"}

    # ── Build linspace of sweep values ────────────────────────────────────────
    lo, hi = float(param_range[0]), float(param_range[1])
    param_values = [
        lo + (hi - lo) * i / (n_sweep_points - 1)
        for i in range(n_sweep_points)
    ]

    cfg = load_config()
    src = cfg.script("comsol_geometry_sweep")
    out = (Path(output_dir) if output_dir
           else cfg.runs_dir / f"param_inversion_{param_name}")
    csv_out = out / f"{param_name}_inversion_sweep.csv"

    argv = (
        [cfg.python_bin, src,
         "--param-name", param_name,
         "--param-values"] + [str(round(v, 6)) for v in param_values] + [
         "--param-unit", param_unit,
         "--study-type", "eigenfrequency",
         "--n-modes", str(n_modes),
         "--freq-start", str(freq_start_ghz),
         "--freq-stop", str(freq_stop_ghz),
         "--cores", str(comsol_cores),
         "--out", str(csv_out)]
    )
    if debug:
        argv = list(argv) + ["--debug"]

    patches_plan = {
        r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
        r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
        r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
    }
    mph_plan = [str(out / f"{param_name}_{round(v, 6)}.mph") for v in param_values]

    if dry_run:
        plan = _preflight("run_parameter_inversion", argv, patches_plan,
                          comsol_host, cfg.comsol_port, mph_plan)
        plan["inversion"] = {
            "param_range": list(param_range),
            "n_sweep_points": n_sweep_points,
            "param_values": [round(v, 4) for v in param_values],
            "target_value_ghz": target_value,
            "mode_index": mode_index,
            "post_physics": post_physics,
            "poly_degree": poly_degree,
            "note": (
                f"After sweep: extract mode {mode_index} freq_ghz vs {param_name}, "
                f"fit degree-{poly_degree} poly, invert to find {param_name} where "
                f"{'fge' if post_physics == 'transmon' else 'eigenfreq'} ≈ "
                f"{target_value} GHz."
            ),
        }
        return plan

    # ── Real-run worker ───────────────────────────────────────────────────────
    def worker(job: Job) -> Dict[str, Any]:
        import csv as _csv
        import math

        out.mkdir(parents=True, exist_ok=True)
        patched = patch_script(
            src,
            Path(job.run_dir) / "_inversion_sweep_patched.py",
            {
                r"^ROOT\s*=.*$":     f'ROOT = r"{cfg.chip_sim_root.as_posix()}"',
                r"^BASE_MPH\s*=.*$": f'BASE_MPH = r"{mph_path}"',
                r"^OUT_DIR\s*=.*$":  f'OUT_DIR = r"{out.as_posix()}"',
                r"^CSV_OUT\s*=.*$":  f'CSV_OUT = r"{csv_out.as_posix()}"',
            },
        )
        res = run_command(
            [cfg.python_bin, str(patched)] + list(argv)[2:],
            log_path=Path(job.log_path),
            cwd=out,
            timeout_s=21600,
            debug=debug,
        )
        if not res.ok:
            return {
                "ok": False,
                "error": f"Geometry sweep failed (rc={res.returncode}); see run.log",
                "returncode": res.returncode,
            }
        if not csv_out.is_file():
            return {"ok": False, "error": f"Sweep CSV not produced: {csv_out}"}

        with open(csv_out, newline="") as fh:
            rows = list(_csv.DictReader(fh))
        if not rows:
            return {"ok": False, "error": "Sweep CSV is empty"}

        # Extract (param_value → freq_ghz) for the requested mode index.
        mode_data: Dict[float, float] = {}
        for row in rows:
            try:
                p_val = float(row[param_name])
                m_num = int(float(row.get("mode", 1)))
                freq  = float(row["freq_ghz"])
            except (KeyError, ValueError):
                continue
            if m_num == mode_index and not math.isnan(freq):
                mode_data[p_val] = freq

        if len(mode_data) < 3:
            return {
                "ok": False,
                "error": (
                    f"Only {len(mode_data)} data point(s) for mode {mode_index}; "
                    f"need ≥ 3 for a degree-{poly_degree} fit. "
                    f"Widen freq_start/freq_stop or increase n_sweep_points."
                ),
                "calibration_csv": str(csv_out),
                "raw_rows": len(rows),
            }

        p_sorted = sorted(mode_data)
        f_sorted = [mode_data[p] for p in p_sorted]

        # Optional transmon post-physics: convert f0 → fge.
        if post_physics == "transmon":
            from .circuit_physics import compute_circuit_params
            y_sorted = []
            for f_ghz in f_sorted:
                params = compute_circuit_params(
                    L_H=lumped_inductance_H, f0_Hz=f_ghz * 1e9
                )
                fge = params.get("fq_Hz")
                y_sorted.append(fge / 1e9 if fge is not None else f_ghz)
            y_label = "fge_ghz"
        else:
            y_sorted = f_sorted
            y_label = "eigenfreq_ghz"

        # Polynomial inversion.
        from .circuit_physics import polynomial_inverse
        roots = polynomial_inverse(p_sorted, y_sorted, target_value, degree=poly_degree)

        if not roots:
            return {
                "ok": False,
                "error": (
                    f"No root found for {y_label} = {target_value} GHz in "
                    f"{param_name} ∈ [{param_range[0]}, {param_range[1]}] {param_unit}. "
                    f"Observed range: [{min(y_sorted):.4f}, {max(y_sorted):.4f}] GHz. "
                    f"Widen param_range or check mode_index."
                ),
                "calibration_csv": str(csv_out),
                "sweep_data": {param_name: p_sorted, y_label: y_sorted},
            }

        recommended = roots[0]  # first (and usually unique) root in range

        # Evaluate poly at recommended to report expected output.
        import numpy as _np
        coeffs   = _np.polyfit(p_sorted, y_sorted, poly_degree)
        y_at_rec = float(_np.polyval(coeffs, recommended))
        residual = abs(y_at_rec - target_value)

        return {
            "ok": True,
            "recommended_value": round(recommended, 4),
            "param_name": param_name,
            "param_unit": param_unit,
            f"expected_{y_label}": round(y_at_rec, 6),
            "target_ghz": target_value,
            "residual_ghz": round(residual, 6),
            "all_roots": [round(r, 4) for r in roots],
            "calibration_csv": str(csv_out),
            "sweep_data": {param_name: p_sorted, y_label: y_sorted},
            "note": (
                f"Set {param_name} = {round(recommended, 4)} [{param_unit}] "
                f"to achieve {y_label} ≈ {round(y_at_rec, 4)} GHz "
                f"(target {target_value} GHz, "
                f"residual {round(residual * 1000, 2)} MHz)."
            ),
        }

    job = registry.submit("run_parameter_inversion", worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


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

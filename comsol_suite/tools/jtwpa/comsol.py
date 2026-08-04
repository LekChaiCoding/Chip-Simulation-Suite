"""JTWPA-specific COMSOL wrappers — one device family's vendored geometry.

Moved out of `..comsol` on 2026-08-04. Both functions were already marked
deprecated in favour of the device-agnostic tools beside them, and both hardcode
the vendored reference device:

  * `build_comsol_model` wraps `recreate_and_solve.py` and assumes that model's
    geometry; `run_custom_comsol_build` is the general form.
  * `run_stub_length_sweep` wraps `sweep_stub_length.py` and sweeps the one
    parameter that device has; `run_geometry_param_sweep(param_name=...)` is the
    general form.

`server.py` still exposes both under their original tool names, so nothing an MCP
client calls has changed. The point of the move is that the import path now
answers "does this generalise?" without reading the body.

The shared solve plumbing stays in `..comsol` and is imported rather than copied
— `_preflight` and `_start_solve` are not device-specific.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ...config import load_config
from ...jobs import Job, JobRegistry
from ..comsol import _preflight, _start_solve

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
    job = registry.reserve("build_comsol_model")
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
    return _start_solve(registry, job, real_argv, out, debug, 7200)


# File types every solve in this module can produce. Passed to the detached
# worker as its collect patterns, so the artifact list is built where the
# artifacts are.
SOLVE_ARTIFACT_PATTERNS = ("*.mph", "*.csv", "*.dat", "*.s2p")



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

    job = registry.reserve("run_stub_length_sweep")
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
    return _start_solve(registry, job, real_argv, out, debug, 21600)



"""MCP server entry point for the COMSOL Simulation Suite.

Registers the CAD / COMSOL / fitting tools (plus job-management and config
helpers) on a FastMCP server and serves them over stdio — the transport Claude
Code uses for local MCP servers.

Run with either::

    python -m comsol_suite
    comsol-suite                # console-script installed by pip

The tool functions themselves live in :mod:`comsol_suite.tools`; this module is
the thin registration/wiring layer. A single shared
:class:`~comsol_suite.jobs.JobRegistry` is injected into the tools that launch
background work, so its parameter is hidden from the MCP-facing signatures.

Pipeline overview (CAD → COMSOL → fitting)
-------------------------------------------
Standard 21-junction JTWPA workflow:

    1. generate_cad()               → GDS + PNG preview
    2. verify_cad(gds_path)         → geometry pass/fail
    3. build_comsol_model(gds_path) → background job (dry_run=False on COMSOL net)
    4. validate_geometry(mph_path)  → face-count + vertex multiset gate
    5. run_stub_length_sweep(...)   → stub_length_sweep.dat
    6. run_abcd_fit(data_path)      → sequential, all stubs
       OR
       run_abcd_fit_parallel(data_path) → concurrent, one subprocess per stub
    7. get_job_result(job_id)       → fitted Cg, Z0, CSV paths

Custom device workflow (user-supplied scripts):

    1. run_custom_cad(cad_script, out_gds_var="OUT_GDS")
    2. verify_cad(gds_path)          (if checker script is available)
    3. build_comsol_model / run_stub_length_sweep (same as above)
    4. run_generic_fit(fit_script, data_path, dat_path_var="DAT_PATH",
                       out_base_var="OUT_BASE",
                       extra_patches={r"^N_JCT\\s*=.*$": "N_JCT = 10"})
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .jobs import JobRegistry
from .tools import cad, comsol, fitting

# ── Shared singletons ────────────────────────────────────────────────────────
CONFIG = load_config()
REGISTRY = JobRegistry(CONFIG.runs_dir)

mcp = FastMCP("comsol-simulation-suite")


# ─────────────────────────────────────────────────────────────────────────────
# CAD stage
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def generate_cad(output_dir: Optional[str] = None, debug: bool = False) -> Dict[str, Any]:
    """Generate the 21-junction JTWPA chip GDS layout (and a preview PNG).

    This reproduces the exact CAD imported into COMSOL. Returns the path to the
    written ``.gds`` and ``.png``. Pair with ``verify_cad`` to confirm the layout
    matches the validated reference geometry.
    """
    return cad.generate_cad(output_dir=output_dir, debug=debug)


@mcp.tool()
def verify_cad(gds_path: Optional[str] = None, debug: bool = False) -> Dict[str, Any]:
    """Verify a GDS against the vertex-validated reference geometry pins.

    Runs the project's own CAD checker. ``passed=true`` means every geometric
    feature (layer bboxes, 21 junction bars, tine edges, pads, ports, centreline)
    matches the geometry measured from the built COMSOL model. Defaults to the
    repo's reference GDS if no path is given.
    """
    return cad.verify_cad(gds_path=gds_path, debug=debug)


@mcp.tool()
def run_custom_cad(
    cad_script: str,
    output_dir: Optional[str] = None,
    out_gds_var: str = "OUT_GDS",
    out_png_var: Optional[str] = "OUT_PNG",
    gds_filename: str = "output.gds",
    debug: bool = False,
) -> Dict[str, Any]:
    """Run a user-supplied GDS generation script with automatic path redirection.

    Applies the same patch-and-run approach as ``generate_cad`` but accepts any
    Python script. Set ``out_gds_var`` / ``out_png_var`` to the variable names
    used in the script (defaults match ``converter_group_recreation.py``). Use
    this to generate GDS for custom chip geometries without modifying this
    package.

    - **cad_script**: absolute path to the CAD generation Python script.
    - **out_gds_var**: variable name for the GDS output path (default ``OUT_GDS``).
    - **out_png_var**: variable name for the PNG preview (``null`` to skip).
    - **gds_filename**: filename of the output GDS (inside ``output_dir``).
    """
    return cad.run_custom_cad(
        cad_script=cad_script,
        output_dir=output_dir,
        out_gds_var=out_gds_var,
        out_png_var=out_png_var,
        gds_filename=gds_filename,
        debug=debug,
    )


# ─────────────────────────────────────────────────────────────────────────────
# COMSOL stage  (wrapped; default dry_run=True — needs a live COMSOL connection)
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def comsol_health_check(comsol_host: Optional[str] = None,
                        comsol_port: Optional[int] = None) -> Dict[str, Any]:
    """Check COMSOL reachability (mph import + TCP probe) without solving."""
    return comsol.comsol_health_check(comsol_host, comsol_port)


@mcp.tool()
def build_comsol_model(
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
    """Build the JTWPA COMSOL EM model from a GDS (build → validate → coarse solve).

    Defaults to dry-run — shows the patch plan, COMSOL health, and where the
    ``.mph`` files would be saved. Set ``dry_run=False`` on the COMSOL network
    to launch as a background job.

    **For a different device** (transmon, resonator chip, etc.) use
    ``run_custom_comsol_build`` instead.

    Geometry & material adjustments
    --------------------------------
    - **geom_params**: COMSOL parameter overrides, e.g.
      ``{"add_stub_length": "350[um]", "metal_t": "200[nm]"}``.
      Values must use COMSOL unit syntax. Applied via ``m.param().set()``.
    - **material_params**: material property overrides, e.g.
      ``{"sub_eps_r": "11.9", "sub_loss_tan": "1e-7"}``.
      Applied to the relevant material nodes before the solve.

    MPH output (when completed)
    ---------------------------
    - ``model_built.mph`` — geometry + mesh, saved before the solve.
      Open this to inspect the mesh and physics setup in the COMSOL GUI.
    - ``model_solved.mph`` — includes the solved S-parameter data.
      Open this to plot field distributions and verify port excitation.
    Both paths appear in ``result["mph_paths"]`` when the job finishes.

    - **build_only**: save ``model_built.mph`` and stop — useful to check
      geometry before committing to a long solve.
    - **comsol_cores**: solver thread count (default 4).
    """
    return comsol.build_comsol_model(
        REGISTRY, gds_path, junction_inductance_ph, comsol_host,
        output_dir, geom_params, material_params, comsol_cores,
        build_only, dry_run, debug)


@mcp.tool()
def run_custom_comsol_build(
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
    """Run any user-supplied COMSOL build script with geometry and material injection.

    The device-agnostic COMSOL tool — use this for transmon qubits, resonator
    chips, fluxonium, quantum memory chips, etc. Your script defines three
    patchable variables::

        OUT_DIR            = "/default"  # → redirected to runs/<job_id>/
        PARAM_OVERRIDES    = {}          # → your geom_params dict
        MATERIAL_OVERRIDES = {}          # → your material_params dict

    then applies them::

        for name, val in PARAM_OVERRIDES.items():
            m.param().set(name, val)
        pymodel.save(os.path.join(OUT_DIR, "model_built.mph"))

    **Geometry params** (``geom_params``) — injected as ``PARAM_OVERRIDES``:
    any COMSOL parameter you would normally set in the Parameters table, e.g.
    ``{"pad_width": "200[um]", "res_length": "5000[um]", "sub_t": "525[um]"}``.

    **Material params** (``material_params``) — injected as ``MATERIAL_OVERRIDES``:
    material property → value strings that the script applies to material nodes,
    e.g. ``{"sub_eps_r": "11.7", "sub_loss_tan": "1e-6", "metal_sigma": "5.88e7"}``.

    **MPH output** appears in ``result["mph_paths"]`` — these are the COMSOL
    models you open in the GUI to double-check geometry, mesh, and results.

    Returns ``{dry_run, patches_applied, mph_files_would_save, comsol_health}``
    in dry-run mode, or ``{job_id, status}`` to poll when ``dry_run=False``.
    """
    return comsol.run_custom_comsol_build(
        REGISTRY, build_script=build_script, output_dir=output_dir,
        out_dir_var=out_dir_var, geom_params=geom_params,
        material_params=material_params,
        param_overrides_var=param_overrides_var,
        material_overrides_var=material_overrides_var,
        comsol_host=comsol_host, comsol_cores=comsol_cores,
        dry_run=dry_run, debug=debug)


@mcp.tool()
def validate_geometry(
    mph_path: str,
    reference_vertices_csv: Optional[str] = None,
    comsol_host: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Validate a built model's geometry (face counts + full vertex multiset).

    Pass the ``mph_path`` returned from a completed ``build_comsol_model`` or
    ``run_custom_comsol_build`` job. The dry-run shows the plan; set
    ``dry_run=False`` on the COMSOL network to run the actual check.
    """
    return comsol.validate_geometry(mph_path, reference_vertices_csv,
                                    comsol_host, dry_run)


@mcp.tool()
def run_stub_length_sweep(
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

    Feeds directly into ``run_abcd_fit`` / ``run_abcd_fit_parallel``. Each stub
    is saved as an interim ``stub_<N>um.mph`` that can be opened in the COMSOL
    GUI to inspect the per-stub solution.

    - **mph_path**: solved model from ``build_comsol_model`` (needs ``stdQ`` study).
    - **stub_lengths_um**: e.g. ``[300, 320, 340, 360, 380, 400]``.
    - **freq_ghz**: frequency points, e.g. ``[1, 2, 3, 4, ..., 16]``.
    - **comsol_cores**: solver thread count (default 4).
    - **port**: ``"1"``, ``"2"``, or ``"both"`` — ``"both"`` extracts the full
      2×2 S-matrix (S11/S21/S12/S22).
    - **resume**: skip stubs already in the output CSV (safe crash-resume).

    Completed result includes ``mph_paths`` (one file per stub) and the
    ``stub_length_sweep.dat`` consumed by the fitting tools.
    """
    return comsol.run_stub_length_sweep(
        REGISTRY, mph_path, stub_lengths_um, freq_ghz, comsol_host,
        output_dir, comsol_cores, port, resume, dry_run, debug)


@mcp.tool()
def run_eigenfrequency_study(
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

    Run this FIRST for any new device (~5 min) to locate resonances before
    committing to a full frequency sweep (~30 min+).

    Eigenvalue physics:
      - f_resonance = Re(λ)           [GHz]
      - Q_factor    = Re(λ) / (2·|Im(λ)|)
      - loss_rate   = |Im(λ)| · 2π   [MHz]

    - **mph_path**: built .mph with EMW physics + PEC boundaries.
    - **n_modes**: number of eigenvalues to find (1–20, default 5).
    - **freq_start_ghz** / **freq_stop_ghz**: eigenvalue search window [GHz].
    - **comsol_cores**: solver threads (default 4).
    - **dry_run**: True (default) = validate only; False = launch background job.

    Returns ``{job_id, status}`` on real-run. Poll ``get_job_result`` for
    ``{mph_paths, eigenfrequencies_csv}`` when the job finishes.
    """
    return comsol.run_eigenfrequency_study(
        REGISTRY, mph_path, n_modes, freq_start_ghz, freq_stop_ghz,
        comsol_host, output_dir, comsol_cores, dry_run, debug)


@mcp.tool()
def export_touchstone(csv_path: str, output_path: Optional[str] = None,
                      dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """Convert an extracted S-parameter CSV to a Touchstone ``.s2p`` file."""
    return comsol.export_touchstone(REGISTRY, csv_path, output_path, dry_run, debug)


# ─────────────────────────────────────────────────────────────────────────────
# Fitting stage
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def run_abcd_fit(
    data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    stub_filter_um: Optional[float] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Fit the lumped circuit from a stub-length sweep via the Python ABCD fitter.

    Tests 3 topologies × 5 objectives per stub; writes a results CSV with fitted
    Cg and implied Z0. Returns a ``job_id`` — poll with ``get_job_status`` /
    ``get_job_result``. Defaults to the bundled bridge/003 sweep data.

    - **stub_filter_um**: restrict the fit to a single stub length (µm). Useful
      for quickly re-fitting one stub or for manual parallelisation.
      Use ``run_abcd_fit_parallel`` to auto-parallelize all stubs at once.
    """
    return fitting.run_abcd_fit(REGISTRY, data_path=data_path,
                                output_dir=output_dir,
                                stub_filter_um=stub_filter_um, debug=debug)


@mcp.tool()
def run_abcd_fit_parallel(
    data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Parallel ABCD fit: one subprocess per stub, results merged when all done.

    Discovers stub lengths from the ``.dat`` file, launches them concurrently
    (up to ``max_workers`` processes), and merges all ``abcd_fit_results.csv``
    files into ``abcd_fit_results_merged.csv``. Typically 4-6× faster than
    the sequential ``run_abcd_fit``.

    Returns ``{job_id, status, n_stubs}`` immediately. Poll ``get_job_result``
    for ``{stubs_ok, merged_csv, output_files, figures}``.
    """
    return fitting.run_abcd_fit_parallel(
        REGISTRY, data_path=data_path, output_dir=output_dir,
        max_workers=max_workers, debug=debug)


@mcp.tool()
def run_generic_fit(
    fit_script: str,
    data_path: str,
    output_dir: Optional[str] = None,
    dat_path_var: str = "DAT_PATH",
    out_base_var: str = "OUT_BASE",
    extra_patches: Optional[Dict[str, str]] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run any user-supplied Python fitting script with path redirection.

    Patches ``dat_path_var`` and ``out_base_var`` in the script to point at the
    user's data and output directory, then runs it as a background job. Pass
    ``extra_patches`` (a ``{regex_pattern: replacement_line}`` dict) for
    additional variable substitutions — e.g. to override topology constants,
    junction counts, or port impedance.

    - **fit_script**: absolute path to the fitting Python script.
    - **data_path**: absolute path to the ``.dat`` / ``.csv`` S-parameter file.
    - **dat_path_var**: variable holding the data path (default ``DAT_PATH``).
    - **out_base_var**: variable holding the output root (default ``OUT_BASE``).
    - **extra_patches**: ``{regex: replacement}`` for extra script customisation.

    Returns ``{job_id, status}`` — poll ``get_job_result`` for ``output_files``
    and ``figures``.
    """
    return fitting.run_generic_fit(
        REGISTRY, fit_script=fit_script, data_path=data_path,
        output_dir=output_dir, dat_path_var=dat_path_var,
        out_base_var=out_base_var, extra_patches=extra_patches, debug=debug)


@mcp.tool()
def fit_stub_sweep(debug: bool = False) -> Dict[str, Any]:
    """Fit a single Cg per stub via the Julia fitter (needs Julia env).

    Returns a ``job_id``. Requires Julia + the project's JosephsonCircuits.jl
    environment to be installed.
    """
    return fitting.fit_stub_sweep(REGISTRY, debug=debug)


@mcp.tool()
def analyze_dispersion(debug: bool = False) -> Dict[str, Any]:
    """Run the Julia Bloch dispersion / delta-k analysis (needs Julia env).

    Returns a ``job_id``. Requires the fit-results CSV from ``fit_stub_sweep``.
    """
    return fitting.analyze_dispersion(REGISTRY, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# Job management + introspection
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return status + recent log lines for a background job."""
    job = REGISTRY.get(job_id)
    if job is None:
        return {"error": f"unknown job_id: {job_id}"}
    out = {"job_id": job.job_id, "tool": job.tool, "status": job.status,
           "elapsed_s": job.elapsed_s, "error": job.error}
    try:
        from pathlib import Path
        lines = Path(job.log_path).read_text(encoding="utf-8",
                                             errors="replace").splitlines()
        out["log_tail"] = "\n".join(lines[-25:])
    except OSError:
        out["log_tail"] = ""
    return out


@mcp.tool()
def get_job_result(job_id: str) -> Dict[str, Any]:
    """Return the full result (output files, summary) of a finished job."""
    job = REGISTRY.get(job_id)
    if job is None:
        return {"error": f"unknown job_id: {job_id}"}
    return {"job_id": job.job_id, "tool": job.tool, "status": job.status,
            "elapsed_s": job.elapsed_s, "error": job.error, "result": job.result}


@mcp.tool()
def list_jobs() -> List[Dict[str, Any]]:
    """List all known jobs (most recent first)."""
    return [{"job_id": j.job_id, "tool": j.tool, "status": j.status,
             "created_at": j.created_at, "elapsed_s": j.elapsed_s}
            for j in REGISTRY.list()]


@mcp.tool()
def describe_config() -> Dict[str, Any]:
    """Show the resolved paths / COMSOL host / interpreters for this machine.

    Useful first call to confirm the suite found the pipeline scripts and data.
    """
    return load_config().as_dict()


def main() -> None:
    """Console-script / ``python -m`` entry point: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

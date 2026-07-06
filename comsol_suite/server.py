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
    2. verify_cad(gds_path)         → geometry pass/fail (user-facing; optional)
    3. build_comsol_model(gds_path) → background job (dry_run=False on COMSOL net)
    4. run_stub_length_sweep(...)   → stub_length_sweep.dat
    5. run_abcd_fit(data_path)      → sequential, all stubs
       OR
       run_abcd_fit_parallel(data_path) → concurrent, one subprocess per stub
    6. get_job_result(job_id)       → fitted Cg, Z0, CSV paths

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
from .tools import cad, comsol, fitting, circuit_physics, design_params, qleap

# ── Shared singletons ────────────────────────────────────────────────────────
CONFIG = load_config()
REGISTRY = JobRegistry(CONFIG.runs_dir)

mcp = FastMCP("comsol-simulation-suite")


# ─────────────────────────────────────────────────────────────────────────────
# CAD stage
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def generate_cad(
    cad_script: Optional[str] = None,
    out_gds_var: str = "OUT_GDS",
    out_png_var: Optional[str] = "OUT_PNG",
    output_dir: Optional[str] = None,
    gds_filename: Optional[str] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Generate a chip GDS layout from any GDS generation script.

    Device-agnostic: works for JTWPA, transmon qubit, resonator, waveguide, or any
    gdstk-based layout script. When ``cad_script`` is omitted, runs the project
    default (config key: ``cad_generator``, currently the JTWPA script) for backward
    compatibility.

    - **cad_script**: absolute path to any Python GDS generation script.
      Set ``None`` to use the project default.
    - **out_gds_var**: variable name the script uses for the GDS output path
      (default ``OUT_GDS``). Override to match your script's variable.
    - **out_png_var**: variable for the PNG preview (``None`` to skip preview).
    - **gds_filename**: override the output ``.gds`` filename within ``output_dir``.

    Pair with ``verify_cad`` to confirm geometry against a custom checker.
    """
    return cad.generate_cad(
        cad_script=cad_script,
        out_gds_var=out_gds_var,
        out_png_var=out_png_var,
        output_dir=output_dir,
        gds_filename=gds_filename,
        debug=debug,
    )


@mcp.tool()
def verify_cad(
    gds_path: Optional[str] = None,
    checker_script: Optional[str] = None,
    gds_var: str = "RECR",
    debug: bool = False,
) -> Dict[str, Any]:
    """Verify a GDS against any geometry checker script.

    Device-agnostic: provide any Python checker that defines a ``main() -> int``
    and a module-level string constant. When ``checker_script`` is omitted,
    runs the project default JTWPA checker (backward compatible).

    - **checker_script**: absolute path to any checker script.
      The script must define ``main() -> int`` (0 = pass) and a module-level
      string constant whose name matches ``gds_var``.
    - **gds_var**: name of the GDS path constant in the checker script
      (default ``"RECR"`` — matches the JTWPA checker).
      Override to match your script, e.g. ``"GDS_PATH"``.
    - **gds_path**: override the GDS path that the checker validates.
      Defaults to the project reference GDS.

    Copy ``scripts/checker_template.py`` to write a checker for any device.
    ``passed=true`` means ``main()`` returned 0.
    """
    return cad.verify_cad(gds_path=gds_path, checker_script=checker_script,
                          gds_var=gds_var, debug=debug)


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


@mcp.tool()
def assemble_geometry(
    components: List[Dict[str, Any]],
    output_path: str,
    top_cell_name: str = "assembly",
    merge_refs: bool = True,
) -> Dict[str, Any]:
    """Assemble multiple GDS components into one top-level layout.

    Device-agnostic: use for 4-qubit unit cells, resonator arrays, multi-chip
    assemblies, or any combination of sub-GDS components. Each component is
    placed as a gdstk Reference (``merge_refs=True``) or flattened
    (``merge_refs=False``).

    - **components**: list of placement dicts, each with keys:
        - ``gds_path`` (str, required): path to the sub-GDS file.
        - ``cell_name`` (str, required): cell within that GDS to place.
        - ``x_um`` / ``y_um`` (float, default 0): origin in µm.
        - ``rotation_deg`` (float, default 0): CCW rotation in degrees.
        - ``magnification`` (float, default 1): scale factor.
        - ``x_reflection`` (bool, default False): mirror about the x-axis.
    - **output_path**: where to write the assembled GDS.
    - **top_cell_name**: name of the top-level cell in the output.
    - **merge_refs**: ``True`` = keep as gdstk References (faster, smaller
      file); ``False`` = flatten all polygons into the top cell.

    Returns ``{ok, output_path, n_components, bbox, error}``.
    """
    return cad.assemble_geometry(
        components=components,
        output_path=output_path,
        top_cell_name=top_cell_name,
        merge_refs=merge_refs,
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
    """Build the JTWPA COMSOL EM model from a GDS (build → coarse solve).

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

    .. deprecated::
        ``build_comsol_model`` is JTWPA-specific. For a different device
        (transmon, resonator, etc.) use ``run_custom_comsol_build`` instead.
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

    .. deprecated::
        ``run_stub_length_sweep`` is JTWPA-specific. Use
        ``run_geometry_param_sweep(param_name='stub_length',
        study_type='frequency_domain', ...)`` for equivalent behavior
        with any COMSOL geometry parameter.
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

    Run this FIRST for any new device (~5 min) to locate resonances before
    committing to a full frequency sweep (~30 min+). Optionally extracts
    per-mode field energies and |E| path integrals for coupling analysis.

    Eigenvalue physics:
      - f_resonance = Re(λ)           [GHz]
      - Q_factor    = Re(λ) / (2·|Im(λ)|)
      - loss_rate   = |Im(λ)| · 2π   [MHz]

    - **mph_path**: built .mph with EMW physics + PEC boundaries.
    - **n_modes**: number of eigenvalues to find (1–20, default 5).
    - **freq_start_ghz** / **freq_stop_ghz**: eigenvalue search window [GHz].
    - **extract_fields**: also extract ``emw.intWe``, ``emw.intWm``, and |E|
      path integrals along ``path_selections``. Uses the extended script
      ``eigenfreq_with_fields.py``. Pass the output CSV to
      ``run_coupling_extraction`` to compute g, χ, participation ratio.
    - **path_selections**: list of COMSOL named selections for |E| path
      integrals, e.g. ``["resonator_path", "qubit_path"]``. Supply the
      names you defined in the COMSOL model's geometry.
    - **node_groups**: list of COMSOL node selections for voltage extraction,
      e.g. ``["junction_node", "port_node"]``.
    - **comsol_cores**: solver threads (default 4).
    - **dry_run**: True (default) = validate only; False = launch background job.

    Returns ``{job_id, status}`` on real-run. Poll ``get_job_result`` for
    ``{mph_paths, eigenfrequencies_csv}`` when the job finishes.
    """
    return comsol.run_eigenfrequency_study(
        REGISTRY, mph_path, n_modes, freq_start_ghz, freq_stop_ghz,
        extract_fields, path_selections, node_groups,
        comsol_host, output_dir, comsol_cores, dry_run, debug)


@mcp.tool()
def export_touchstone(csv_path: str, output_path: Optional[str] = None,
                      dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """Convert an extracted S-parameter CSV to a Touchstone ``.s2p`` file."""
    return comsol.export_touchstone(REGISTRY, csv_path, output_path, dry_run, debug)


@mcp.tool()
def run_geometry_param_sweep(
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

    Device-agnostic replacement for ``run_stub_length_sweep``. Works for any
    named COMSOL parameter: stub length (TWPA), slider length (resonator),
    coupler angle (transmon), gap width (waveguide), junction radius, etc.

    Supports both eigenfrequency and frequency-domain study types. Feed the
    output CSV into ``run_coupling_extraction`` (eigenfrequency + fields) or
    the fitting tools (frequency-domain).

    - **mph_path**: built .mph with the target parameter defined.
    - **param_name**: COMSOL parameter to sweep (e.g. ``"l_slider_single"``,
      ``"delta_angle_coupler"``, ``"stub_length"``).
    - **param_values**: values to sweep; units set by ``param_unit``.
    - **param_unit**: COMSOL unit string (default ``"um"``).
      Use ``"deg"`` for angles, ``"H"`` for inductance, etc.
    - **study_type**: ``"eigenfrequency"`` (default) or ``"frequency_domain"``.
    - **extract_fields**: extract per-mode We/Wm/path integrals
      (eigenfrequency only; see ``run_eigenfrequency_study``).
    - **freq_points_ghz**: evaluation frequencies for frequency-domain sweeps.
    - **resume**: skip values already present in the output CSV.
    - **dry_run**: True (default) = validate; False = launch background job.
    """
    return comsol.run_geometry_param_sweep(
        REGISTRY, mph_path=mph_path, param_name=param_name,
        param_values=param_values, param_unit=param_unit,
        study_type=study_type, n_modes=n_modes,
        freq_start_ghz=freq_start_ghz, freq_stop_ghz=freq_stop_ghz,
        extract_fields=extract_fields, path_selections=path_selections,
        node_groups=node_groups, freq_points_ghz=freq_points_ghz,
        port=port, resume=resume, comsol_host=comsol_host,
        output_dir=output_dir, comsol_cores=comsol_cores,
        dry_run=dry_run, debug=debug,
    )


@mcp.tool()
def run_decay_rate_sweep(
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
    """Sweep a lumped-element parameter and extract decay rate κ and T₁.

    Device-agnostic: works for qubit Purcell decay (sweep L_JJ), drive-port
    coupling (sweep coupling inductance), resonator external Q (sweep coupling
    gap), or any admittance-dominated decay channel.

    For each sweep value: set the parameter, rebuild geometry+mesh, run a
    frequency-domain study, extract voltages at two named COMSOL selections,
    compute ``κ = |V_port/V_junction|² / (Z0 · C)`` and ``T1 = 1/κ``.

    - **mph_path**: built .mph with the sweep parameter and both selections.
    - **sweep_param**: COMSOL parameter name to sweep.
    - **sweep_values**: values to sweep; units given by ``sweep_unit``.
    - **sweep_unit**: COMSOL unit string (e.g. ``"H"``, ``"um"``, ``"1"``).
    - **junction_selection**: COMSOL selection for the lumped element node.
    - **port_selection**: COMSOL selection for the output port node.
    - **shunt_capacitance_F**: shunt capacitance [F] for κ calculation.
    - **freq_ghz**: fixed drive frequency [GHz] (None = model default).
    - **Z0_Ohm**: port impedance (default 50 Ω).
    - **resume**: skip values already in the output CSV.
    - **dry_run**: True (default) = validate; False = launch background job.
    """
    return comsol.run_decay_rate_sweep(
        REGISTRY, mph_path=mph_path, sweep_param=sweep_param,
        sweep_values=sweep_values, sweep_unit=sweep_unit,
        junction_selection=junction_selection, port_selection=port_selection,
        shunt_capacitance_F=shunt_capacitance_F, freq_ghz=freq_ghz,
        Z0_Ohm=Z0_Ohm, resume=resume, comsol_host=comsol_host,
        output_dir=output_dir, comsol_cores=comsol_cores,
        dry_run=dry_run, debug=debug,
    )


@mcp.tool()
def run_coupling_extraction(
    eigenfreq_csv: str,
    mode1_path_col: str,
    mode2_path_col: str,
    lumped_inductance_H: float,
    mode1_label: str = "mode1",
    mode2_label: str = "mode2",
) -> Dict[str, Any]:
    """Extract coupling g between any two modes from eigenfrequency field data.

    Pure-Python post-processing — no COMSOL connection needed. Reads the CSV
    produced by ``run_eigenfrequency_study`` with ``extract_fields=True`` and
    applies Jaynes-Cummings energy-partition analysis.

    Device-agnostic: works for qubit-resonator, resonator-filter, or any
    two-mode coupled system where one mode contains a lumped inductive element.

    - **eigenfreq_csv**: CSV from ``run_eigenfrequency_study`` (must have
      ``We_J``, ``Wm_J``, and the two path-integral columns).
    - **mode1_path_col**: CSV column for |E| integral along mode 1's path
      (typically the "resonator-like" mode).
    - **mode2_path_col**: CSV column for |E| integral along mode 2's path
      (typically the "qubit-like" mode).
    - **lumped_inductance_H**: inductance of the lumped element (JJ, coupling
      inductor, etc.) in Henry.
    - **mode1_label** / **mode2_label**: human-readable labels in the output.

    Returns ``{g_Hz, g_MHz, chi_Hz, chi_MHz, anharmonicity_Hz, f_mode1_Hz,
    f_mode2_Hz, mode_labels, participation_ratio, error}``.
    """
    return comsol.run_coupling_extraction(
        eigenfreq_csv=eigenfreq_csv,
        mode1_path_col=mode1_path_col,
        mode2_path_col=mode2_path_col,
        lumped_inductance_H=lumped_inductance_H,
        mode1_label=mode1_label,
        mode2_label=mode2_label,
    )


@mcp.tool()
def run_parameter_inversion(
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

    One-shot replacement for the manual sweep → fit → invert loop.  Sweeps
    ``param_name`` over ``n_sweep_points`` evenly-spaced values in
    ``param_range``, runs an eigenfrequency study at each point, then inverts
    a polynomial fit to find the dimension that delivers ``target_value`` GHz.

    Example usage::

        run_parameter_inversion(
            mph_path   = "model_built.mph",
            param_name = "l_slider_single",
            param_range  = [200, 400],     # µm
            target_value = 6.5,            # GHz
            n_sweep_points = 9,
            post_physics = None,           # bare resonator
        )
        # → {recommended_value: 312.4, expected_eigenfreq_ghz: 6.499, ...}

    For a transmon qubit, add ``post_physics="transmon"`` and
    ``lumped_inductance_H`` to convert the bare eigenfrequency to ``fge``
    before inverting::

        run_parameter_inversion(
            mph_path   = "qubit_built.mph",
            param_name = "d_q",
            param_range  = [150, 350],     # µm (pad diameter)
            target_value = 5.8,            # GHz target fge
            post_physics = "transmon",
            lumped_inductance_H = 11.2e-9, # H
        )
        # → {recommended_value: 248.3, expected_fge_ghz: 5.802, ...}

    - **param_range**: ``[min, max]`` in ``param_unit``; endpoints are included.
    - **mode_index**: 1-based; mode 1 = lowest eigenvalue in search window.
    - **poly_degree**: polynomial degree for the fit (3 covers most monotonic
      curves; increase to 4–5 for more complex trends).
    - **dry_run**: True (default) = show sweep plan + inversion note.  False =
      launch background job (poll ``get_job_result`` for the recommendation).

    Result keys (real-run)
    ----------------------
    ``recommended_value``   : optimal geometry dimension  [param_unit]
    ``expected_eigenfreq_ghz`` or ``expected_fge_ghz``: predicted observable
    ``target_ghz``          : the target you passed in
    ``residual_ghz``        : |predicted − target|
    ``calibration_csv``     : path to the sweep CSV (keep for records)
    ``sweep_data``          : raw (param_value, observable) pairs
    ``note``                : human-readable recommendation string
    """
    return comsol.run_parameter_inversion(
        REGISTRY,
        mph_path=mph_path,
        param_name=param_name,
        param_range=param_range,
        target_value=target_value,
        n_sweep_points=n_sweep_points,
        param_unit=param_unit,
        mode_index=mode_index,
        post_physics=post_physics,
        lumped_inductance_H=lumped_inductance_H,
        poly_degree=poly_degree,
        freq_start_ghz=freq_start_ghz,
        freq_stop_ghz=freq_stop_ghz,
        n_modes=n_modes,
        comsol_host=comsol_host,
        output_dir=output_dir,
        comsol_cores=comsol_cores,
        dry_run=dry_run,
        debug=debug,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Superconducting circuit physics
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def compute_circuit_params(**kwargs: Any) -> Dict[str, Any]:
    """Compute superconducting circuit parameters from any input combination.

    Device-agnostic: works for transmon, fluxonium, charge qubit, or any
    lumped-element superconducting circuit.

    Pass any combination of the following keyword arguments and receive all
    derivable quantities in return:

    | Input key           | Meaning                           | Unit |
    |---------------------|-----------------------------------|------|
    | ``L_H``             | Josephson/coupling inductance     | H    |
    | ``f0_Hz``           | Bare oscillator frequency         | Hz   |
    | ``EJ_Hz``           | Josephson energy (in freq units)  | Hz   |
    | ``EC_Hz``           | Charging energy (in freq units)   | Hz   |
    | ``C_F``             | Capacitance                       | F    |
    | ``Ic_A``            | Critical current                  | A    |
    | ``g_Hz``            | Mode–mode coupling rate           | Hz   |
    | ``fq_Hz``           | Qubit frequency                   | Hz   |
    | ``fr_Hz``           | Resonator frequency               | Hz   |
    | ``anh_Hz``          | Qubit anharmonicity               | Hz   |
    | ``V_junction``      | Complex junction voltage phasor   | V    |
    | ``V_port``          | Complex port voltage phasor       | V    |
    | ``Z0_Ohm``          | Port impedance (default 50)       | Ω    |

    Returns all derivable quantities as a flat dict with consistent ``_Hz``,
    ``_fF``, ``_nH`` suffixes. Unknown kwargs are forwarded unchanged.
    """
    return circuit_physics.compute_circuit_params(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Design parameter management + session planning
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def design_params_read(yaml_path: str, key_path: str) -> Dict[str, Any]:
    """Read a value from a design_params.yaml by dot-separated key path.

    - **yaml_path**: absolute path to the YAML file.
    - **key_path**: dot-separated key chain, e.g.
      ``"design_Q0.readout_resonator.l_slider_single"``.

    Returns ``{ok, value, key_path, yaml_path, error}``.
    Creates the file with an empty dict if it does not exist.
    """
    try:
        value = design_params.read_param(yaml_path, key_path)
        return {"ok": True, "value": value, "key_path": key_path,
                "yaml_path": yaml_path, "error": None}
    except Exception as exc:
        return {"ok": False, "value": None, "key_path": key_path,
                "yaml_path": yaml_path, "error": str(exc)}


@mcp.tool()
def design_params_write(yaml_path: str, key_path: str, value: Any) -> Dict[str, Any]:
    """Atomically write a value to a design_params.yaml by dot-separated key path.

    Creates parent keys if missing. Uses ``os.replace`` for atomic writes so
    the file is never partially written (safe for concurrent MCP calls).

    - **yaml_path**: absolute path to the YAML file (created if absent).
    - **key_path**: dot-separated key chain, e.g.
      ``"design_Q0.qubit.d_q"``.
    - **value**: any YAML-serialisable value (float, int, str, list, dict).

    Returns ``{ok, key_path, value, yaml_path, error}``.
    """
    try:
        design_params.write_param(yaml_path, key_path, value)
        return {"ok": True, "key_path": key_path, "value": value,
                "yaml_path": yaml_path, "error": None}
    except Exception as exc:
        return {"ok": False, "key_path": key_path, "value": value,
                "yaml_path": yaml_path, "error": str(exc)}


@mcp.tool()
def get_pipeline_session_plan(
    yaml_path: str,
    stage_map: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Determine which pipeline stages are complete and what comes next.

    Device-agnostic session planner. Works for any pipeline that tracks progress
    in a ``design_params.yaml`` — AlNtransmon (D0–D6), resonator chip, TWPA,
    or any multi-stage sweep workflow.

    Call this at the **start of every pipeline session** before launching any
    COMSOL job. Enter plan mode and get explicit approval before proceeding.

    - **yaml_path**: path to the design_params.yaml for this device.
    - **stage_map**: maps stage name → list of dot-separated key paths that
      must be populated (non-null) for the stage to count as complete.

      AlNtransmon example::

          {
            "D0": ["design_Q0.qubit.d_q"],
            "D1": ["design_Q0.qr_coupler.delta_angle_coupler"],
            "D2": ["design_Q0.readout_resonator.l_slider_single"],
            "D3": ["design_Q0.readout_resonator.l_slider_single",
                   "design_Q0.coupling_params.g_qr_Hz"],
            "D4": ["design_Q0.qubit.LJJ_H"],
            "D5": ["design_Q0.decay.kappa_Hz", "design_Q0.decay.T1_s"],
            "D6": ["design_Q0.final.assembled_gds"]
          }

      Resonator chip example::

          {
            "freq_tune": ["res_A.l_slider_um"],
            "Q_tune":    ["res_A.coupling_gap_um"]
          }

    Returns ``{completed_stages, next_stage, missing_params, all_values,
    session_scope, error}``.
    """
    try:
        return design_params.get_session_plan(yaml_path, stage_map)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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


# ─────────────────────────────────────────────────────────────────────────────
# qleap chip simulations (tile pipelines in <repo>/simulations/)
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def qleap_notch_status(unit: Optional[str] = None,
                       row: Optional[str] = None) -> Dict[str, Any]:
    """Progress + results of the qleap NDS001 notch-decay pipeline per tile.

    Read-only and instant. Shows, per tile (U0_R0 .. U2_R1 or just
    ``unit``/``row``): whether the stitched copy and the prepared+gated model
    exist, which coarse/fine sweep CSVs are done, the metal-verification
    verdict, and — when extraction has run — the per-qubit notch summary
    (f_q, f_notch, kappa(f_q), T1, depth) plus any recorded problems.
    """
    return qleap.qleap_notch_status(unit=unit, row=row)


@mcp.tool()
def qleap_run_notch_pipeline(
    unit: str,
    row: str,
    letters: str = "ABCD",
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run the full NDS001 notch pipeline for one tile (background job).

    Resume-safe (existing artifacts are skipped): stitched-model copy ->
    read-only inspection (coax survey gate) -> S-param prep (selection
    verification gate, JJ ports as Cable/50ohm) -> metal raster gate ->
    presence renders -> per-letter coarse sweeps (3.5-7.5 GHz) -> fine
    windows -> fine sweeps -> notch/kappa/T1 extraction + figure.
    ~5-6 h per tile. ``letters`` restricts the sweeps (e.g. "A").
    """
    return qleap.qleap_run_notch_pipeline(
        REGISTRY, unit=unit, row=row, letters=letters,
        dry_run=dry_run, debug=debug)


@mcp.tool()
def qleap_run_notch_sweep(
    unit: str,
    row: str,
    letter: str,
    pass_name: str = "coarse",
    flist: Optional[str] = None,
    others: str = "inductor",
    reciprocity: bool = False,
    cores: int = 8,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """One JJ-port S-parameter sweep on a prepared tile (background job).

    Excites qubit ``letter``'s JJ as a lumped port (its inductor removed,
    the other three keep 12 nH unless ``others='open'``) and records
    S_ii/S_5i/Vport per frequency. ``flist`` overrides the grid with a
    COMSOL range/list expression, e.g. ``"range(6.3[GHz],0.005[GHz],6.5[GHz])"``
    — that makes this the right tool for targeted notch-region scans.
    ~45 s per frequency point.
    """
    return qleap.qleap_run_notch_sweep(
        REGISTRY, unit=unit, row=row, letter=letter, pass_name=pass_name,
        flist=flist, others=others, reciprocity=reciprocity, cores=cores,
        dry_run=dry_run, debug=debug)


@mcp.tool()
def qleap_extract_notch(unit: str, row: str,
                        stage: str = "final") -> Dict[str, Any]:
    """Post-process NDS001 sweep CSVs (foreground, seconds).

    ``stage='window'``: locate coarse notches, write per-letter fine flists.
    ``stage='final'``: merged kappa(omega) -> notch position/depth, kappa(f_q),
    T1, readout peak; returns the notch summary and the figure path.
    """
    return qleap.qleap_extract_notch(unit=unit, row=row, stage=stage)


@mcp.tool()
def qleap_run_eigen_gqr(
    unit: str,
    row: str,
    run: int,
    neigs: Optional[int] = None,
    shift: Optional[str] = None,
    cores: int = 8,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """RCS001 eigenfrequency solve on a prepared gQR tile model (background).

    ``run=1``: JJ inductors active -> qubit frequencies (then
    ``qleap_extract_gqr(stage='qubit-freqs')``).
    ``run=2``: JJ current ports active -> g_QR per readout mode (then
    ``qleap_extract_gqr(stage='final')``).
    """
    return qleap.qleap_run_eigen_gqr(
        REGISTRY, unit=unit, row=row, run=run, neigs=neigs, shift=shift,
        cores=cores, dry_run=dry_run, debug=debug)


@mcp.tool()
def qleap_extract_gqr(unit: str, row: str,
                      stage: str = "final") -> Dict[str, Any]:
    """Post-process RCS001 eigen CSVs (foreground, seconds): qubit
    frequencies after run 1, g_QR summary after run 2."""
    return qleap.qleap_extract_gqr(unit=unit, row=row, stage=stage)


def main() -> None:
    """Console-script / ``python -m`` entry point: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

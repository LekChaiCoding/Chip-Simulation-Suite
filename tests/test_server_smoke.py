"""Smoke tests: the package imports, config resolves, tools register."""

from __future__ import annotations


def test_package_imports():
    import comsol_suite
    assert comsol_suite.__version__


def test_config_resolves_paths_under_the_repo():
    """Every configured path resolves under the repo root.

    Resolution is what this suite owns; EXISTENCE of the legacy JTWPA trees
    (``resources/COMSOL Simulation/``, ``resources/JosephsonCircuit/``) is a
    property of the checkout, and they are not vendored here. Asserting they
    exist made this fail permanently on a machine where nothing was wrong —
    see tests/conftest.py. ``describe_config`` reports which are absent.
    """
    from comsol_suite.config import load_config
    cfg = load_config()
    root = cfg.repo_root.resolve()
    for key in ("cad_generator", "abcd_fit", "comsol_build", "comsol_eigenfreq"):
        assert cfg.script(key).resolve().is_relative_to(root), key
    for key in ("reference_gds", "bridge003_sweep"):
        assert cfg.datum(key).resolve().is_relative_to(root), key


def test_the_suites_own_scripts_are_present():
    """The scripts this repo DOES vendor must be there — a real regression if not."""
    from comsol_suite.config import load_config
    cfg = load_config()
    for key in ("comsol_eigenfreq", "comsol_eigenfreq_fields",
                "comsol_geometry_sweep", "comsol_decay_sweep"):
        assert cfg.script(key).is_file(), f"{key} -> {cfg.script(key)}"


def test_tools_register_on_server():
    # Importing the server constructs the FastMCP app and registers all tools.
    import asyncio
    from comsol_suite import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        # CAD stage
        "generate_cad", "verify_cad", "run_custom_cad", "assemble_geometry",
        # COMSOL stage
        "comsol_health_check", "build_comsol_model", "run_custom_comsol_build",
        "validate_geometry",
        "run_stub_length_sweep", "export_touchstone",
        "run_eigenfrequency_study", "run_geometry_param_sweep",
        "run_decay_rate_sweep", "run_coupling_extraction",
        "run_parameter_inversion",
        # SC circuit physics
        "compute_circuit_params",
        # Design parameter management
        "design_params_read", "design_params_write", "get_pipeline_session_plan",
        # Fitting stage
        "run_abcd_fit", "run_abcd_fit_parallel", "run_generic_fit",
        "fit_stub_sweep", "analyze_dispersion",
        # Job management
        "get_job_status", "get_job_result", "list_jobs", "describe_config",
        # qleap NDS001 / RCS001 tile pipelines
        "qleap_notch_status", "qleap_run_notch_pipeline", "qleap_run_notch_sweep",
        "qleap_extract_notch", "qleap_run_nt2_probe",
        "qleap_run_eigen_gqr", "qleap_extract_gqr",
        # qleap NT002 filter-retuning campaign
        "qleap_nt2_linear_retune", "qleap_nt2_purcell_check",
        "qleap_nt2_ratio_retune", "qleap_nt2_ratio_gap_check",
        "qleap_nt2_ratio_geometry_gate", "qleap_nt2_run_ratio_trade_probe",
        "qleap_nt2_build_merged_model", "qleap_nt2_verify_merged_notches",
        "qleap_nt2_publish_optimized",
        # qleap CCT001 cable-coupling tuning
        "qleap_cct001_tune_width", "qleap_cct001_rollout_letter",
        # qleap F1 capacitance campaign (factory)
        "qleap_cs002_optimize", "qleap_cs002_direct_probe",
        "qleap_cs002_coupling_correct", "qleap_cs002_finalize",
        # qleap ChipConstruction (Stitching005 mph -> GDS -> block/chip)
        "qleap_chipconstruction_build_schematics",
        "qleap_chipconstruction_mph_preflight",
        "qleap_chipconstruction_export_tile",
        "qleap_chipconstruction_insert_jj",
        "qleap_chipconstruction_render_cell_thumbs",
        "qleap_chipconstruction_gds_validity_check",
        "qleap_chipconstruction_gds_diff",
        "qleap_chipconstruction_assemble_block",
        "qleap_chipconstruction_build_block",
        "qleap_chipconstruction_tile_chip",
        "qleap_chipconstruction_build_chip",
        "qleap_chipconstruction_verify_block",
        "qleap_chipconstruction_build_hexlattice",
        "qleap_chipconstruction_status",
        # F0-F3 factory control plane (added 2026-07-27; this set was not
        # updated with them, so the suite reported three real tools as
        # "unexpected" on every run)
        "qleap_factory_status", "qleap_factory_line", "qleap_factory_record",
        # The one that RUNS F2 rather than describing it (added 2026-08-04).
        # Until it existed the agent could read the line's plan and its record
        # chain but had no way to execute a station.
        "qleap_f2_gauntlet",
        # The one that LAUNCHES the new design-agnostic line (added 2026-08-08).
        # Its own registration re-committed the omission the note above records,
        # in the file that records it: the tool was added to server.py and not
        # to this set, so the suite reported it as "unexpected" on every run.
        "qleap_line_execute",
        # ...and the one that READS the new line back (added 2026-08-12): the
        # launch surface above had no read surface, so an agent that started a
        # run could only ask qleap_factory_status — the OLD tree — and be told
        # nothing happened.
        "qleap_line_status",
        # ...and the one that READS A GATE (added 2026-08-19). The five coupling
        # gates are library functions the runner never dispatches, so a gate read
        # is an operator act: the agent could spend solver time on seventeen
        # blocks and then not read a single gate, which is why "the gates have no
        # callable surface" sat first on the Qwen-readiness blocker list. Added
        # here in the same commit as the tool, which is what the note above this
        # set exists to make happen.
        "qleap_coupling_gate",
    }
    missing = expected - names
    assert not missing, f"tools not registered: {missing}"
    extra = names - expected
    assert not extra, f"unexpected tools not in expected set: {sorted(extra)}"

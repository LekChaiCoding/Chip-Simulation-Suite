"""Smoke tests: the package imports, config resolves, tools register."""

from __future__ import annotations


def test_package_imports():
    import comsol_suite
    assert comsol_suite.__version__


def test_config_resolves_paths():
    from comsol_suite.config import load_config
    cfg = load_config()
    # The wrapped source scripts and data should resolve to real files when the
    # suite sits inside the Chip Simulation tree.
    assert cfg.script("cad_generator").is_file()
    assert cfg.script("abcd_fit").is_file()
    assert cfg.datum("reference_gds").is_file()
    assert cfg.datum("bridge003_sweep").is_file()


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
        "run_stub_length_sweep", "export_touchstone",
        "run_eigenfrequency_study", "run_geometry_param_sweep",
        "run_decay_rate_sweep", "run_coupling_extraction",
        # SC circuit physics
        "compute_circuit_params",
        # Design parameter management
        "design_params_read", "design_params_write", "get_pipeline_session_plan",
        # Fitting stage
        "run_abcd_fit", "run_abcd_fit_parallel", "run_generic_fit",
        "fit_stub_sweep", "analyze_dispersion",
        # Job management
        "get_job_status", "get_job_result", "list_jobs", "describe_config",
    }
    missing = expected - names
    assert not missing, f"tools not registered: {missing}"
    assert len(names) == 26, f"expected 26 tools, got {len(names)}: {sorted(names)}"

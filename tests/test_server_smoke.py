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
        "generate_cad", "verify_cad",
        "comsol_health_check", "build_comsol_model", "validate_geometry",
        "run_stub_length_sweep", "export_touchstone",
        "run_abcd_fit", "fit_stub_sweep", "analyze_dispersion",
        "get_job_status", "get_job_result", "list_jobs", "describe_config",
    }
    missing = expected - names
    assert not missing, f"tools not registered: {missing}"

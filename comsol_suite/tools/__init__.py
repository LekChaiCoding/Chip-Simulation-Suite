"""Tool implementations for the COMSOL Simulation Suite.

Each submodule exposes plain Python functions that return JSON-serialisable
dicts. They are deliberately *not* decorated with MCP machinery — ``server.py``
registers them as MCP tools. This separation keeps the functions directly
unit-testable (see ``tests/``) without spinning up an MCP session.

  * :mod:`comsol_suite.tools.cad`            — generate_cad, verify_cad, assemble_geometry
  * :mod:`comsol_suite.tools.comsol`         — COMSOL build/validate/sweep/export + new generic sweeps
  * :mod:`comsol_suite.tools.fitting`        — device-agnostic: run any fit script
  * :mod:`comsol_suite.tools.jtwpa`          — ONE device family's vendored surface
                                               (JTWPA ABCD + Julia circuit fits)
  * :mod:`comsol_suite.tools.circuit_physics`— SC circuit math (transmon, fluxonium, generic)
  * :mod:`comsol_suite.tools.design_params`  — YAML design parameter manager + session planner
"""

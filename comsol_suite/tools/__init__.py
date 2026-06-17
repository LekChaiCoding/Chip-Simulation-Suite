"""Tool implementations for the COMSOL Simulation Suite.

Each submodule exposes plain Python functions that return JSON-serialisable
dicts. They are deliberately *not* decorated with MCP machinery — ``server.py``
registers them as MCP tools. This separation keeps the functions directly
unit-testable (see ``tests/``) without spinning up an MCP session.

  * :mod:`comsol_suite.tools.cad`     — generate_cad, verify_cad
  * :mod:`comsol_suite.tools.comsol`  — COMSOL build/validate/sweep/export
  * :mod:`comsol_suite.tools.fitting` — ABCD (Python) + Julia circuit fits
"""

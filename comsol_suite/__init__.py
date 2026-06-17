"""COMSOL Simulation Suite — an MCP server wrapping the chip-simulation pipeline.

The pipeline has three stages:

    CAD  ->  COMSOL  ->  fitting

  * CAD     : generate a GDS layout with gdstk and verify it against the
              vertex-validated reference geometry.
  * COMSOL  : build the EM model from the GDS, validate the geometry, solve a
              frequency / stub-length sweep, export Touchstone S-parameters.
  * fitting : extract the lumped circuit (Cg, dispersion, delta-k) from the
              S-parameters via the Python ABCD fit or the Julia fitter.

This package does NOT re-implement any of that physics. It is a thin, well
-documented *orchestrator*: it launches the project's existing, proven scripts
as subprocesses and exposes them as MCP tools so they can be driven from
Claude Code. See ``docs/ARCHITECTURE.md`` for the design rationale.
"""

__version__ = "0.1.0"

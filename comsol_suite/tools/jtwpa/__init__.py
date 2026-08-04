"""Tools tied to ONE device family: the vendored JTWPA reference pipeline.

Anything in here wraps the vendored `COMSOL Simulation/` or `JosephsonCircuit/`
trees and assumes that device's geometry or data layout. Anything that merely
*defaults* to a vendored script while accepting a caller-supplied one is NOT in
here — `generate_cad`, `verify_cad` and `run_generic_fit` are device-agnostic
tools with a legacy default, which is a different thing.

The distinction exists so "does this tool generalise?" is answerable from the
import path instead of by reading the implementation.
"""

from . import comsol, fitting  # noqa: F401

__all__ = ["comsol", "fitting"]

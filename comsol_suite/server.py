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


# ─────────────────────────────────────────────────────────────────────────────
# COMSOL stage  (wrapped; default dry_run=True — needs a live COMSOL connection)
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def comsol_health_check(comsol_host: Optional[str] = None,
                        comsol_port: Optional[int] = None) -> Dict[str, Any]:
    """Check COMSOL reachability (mph import + TCP probe) without solving."""
    return comsol.comsol_health_check(comsol_host, comsol_port)


@mcp.tool()
def build_comsol_model(gds_path: str, junction_inductance_ph: float = 280.0,
                       comsol_host: Optional[str] = None,
                       output_dir: Optional[str] = None,
                       dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """Build the COMSOL EM model from a GDS (build -> validate -> coarse solve).

    Defaults to dry-run (validate args + health-check only). Set
    ``dry_run=False`` once on the COMSOL network to launch as a background job.
    """
    return comsol.build_comsol_model(
        REGISTRY, gds_path, junction_inductance_ph, comsol_host,
        output_dir, dry_run, debug)


@mcp.tool()
def validate_geometry(mph_path: str, reference_vertices_csv: Optional[str] = None,
                      comsol_host: Optional[str] = None,
                      dry_run: bool = True) -> Dict[str, Any]:
    """Validate a built model's geometry (face counts + full vertex multiset)."""
    return comsol.validate_geometry(mph_path, reference_vertices_csv,
                                    comsol_host, dry_run)


@mcp.tool()
def run_stub_length_sweep(mph_path: str, stub_lengths_um: List[float],
                          freq_ghz: List[float], comsol_host: Optional[str] = None,
                          output_dir: Optional[str] = None,
                          dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """Solve a parametric stub-length sweep, extracting complex S-parameters.

    Produces the ``.dat`` the fitting tools consume. Defaults to dry-run.
    """
    return comsol.run_stub_length_sweep(
        REGISTRY, mph_path, stub_lengths_um, freq_ghz, comsol_host,
        output_dir, dry_run, debug)


@mcp.tool()
def export_touchstone(csv_path: str, output_path: Optional[str] = None,
                      dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """Convert an extracted S-parameter CSV to a Touchstone ``.s2p`` file."""
    return comsol.export_touchstone(REGISTRY, csv_path, output_path, dry_run, debug)


# ─────────────────────────────────────────────────────────────────────────────
# Fitting stage
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def run_abcd_fit(data_path: Optional[str] = None, output_dir: Optional[str] = None,
                 debug: bool = False) -> Dict[str, Any]:
    """Fit the lumped circuit from a stub-length sweep via the Python ABCD fitter.

    Tests 3 topologies x 5 objectives per stub; writes a results CSV with fitted
    Cg and implied Z0. Returns a ``job_id`` — poll with ``get_job_status`` /
    ``get_job_result``. Defaults to the bundled bridge/003 sweep data.
    """
    return fitting.run_abcd_fit(REGISTRY, data_path=data_path,
                                output_dir=output_dir, debug=debug)


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

# COMSOL Simulation Suite

An **MCP (Model Context Protocol) server** that wraps the lab's chip-simulation
pipeline so it can be driven conversationally from Claude Code:

```
        ┌─────────┐      ┌──────────┐      ┌──────────┐
  CAD → │ generate│ →    │  COMSOL  │  →   │ fitting  │ → circuit params
        │ + verify│      │  solve   │      │ (ABCD /  │   (Cg, Z0, Δk)
        └─────────┘      └──────────┘      │  Julia)  │
         gdstk GDS        mph / EM         └──────────┘
```

Each lab mate installs this package locally and adds it to their Claude Code
config; the existing, validated pipeline scripts are then exposed as tools
(`generate_cad`, `run_stub_length_sweep`, `run_abcd_fit`, …) that an agent can
call directly.

> **Design in one line:** this is a *thin orchestrator*. It does not
> re-implement any physics — it launches the project's proven scripts as
> subprocesses and reports their results. See
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## What works today

| Stage   | Status | Notes |
|---------|--------|-------|
| **CAD** | ✅ runnable & verified | `generate_cad` reproduces the exact device GDS; `verify_cad` passes the vertex-validated geometry checks. |
| **COMSOL** | 🔌 wrapped, gated | Tools are fully coded but default to `dry_run=True`. Real solves need a live COMSOL connection (`mph` + licence/server). |
| **Fitting** | ✅ runnable & verified | `run_abcd_fit` (Python) extracts Cg / Z0 from a stub sweep. Julia tools available if a Julia env is installed. |

The COMSOL link is intentionally shipped gated: the pipeline is *wired*
end-to-end, but the one segment that needs hardware (a COMSOL server) is not run
automatically.

---

## Quick start

```bash
# 1. Install (from the repo root, inside the Chip Simulation folder)
pip install -e .

# 2. Sanity-check the wiring
python -c "from comsol_suite.config import load_config; import json; \
           print(json.dumps(load_config().as_dict(), indent=2))"

# 3. Run the tests (CAD + fitting run for real against the project data)
pip install -e ".[dev]"
pytest -q
```

Then wire it into Claude Code — see [`docs/INSTALL.md`](docs/INSTALL.md).

## Documentation

- [`docs/INSTALL.md`](docs/INSTALL.md) — per-machine setup & Claude Code config
- [`docs/TOOLS.md`](docs/TOOLS.md) — every MCP tool, its inputs/outputs, examples
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design rationale & job model
- [`docs/PIPELINE.md`](docs/PIPELINE.md) — the CAD→COMSOL→fitting data flow & formats

## Requirements

- Python ≥ 3.10 with `mcp`, `gdstk`, `numpy` (installed automatically).
- **Optional** for the COMSOL stage: `mph` + a COMSOL install/licence (`pip install -e ".[comsol]"`).
- **Optional** for the Julia fitters: a Julia install with the project's
  `JosephsonCircuits.jl` environment.

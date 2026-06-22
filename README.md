# Chip Simulation Suite

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that
exposes a guided superconducting chip simulation pipeline — **CAD →
materials → COMSOL → analysis/fitting** — as tools that any AI coding assistant
can drive.

You describe what you want in plain language. The AI calls the right tools in
the right order, monitors background jobs, and surfaces the `.mph` files and
fitted parameters when done. You never touch a script directly.

```
You: "Generate the CAD for my 21-junction JTWPA, run the stub-length sweep
      at 300–400 µm in parallel, and fit the ABCD matrix."

AI:  generate_cad()  →  verify_cad()  →  confirm materials with the user
     →  build_comsol_model(dry_run=False)  →  run grid-search tuning
     →  run_stub_length_sweep(...)  →  run_abcd_fit_parallel()
     →  "Fit complete. Z0 ≈ 48 Ω across all stubs.
         Results at runs/.../abcd_fit_results_merged.csv"
```

**Design in one line:** this is a *thin orchestrator*. It launches the project's
validated physics/geometry scripts as subprocesses — it never reimplements them.
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## What's inside

| Stage | Tools | What it does |
|-------|-------|-------------|
| **CAD** | `generate_cad`, `verify_cad`, `run_custom_cad` | Generates GDS layouts; verifies geometry against the validated reference |
| **Materials** | guided conversation in `stack/prompts/material_selection.md` | Confirms substrate, metal, loss tangent, and COMSOL material parameters after CAD verification |
| **COMSOL** | `build_comsol_model`, `run_custom_comsol_build`, `run_stub_length_sweep`, `run_eigenfrequency_study`, `export_touchstone`, `comsol_health_check` | Builds EM models with adjustable geometry and material parameters; saves inspectable `.mph` files. `run_eigenfrequency_study` finds resonances + Q-factors in ~5 min (run this FIRST for any new device) |
| **Automated tuning** | `stack/prompts/automated_grid_search.md`, `get_job_status`, `get_job_result` | Lets the AI run parameter changes, simulations, scoring, and refinement until design targets pass or the approved budget is exhausted |
| **Fitting** | `run_abcd_fit`, `run_abcd_fit_parallel`, `run_generic_fit`, `fit_stub_sweep`, `analyze_dispersion` | Extracts lumped circuit parameters (Cg, Z0, Bloch dispersion Δk); parallel mode is ~5× faster |
| **Jobs** | `get_job_status`, `get_job_result`, `list_jobs` | Monitors long-running background solves; survives server restarts |
| **Config** | `describe_config`, `comsol_health_check` | Resolves all paths; probes COMSOL connectivity without solving |

18 tools total — full reference in [`docs/TOOLS.md`](docs/TOOLS.md).

---

## Requirements

| Dependency | Needed for | How to get it |
|---|---|---|
| Python ≥ 3.10 | Everything | same interpreter used for pipeline scripts |
| `mcp`, `gdstk`, `numpy` | Core (auto-installed) | `pip install -e .` |
| `mph` | Real COMSOL solves | `pip install -e ".[comsol]"` |
| COMSOL ≥ 6.0 + licence | Real COMSOL solves | local install or network server |
| Julia + JosephsonCircuits.jl | Julia fitting tools | optional; Python fitting works without it |
| Claude Code / Codex / Cursor / … | AI-driven workflow | any MCP-compatible client |

The MCP server starts and all 18 tools register **without COMSOL or Julia
installed**. COMSOL tools stay in `dry_run=True` (plan-only) mode until
a live connection is available.

---

## Installation

### 1. Access the shared drive

The suite lives on the lab shared drive. Mount the drive and navigate to:

```
Z:\users\Alex\Chip Simulation\COMSOL Simulation Suite
```

> **Drive letter may vary by machine.** If your shared drive is not mapped to
> `Z:`, use whatever letter it is mounted at. The rest of the path stays the
> same. On Linux/macOS the mount point is typically
> `/mnt/smb/HSS/users/Alex/Chip Simulation/COMSOL Simulation Suite`.

The suite auto-discovers all pipeline scripts relative to its own location
inside `Chip Simulation` — no extra configuration needed on lab machines.

### 2. Install

```bash
pip install -e .              # core — CAD + fitting + server
pip install -e ".[comsol]"   # add real COMSOL connection support
pip install -e ".[dev]"      # add pytest for running the tests
```

### 3. Verify path resolution

```bash
python -c "from comsol_suite.config import load_config; import json; \
           print(json.dumps(load_config().as_dict(), indent=2))"
```

Every `scripts.*` and `data.*` entry should point at a real file.

### 4. Run the tests

```bash
pytest -q
```

Expected: **17 tests pass**. COMSOL tests run in dry-run mode (no connection
needed); CAD and fitting tests exercise the real pipeline.

---

## Deployment — connecting to an AI model

The server speaks **stdio JSON-RPC**, the standard MCP transport. Any
MCP-compatible client works; only the config file path differs.

### Claude Code

Add to `~/.claude/settings.json` (user-wide) or `.claude/settings.json`
(project-level):

```json
{
  "mcpServers": {
    "comsol-suite": {
      "command": "python",
      "args": ["-m", "comsol_suite"],
      "cwd": "Z:/users/Alex/Chip Simulation/COMSOL Simulation Suite",
      "env": {
        "COMSOL_HOST": "your-lab-comsol-server"
      }
    }
  }
}
```

> Replace `Z:` with your actual drive letter. Drop the `env` block entirely
> to use a local COMSOL install instead of a network server.

Restart Claude Code. Confirm the tools loaded:
> *"Describe the COMSOL suite config."*

### OpenAI Codex CLI

```json
// ~/.codex/config.json
{
  "mcpServers": {
    "comsol-suite": {
      "command": "python",
      "args": ["-m", "comsol_suite"],
      "cwd": "Z:/users/Alex/Chip Simulation/COMSOL Simulation Suite",
      "env": { "COMSOL_HOST": "your-lab-comsol-server" }
    }
  }
}
```

### Cursor / Windsurf / Zed

```json
// .cursor/mcp.json  (or your editor's equivalent)
{
  "mcpServers": {
    "comsol-suite": {
      "command": "python",
      "args": ["-m", "comsol_suite"],
      "cwd": "Z:/users/Alex/Chip Simulation/COMSOL Simulation Suite"
    }
  }
}
```

Check your editor's MCP documentation for the exact config filename. The
server command is the same for every client.

> **Note on paths:** Always use forward slashes in the `cwd` field, even on
> Windows. Replace `Z:` with your actual drive letter if the shared drive is
> mounted differently on your machine.

### Running the server directly

```bash
python -m comsol_suite     # stdio MCP server
comsol-suite               # same, via installed console script
```

---

## Per-machine configuration

Override any path or setting without editing the repo:

```bash
cp config/paths.example.toml config/paths.toml   # gitignored; stays local
```

Or set environment variables (these win over the file):

| Variable | Meaning | Default |
|---|---|---|
| `CHIP_SIM_ROOT` | Path to the `Chip Simulation` folder | parent of this repo |
| `COMSOL_HOST` | COMSOL server hostname | local |
| `COMSOL_PORT` | COMSOL server port | 2036 |
| `JULIA_BIN` | Julia executable | `julia` on PATH |
| `CHIP_SIM_PYTHON` | Python used to run wrapped scripts | `sys.executable` |

---

## End-to-end walkthrough

### Standard JTWPA workflow

The complete path from a CAD idea to fitted circuit parameters, driven through
an AI assistant.

**Step 1 — Generate and verify the chip layout**

> *"Generate the 21-junction JTWPA GDS and verify it against the reference geometry."*

```
generate_cad()
→ { "ok": true,
    "gds_path": "runs/cad-.../converter_group_recreation.gds",
    "preview_png": "runs/cad-.../converter_group_recreation.png" }

verify_cad("runs/cad-.../converter_group_recreation.gds")
→ { "passed": true, "n_failures": 0,
    "report": "ALL PASS — recreation matches the built reference geometry pins" }
```

**Step 2 — Confirm materials**

After the GDS passes verification, the AI asks the human to confirm substrate,
metal, first-pass loss tangent, and the exact COMSOL material parameters. This
happens after CAD, not during the initial geometry conversation.

```
material_params = {
    "sub_eps_r": "11.7",
    "sub_loss_tan": "0",
    "metal_model": "PEC"
}
```

**Step 3 — Check COMSOL connectivity**

> *"Is COMSOL reachable?"*

```
comsol_health_check()
→ { "ok": true, "mph_available": true,
    "host_reachable": true, "detail": "mph import OK; TCP connect to comsol-server:2036 OK" }
```

If `ok: false` you are off-network. All COMSOL tools still work in
`dry_run=True` — they return the exact plan (including which `.mph` files
would be saved) without running anything.

**Step 4 — Build the COMSOL EM model**

> *"Build the model with an 8-thread solve. Adjust the stub length to 350 µm and use silicon substrate. Show me the plan first."*

```
build_comsol_model(
    gds_path        = "runs/cad-.../converter_group_recreation.gds",
    geom_params     = {"add_stub_length": "350[um]"},
    material_params = {"sub_eps_r": "11.7", "sub_loss_tan": "1e-6"},
    comsol_cores    = 8,
    build_only      = False,
    dry_run         = True     ← default; shows plan, no solve
)
→ { "dry_run": true,
    "patches_applied": {
        "^ROOT\\s*=.*$": "ROOT = r\"...\"",
        "GEOM_PARAM_OVERRIDES (injected)": "{'add_stub_length': '350[um]'}",
        "MATERIAL_PARAM_OVERRIDES (injected)": "{'sub_eps_r': '11.7', ...}"
    },
    "mph_files_would_save": [
        "runs/comsol_build/model_built.mph",
        "runs/comsol_build/model_solved.mph"
    ],
    "ready": true }
```

Set `dry_run=False` on the COMSOL network to launch the real solve as a
background job. Use `build_only=True` to stop after saving `model_built.mph`
so you can inspect geometry before committing to a long solve.

**Step 5 — Inspect the `.mph` file**

Every completed COMSOL job returns `mph_paths` — open any of them directly in
the **COMSOL GUI** to verify your work:

| File | What to check |
|---|---|
| `model_built.mph` | Geometry, mesh quality, port placement, physics settings |
| `model_solved.mph` | Field distributions, S-parameter convergence, port excitation |
| `stub_<N>um.mph` | Per-stub solution from the sweep (one file per stub length) |

**Step 6 — Automated grid-search tuning**

For new devices, the AI should run automated tuning after materials are
confirmed. The human approves the parameter ranges, tolerances, and trial budget
once; the AI then iterates different models by grid search, reads each result
CSV, scores it against the design targets, and keeps refining until a candidate
passes or the budget is exhausted.

The loop follows the reference tuning scripts in
`Z:\users\ishida\backup\python_script`: set COMSOL parameters, regenerate
geometry/mesh, run the study, extract numerical results, score the trial, and
store the best candidate.

**Step 7 — Parametric stub-length sweep**

> *"Sweep 300–400 µm at 16 frequency points, extracting the full 2×2 S-matrix. Resume safely if it crashes."*

```
run_stub_length_sweep(
    mph_path        = "runs/comsol_build/model_solved.mph",
    stub_lengths_um = [300, 320, 340, 360, 380, 400],
    freq_ghz        = list(range(1, 17)),
    port            = "both",   # S11/S21/S12/S22
    comsol_cores    = 8,
    resume          = True,     # skip stubs already in the output CSV
    dry_run         = False
)
→ { "job_id": "comsol_sweep-a3f9...", "status": "running" }
```

**Step 8 — Fit the circuit model (parallel)**

> *"Fit the ABCD matrix for all stubs simultaneously."*

```
run_abcd_fit_parallel(
    data_path = "runs/comsol_sweep-.../stub_length_sweep.dat"
)
→ { "job_id": "abcd_fit_parallel-3a9f...", "n_stubs": 6, "status": "running" }

get_job_result("abcd_fit_parallel-3a9f...")
→ { "status": "completed",
    "result": {
        "stubs_ok":   {"300": true, "320": true, "340": true,
                       "360": true, "380": true, "400": true},
        "merged_csv": "runs/.../abcd_fit_results_merged.csv",
        "summary":    "Parallel fit: 6/6 stubs OK; 102 result rows merged"
    } }
```

Parallel fitting runs one subprocess per stub — **~5× faster** than
sequential on a multi-core machine. Results for the canonical
`topology=topoA, fit_method=fitA` objective:

| stub µm | Cg fF | Z0 Ω |
|--------:|------:|-----:|
| 300 | 109.9 | 51.2 |
| 340 | 124.7 | 48.1 |
| 400 | 147.5 | 44.2 |

**Step 9 — Bloch dispersion analysis (optional, needs Julia)**

```
analyze_dispersion()
→ produces delta_k.csv  (Δk = 2k_p − k_s − k_i vs pump frequency)
```

---

### Custom device workflow

The pipeline is **device-agnostic**. Swap in your own scripts at any stage.

**Custom CAD** — script defines `OUT_GDS` and optionally `OUT_PNG`:

```python
# my_transmon.py
OUT_GDS = "/default/output.gds"
# ... gdstk layout for transmon cross-pad + readout resonator ...
```

```
run_custom_cad(cad_script="/path/to/my_transmon.py", gds_filename="transmon.gds")
```

**Custom COMSOL build** — script defines three patchable variables:

```python
# transmon_build.py
OUT_DIR            = "/default/output"   # → redirected to runs/<job>/
PARAM_OVERRIDES    = {}                  # → your geom_params
MATERIAL_OVERRIDES = {}                  # → your material_params

for name, val in PARAM_OVERRIDES.items():
    m.param().set(name, val)
pymodel.save(os.path.join(OUT_DIR, "model_built.mph"))
```

```
run_custom_comsol_build(
    build_script    = "/path/to/transmon_build.py",
    geom_params     = {
        "pad_width":  "200[um]",    # transmon cross-pad width
        "pad_height": "300[um]",
        "res_length": "5000[um]",   # readout resonator length
        "sub_t":      "525[um]",    # silicon substrate thickness
        "air_height": "1[mm]",
    },
    material_params = {
        "sub_eps_r":    "11.7",     # silicon εr
        "sub_loss_tan": "1e-6",
        "metal_sigma":  "5.88e7",   # aluminum σ (S/m)
    },
    comsol_cores = 8,
    dry_run = True    # set False on COMSOL network
)
```

This pattern works for any study type — S-parameter sweeps, eigenfrequency
studies (qubit frequency extraction), capacitance matrix extraction, etc.

**Custom fitting** — script defines `DAT_PATH` and `OUT_BASE`:

```python
# my_cap_fit.py
DAT_PATH = "/default/data.dat"
OUT_BASE = "/default/output"
# ... extract Ec = e²/2Cσ, coupling g from capacitance matrix ...
```

```
run_generic_fit(
    fit_script    = "/path/to/my_cap_fit.py",
    data_path     = "runs/comsol-.../capacitance.dat",
    extra_patches = {r"^N_QUBITS\s*=.*$": "N_QUBITS = 5"}
)
```

---

## Key design decisions

**Scripts are never modified.** The upstream physics/geometry scripts are
treated as a read-only, validated source of truth. The suite makes a
*path-redirected copy* — only I/O path assignment lines (e.g. `OUT_DIR = "..."`)
are rewritten in the copy. If a script is refactored and the pattern stops
matching, the tool fails loudly rather than producing a silently wrong result.

**COMSOL runs in a child process.** The `mph`/JPype bridge is crash-prone.
Running COMSOL as a subprocess means a JPype SIGSEGV kills only that child,
never the MCP server. The server stays up; the failed job is marked `failed`
with the crash log attached.

**`dry_run=True` is the default for all COMSOL tools.** Every COMSOL tool
validates its arguments and probes COMSOL connectivity *without solving*, then
returns the exact command it would run, what patches would be applied, and
where the `.mph` files would be saved. This makes the suite fully usable and
testable off-network — one flag (`dry_run=False`) away from a real solve.

**Jobs survive server restarts.** Every background job writes its state to
`runs/<job_id>/job.json`. If the MCP server is restarted mid-solve, history
rehydrates from disk and reports the interrupted job as `failed (interrupted)` —
nothing disappears silently.

---

## Sharing with lab mates

Everyone with access to the shared drive gets the full pipeline. They need:

1. **Access the shared drive** and navigate to
   `Z:\users\Alex\Chip Simulation\COMSOL Simulation Suite`
   (replace `Z:` with your actual drive letter)
2. **Install**: open a terminal in that folder and run
   `pip install -e .` (add `[comsol]` on machines with a COMSOL licence)
3. **Configure** their AI client (see [Deployment](#deployment--connecting-to-an-ai-model))
4. **Optional**: copy `config/paths.example.toml` → `config/paths.toml` to
   override the Python interpreter, Julia path, or COMSOL host

Nobody needs to know the pipeline script internals — they describe what they
want and poll for results.

**Quickstart:** Open Claude Code inside this folder and ask *"Set up the
COMSOL suite for me"* — it will run the install and write the MCP config
automatically. Restart Claude Code once and the 18 tools are live. If you are
connecting to a network COMSOL server, say so: *"Set up the COMSOL suite,
connecting to `comsol-server.lab.local`."*

---

## Project structure

```
Chip-Simulation-Suite/
├── comsol_suite/
│   ├── config.py       — path resolution (env > paths.toml > built-in default)
│   ├── runner.py       — patch_script() + run_command() (sole subprocess spawn point)
│   ├── jobs.py         — JobRegistry: background threads, state persisted to job.json
│   ├── server.py       — FastMCP app; registers all 18 tools
│   └── tools/
│       ├── cad.py      — generate_cad, verify_cad, run_custom_cad
│       ├── comsol.py   — build/sweep/export; dry_run=True default; mph_paths in results
│       └── fitting.py  — ABCD fit (Python, parallel), Julia fit + dispersion
├── tests/              — 17 pytest tests (CAD + COMSOL dry-run + fitting end-to-end)
├── docs/
│   ├── TOOLS.md        — full tool reference with examples
│   ├── INSTALL.md      — detailed installation guide
│   ├── PIPELINE.md     — data formats and stage-to-stage flow
│   └── ARCHITECTURE.md — design decisions and component map
├── config/
│   └── paths.example.toml   — copy to paths.toml for per-machine overrides
└── pyproject.toml
```

---

## Documentation

- [`docs/INSTALL.md`](docs/INSTALL.md) — per-machine setup and MCP client config
- [`docs/TOOLS.md`](docs/TOOLS.md) — every tool, its inputs/outputs, and examples
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design rationale and component map
- [`docs/PIPELINE.md`](docs/PIPELINE.md) — CAD→COMSOL→fitting data flow and file formats

---

## Licence

Proprietary — internal lab use.

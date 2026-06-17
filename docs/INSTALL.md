# Install & wire into Claude Code

## 1. Prerequisites

- Python ≥ 3.10 (the same interpreter you use for the rest of the pipeline is fine).
- The suite must live **inside the `Chip Simulation` folder** (it auto-discovers
  the pipeline scripts relative to its own location). If you put it elsewhere,
  set `chip_sim_root` (see step 3).

## 2. Install the package

```bash
cd "Chip Simulation/COMSOL Simulation Suite"
pip install -e .                 # core (CAD + fitting + server)
# optional extras:
pip install -e ".[comsol]"       # adds mph for real COMSOL solves
pip install -e ".[dev]"          # adds pytest
```

Confirm it resolved your paths:

```bash
python -c "from comsol_suite.config import load_config; import json; \
           print(json.dumps(load_config().as_dict(), indent=2))"
```

Every `scripts.*` and `data.*` entry should point at a real file.

## 3. (Optional) Per-machine configuration

Defaults work out-of-the-box when the suite sits inside `Chip Simulation`. To
override anything, copy the example and edit it:

```bash
cp config/paths.example.toml config/paths.toml
```

Or use environment variables (these win over the file):

| Variable           | Meaning                                   |
|--------------------|-------------------------------------------|
| `CHIP_SIM_ROOT`    | Path to the `Chip Simulation` folder      |
| `COMSOL_HOST`      | COMSOL server hostname (omit = local)     |
| `COMSOL_PORT`      | COMSOL server port (default 2036)         |
| `JULIA_BIN`        | Julia executable (default `julia`)        |
| `CHIP_SIM_PYTHON`  | Python used to run wrapped scripts        |

## 4. Add to Claude Code

Add an entry to your Claude Code MCP settings (user `~/.claude/settings.json` or
a project `.claude/settings.json`):

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

- Use the **absolute path** to the suite for `cwd`.
- Drop the `env` block to run COMSOL locally (or omit `COMSOL_HOST` entirely).
- If `python` is not the right interpreter on your PATH, give a full path
  (e.g. `"command": "C:/Users/you/env/python.exe"`).

Restart Claude Code. The tools appear under `/mcp` — try asking it to
*"describe the COMSOL suite config"* or *"generate the CAD and verify it."*

## 5. Verify

```bash
pip install -e ".[dev]"
pytest -q
```

`test_cad_end_to_end` and `test_fitting_end_to_end` run the real pipeline
against the project data; `test_comsol_dryrun` exercises the COMSOL wrapping
without needing a connection.

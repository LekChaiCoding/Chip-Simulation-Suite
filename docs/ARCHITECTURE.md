# Architecture

## Goal

Expose the lab's three-stage chip-simulation pipeline (CAD → COMSOL → fitting)
as MCP tools so it can be driven from Claude Code, **without rewriting any of the
validated physics/geometry code**.

## Core decision: a thin subprocess orchestrator

The suite does *not* import and call the pipeline scripts in-process as a
library. It launches them as **subprocesses**. Two findings forced this:

1. **Some upstream scripts execute on import.**
   `converter_group_recreation.py` (and `cadfun.py`) write their output GDS at
   module top-level — there is no `if __name__ == "__main__"` guard. Importing
   them would trigger side effects.

2. **The COMSOL scripts use JPype, which is crash-prone.**
   The project's own notes document `list()`-on-Java-iterable SIGSEGVs and silent
   batch failures. Running COMSOL in a child process means a JPype crash kills
   that child, **never the MCP server**.

Subprocessing also gives us free process isolation, clean per-job logging, and an
obvious place to enforce timeouts.

### The one exception: in-process verification

`verify_cad` reuses `cad_verify_gds.py` *in-process*, because that checker is
import-safe (pure `gdstk`/`numpy`, `__main__`-guarded) and we want its exact
pass/fail logic rather than a reimplementation. We import it by file path and
point its `RECR` constant at the GDS under test.

## Path patching (keeping originals read-only)

Several upstream scripts hard-code absolute paths — a Linux `/mnt/smb/...` mount
for CAD output, and output folders *inside the tracked `JosephsonCircuit` tree*
for the fits. We treat those scripts as a read-only, vertex-validated source of
truth, so instead of editing them we make a **path-redirected copy**:

```
runner.patch_script(src, dest, { r"^OUT_GDS\s*=.*$": 'OUT_GDS = r"<runs/.../x.gds>"' })
```

`patch_script` rewrites only the specific assignment lines (whole-line regex
replace) and **raises if a pattern stops matching** — so an upstream refactor
becomes a loud failure, not a silent wrong result. The geometry/physics code is
byte-for-byte unchanged; only I/O destinations move into `runs/`.

This is why the suite never writes into `bridge/`, `gds/`, or the original
`Scripts/` folders.

## Components

```
comsol_suite/
├── config.py   — resolve chip_sim_root, script/data paths, COMSOL host,
│                  interpreters. Env var > config/paths.toml > built-in default.
├── runner.py   — patch_script() + run_command() (the only subprocess spawn point)
├── jobs.py     — JobRegistry: background threads, UUID job ids, status persisted
│                  to runs/<job_id>/job.json (survives server restarts)
├── server.py   — FastMCP app; registers every tool; injects the shared registry
└── tools/
    ├── cad.py      — generate_cad (subprocess), verify_cad (in-process checker)
    ├── comsol.py   — build/validate/sweep/export; default dry_run=True
    └── fitting.py  — run_abcd_fit (Python), fit_stub_sweep/analyze_dispersion (Julia)
```

## Job lifecycle

Long stages (COMSOL solves, fits) return immediately with a `job_id`:

```
submit ──► pending ──► running ──► completed | failed
                          │
                          └─ writes runs/<job_id>/run.log
           runs/<job_id>/job.json updated on every transition
```

`get_job_status` / `get_job_result` / `list_jobs` query the registry. Because
`job.json` is persisted, history and status survive an MCP-server restart; a job
that was mid-run when the server died is rehydrated as `failed (interrupted)`.

Quick tools (`generate_cad`, `verify_cad`, `comsol_health_check`,
`describe_config`) run synchronously and return their result directly.

## The COMSOL boundary

Every COMSOL tool defaults to `dry_run=True`: it validates arguments and runs
`comsol_health_check` (mph import + TCP probe of the configured host) **without
solving**, returning the exact command it *would* run. Passing `dry_run=False`
on a machine with a live COMSOL connection submits the real solve as a job. This
keeps the suite useful and testable off-network while remaining one flag away
from a real run.

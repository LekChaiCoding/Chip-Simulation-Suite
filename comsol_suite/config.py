"""Configuration & path resolution for the COMSOL Simulation Suite.

This module is the single source of truth for *where things live* on the
current machine. Everything else in the package asks :data:`CONFIG` for paths
rather than hard-coding them, which is what makes the suite portable across the
different lab machines (Windows, WSL, the COMSOL server, ...).

Resolution order for every setting (first hit wins):

    1. environment variable        e.g. ``COMSOL_HOST``, ``CHIP_SIM_ROOT``
    2. ``config/paths.toml``       a per-machine file (gitignored)
    3. built-in default            computed relative to this repo

The built-in default assumes the suite repo sits *inside* the top-level
"Chip Simulation" folder, e.g. ::

    Chip Simulation/
    ├── COMSOL Simulation/ ...      <- original pipeline scripts
    ├── JosephsonCircuit/  ...      <- fitting scripts + bridge data
    ├── gds/               ...      <- reference GDS
    └── COMSOL Simulation Suite/    <- THIS repo
        └── comsol_suite/config.py  <- this file

so ``chip_sim_root`` defaults to the repo's parent directory.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

# ``tomllib`` is stdlib on 3.11+; fall back to the ``tomli`` backport on 3.10.
try:  # pragma: no cover - trivial import shim
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # TOML config simply unavailable; defaults still work.


# Repo root = two levels up from this file (comsol_suite/config.py -> repo).
REPO_ROOT = Path(__file__).resolve().parent.parent
PATHS_TOML = REPO_ROOT / "config" / "paths.toml"

# ── Default *relative* locations of the wrapped source assets ────────────────
# (relative to chip_sim_root). Centralised here so there is exactly one place
# to update if the upstream layout ever moves.
_DEFAULT_SCRIPTS: Dict[str, str] = {
    "cad_generator": "COMSOL Simulation/001/Scripts/converter_group_recreation.py",
    "cad_verifier":  "COMSOL Simulation/001/Scripts/cad_verify_gds.py",
    "comsol_build":  "COMSOL Simulation/001/Scripts/recreate_and_solve.py",
    "comsol_sweep":  "COMSOL Simulation/001/Scripts/sweep_stub_length.py",
    "comsol_export": "COMSOL Simulation/001/Scripts/export_touchstone.py",
    "abcd_fit":      "JosephsonCircuit/Fits/003_data/ABCD_Matrix/Scripts/abcd_fit.py",
    "julia_fit":     "JosephsonCircuit/Fits/003_data/Scripts/fit_stub_sweep.jl",
    "julia_disp":    "JosephsonCircuit/Fits/003_data/Scripts/dispersion_analysis.jl",
}
_DEFAULT_DATA: Dict[str, str] = {
    "reference_gds":   "gds/converter_group_recreation.gds",
    "bridge003_sweep": "JosephsonCircuit/bridge/003/stub_length_sweep.dat",
}


def _load_toml() -> Dict[str, Any]:
    """Return the parsed ``config/paths.toml`` (empty dict if absent/unreadable)."""
    if tomllib is None or not PATHS_TOML.is_file():
        return {}
    try:
        with open(PATHS_TOML, "rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # pragma: no cover - defensive, never fatal
        print(f"[config] WARNING: could not parse {PATHS_TOML}: {exc}",
              file=sys.stderr)
        return {}


@dataclass(frozen=True)
class SuiteConfig:
    """Fully-resolved, absolute configuration for one machine.

    Built once via :func:`load_config`. All path attributes are absolute
    :class:`~pathlib.Path` objects; ``comsol_host`` / ``comsol_port`` may be
    ``None`` when COMSOL runs locally.
    """

    chip_sim_root: Path
    runs_dir: Path
    python_bin: str
    julia_bin: str
    comsol_host: Optional[str]
    comsol_port: int
    scripts: Dict[str, Path] = field(default_factory=dict)
    data: Dict[str, Path] = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------------
    def script(self, name: str) -> Path:
        """Absolute path to a wrapped source script (raises if unknown)."""
        if name not in self.scripts:
            raise KeyError(f"unknown script '{name}'; "
                           f"known: {sorted(self.scripts)}")
        return self.scripts[name]

    def datum(self, name: str) -> Path:
        """Absolute path to a wrapped data asset (raises if unknown)."""
        if name not in self.data:
            raise KeyError(f"unknown data asset '{name}'; "
                           f"known: {sorted(self.data)}")
        return self.data[name]

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view, handy for the ``describe_config`` tool."""
        return {
            "chip_sim_root": str(self.chip_sim_root),
            "runs_dir": str(self.runs_dir),
            "python_bin": self.python_bin,
            "julia_bin": self.julia_bin,
            "comsol_host": self.comsol_host,
            "comsol_port": self.comsol_port,
            "scripts": {k: str(v) for k, v in self.scripts.items()},
            "data": {k: str(v) for k, v in self.data.items()},
        }


def _resolve(base: Path, value: str) -> Path:
    """Resolve ``value`` against ``base`` unless it is already absolute."""
    p = Path(value)
    return p if p.is_absolute() else (base / p)


@lru_cache(maxsize=1)
def load_config() -> SuiteConfig:
    """Build (and cache) the :class:`SuiteConfig` for this machine."""
    toml = _load_toml()
    scripts_toml = toml.get("scripts", {}) or {}
    data_toml = toml.get("data", {}) or {}

    # chip_sim_root: env > toml > default(parent of repo)
    chip_sim_root = Path(
        os.environ.get("CHIP_SIM_ROOT")
        or toml.get("chip_sim_root")
        or REPO_ROOT.parent
    ).resolve()

    # Interpreters.
    python_bin = (os.environ.get("CHIP_SIM_PYTHON")
                  or toml.get("python_bin")
                  or sys.executable)
    julia_bin = (os.environ.get("JULIA_BIN")
                 or toml.get("julia_bin")
                 or "julia")

    # COMSOL endpoint (optional).
    comsol_host = os.environ.get("COMSOL_HOST") or toml.get("comsol_host") or None
    comsol_port = int(os.environ.get("COMSOL_PORT")
                      or toml.get("comsol_port")
                      or 2036)

    # Resolve every script / data path (env override key e.g. SCRIPT_CAD_GENERATOR).
    scripts = {
        name: _resolve(chip_sim_root,
                       os.environ.get(f"SCRIPT_{name.upper()}")
                       or scripts_toml.get(name)
                       or default)
        for name, default in _DEFAULT_SCRIPTS.items()
    }
    data = {
        name: _resolve(chip_sim_root,
                       os.environ.get(f"DATA_{name.upper()}")
                       or data_toml.get(name)
                       or default)
        for name, default in _DEFAULT_DATA.items()
    }

    runs_dir = Path(os.environ.get("CHIP_SIM_RUNS")
                    or toml.get("runs_dir")
                    or (REPO_ROOT / "runs")).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)

    return SuiteConfig(
        chip_sim_root=chip_sim_root,
        runs_dir=runs_dir,
        python_bin=python_bin,
        julia_bin=julia_bin,
        comsol_host=comsol_host,
        comsol_port=comsol_port,
        scripts=scripts,
        data=data,
    )


# Eagerly-available singleton for convenience (callers may also call
# load_config() directly; it is cached so both styles share one instance).
CONFIG = load_config()

"""Shared fixtures — chiefly: skip what this checkout cannot possibly run.

The suite wraps two bodies of work. One is the qleap qubit line (F0-F3), whose
scripts live in this repo. The other is the older JTWPA/bridge tooling, whose
scripts and reference data live in two vendored trees —
``resources/COMSOL Simulation/`` and ``resources/JosephsonCircuit/`` — that are
**not present in this checkout** and never have been (they are not gitignored;
they simply were not vendored here).

Twenty tests exercised that second surface and failed on every run, all with the
same root cause. A permanently red suite is worse than useless: it trains
everyone to ignore the result, and it hides a genuine regression the day one
appears. These tests are not wrong and are not deleted — they skip, with a
message naming the exact file that is missing, so restoring the asset tree turns
them back on with no edit.

What is deliberately NOT hidden: the tools themselves still fail loudly at
runtime with the missing path, and ``describe_config`` now reports which
configured scripts are absent, so an agent offered one of those tools can see
why it cannot work rather than inferring it from a subprocess error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comsol_suite.config import load_config


def _missing(*script_keys: str, data_keys: tuple[str, ...] = ()) -> str | None:
    """Return a skip reason if any named script/data asset is absent."""
    cfg = load_config()
    for key in script_keys:
        path = cfg.script(key)
        if not Path(path).is_file():
            return (f"vendored asset absent from this checkout: "
                    f"{key} -> {path}")
    data = getattr(cfg, "data", {}) or {}
    for key in data_keys:
        path = Path(data.get(key, ""))
        if not path.exists():
            return (f"vendored asset absent from this checkout: "
                    f"{key} -> {path}")
    return None


def requires(*script_keys: str, data: tuple[str, ...] = ()):
    """Decorator: skip a test whose vendored inputs are not in this checkout."""
    reason = _missing(*script_keys, data_keys=data)
    return pytest.mark.skipif(reason is not None, reason=reason or "")


@pytest.fixture
def legacy_cad_assets():
    reason = _missing("cad_generator", "cad_verifier", data_keys=("reference_gds",))
    if reason:
        pytest.skip(reason)

"""This venv can run a FULL walk: every blessed `--with` dep imports here.

Measured defect this canary pins (2026-08-14): `qleap_line_execute` launches
`execute_line.py` under THIS server's own interpreter (`sys.executable`,
`comsol_suite/tools/qleap_factory.py::_cli_argv`) — a deliberate choice, since
`uv run` over SMB added 140+ s and got SIGKILLed. The walk's blocks then
import their deps in-process. `ChipReconstruction008` burned a 57-minute real
B1 solve and died at B2_coupling on `ModuleNotFoundError: No module named
'networkx'` — the ONE package this venv lacked from the blessed set (andon
`OPEN_B2_coupling_chip_20260814T161356_756034`, closed with that root cause).

On 2026-08-17 the standing decision "MCP is for status/dry-run only" was
deliberately reversed (Qwen drives full walks through this server), networkx
was installed, and this test exists so the gap class fails LOUDLY at test
time instead of an hour into the next walk. The source of truth is parsed
from RUNNING.md's own launch incantation — a second hand-kept list here would
be one more declaration to drift.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

RUNNING_MD = (
    Path(__file__).resolve().parents[2].parent
    / "QubitDesignPipeline"
    / "NewPipeline"
    / "RUNNING.md"
)

#: Distribution name (as RUNNING.md spells it) → the module a block imports.
IMPORT_NAMES = {
    "mph": "mph",
    "numpy": "numpy",
    "scipy": "scipy",
    "scikit-rf": "skrf",
    "typedunits": "tunits",
    "gdstk": "gdstk",
    "pyyaml": "yaml",
    "networkx": "networkx",
    "matplotlib": "matplotlib",
}


def blessed_with_list() -> list[tuple[str, str | None]]:
    """(dist, pinned_version|None) parsed off RUNNING.md's incantation."""
    text = RUNNING_MD.read_text(encoding="utf-8")
    found = re.findall(r"--with\s+([A-Za-z0-9._-]+)(?:==([0-9.]+))?", text)
    # Deduplicate preserving order; the incantation may appear more than once.
    seen: dict[str, str | None] = {}
    for dist, version in found:
        seen.setdefault(dist, version or None)
    return list(seen.items())


def test_the_incantation_is_still_where_this_test_reads_it() -> None:
    """The premise, asserted: an empty parse would vacuously pass below."""
    assert RUNNING_MD.is_file(), f"RUNNING.md moved: {RUNNING_MD}"
    blessed = blessed_with_list()
    assert len(blessed) >= 9, (
        "RUNNING.md's --with list parsed to "
        + str(len(blessed))
        + " entries; the launch incantation moved or changed shape, so this "
        "canary is reading nothing"
    )
    unknown = [dist for dist, _ in blessed if dist not in IMPORT_NAMES]
    assert unknown == [], (
        "RUNNING.md added dep(s) this canary cannot map to an import name: "
        + str(unknown)
        + " — extend IMPORT_NAMES so the new dep is actually checked"
    )


@pytest.mark.parametrize("dist,version", blessed_with_list())
def test_every_blessed_walk_dep_imports_in_this_venv(dist, version) -> None:
    module_name = IMPORT_NAMES.get(dist)
    if module_name is None:
        pytest.fail(f"no import mapping for {dist}; extend IMPORT_NAMES")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        pytest.fail(
            f"{dist} (import {module_name!r}) is missing from this venv — an "
            "MCP-dispatched walk will die on it in-process exactly like "
            f"ChipReconstruction008 died on networkx: {exc}"
        )
    if version is not None:
        actual = getattr(module, "__version__", None)
        assert actual == version, (
            f"{dist} is pinned to {version} in RUNNING.md but this venv has "
            f"{actual} — a walk here and a walk under uv would measure with "
            "different code"
        )

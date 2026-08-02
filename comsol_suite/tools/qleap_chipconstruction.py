"""qleap ChipConstruction tools — MCP wrappers for the mph->GDS->real-JJ->
block/chip-assembly factory in ``<repo>/simulations/ChipConstruction/``.

Same conventions as :mod:`qleap`, :mod:`qleap_nt2`, and :mod:`qleap_cct001`:
thin argv-building wrappers, never import ``chipconstructionlib`` into the
server process (it dynamically loads sibling modules at import time). Split
by whether a step touches COMSOL:

- COMSOL-touching steps (``mph_preflight``, ``export_tile``) follow the
  CCT001/NT2 pattern: ``dry_run=True`` default, background execution via the
  shared :class:`JobRegistry` when launched.
- Pure gdstk/numpy steps (schematics, JJ insertion, validity/diff checkers,
  block/chip assembly, verification) follow :mod:`cad`'s
  ``assemble_geometry``/``verify_cad`` precedent: no COMSOL connection
  required, so they run synchronously and return their result directly.

See ``simulations/ChipConstruction/tools/FACTORY_LAYOUT.md`` for the full
floor plan and ``tools/PROCESSES.md`` for the exact per-script CLI this
module wraps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import load_config
from ..jobs import JobRegistry
from ..runner import (extract_trailing_json, new_log_path, run_command,
                      update_last_log_pointer)

TILES = ("U0_R0", "U0_R1", "U1_R0", "U1_R1", "U2_R0", "U2_R1")


def _repo_root() -> Path:
    """The qleap repo, from the config's own marker-based resolution.

    NOT ``chip_sim_root.parent``: that read the containment root as if it were
    the legacy assets dir, so every campaign path landed one level ABOVE the
    checkout — where a status call silently created an empty shadow
    ``simulations/ChipConstruction/`` tree outside the repo.
    """
    return Path(load_config().repo_root)


def _chipcon_dir() -> Path:
    return _repo_root() / "simulations" / "ChipConstruction"


def _tools_dir() -> Path:
    return _chipcon_dir() / "tools"


def _tile(tile: str) -> str:
    if tile not in TILES:
        raise ValueError(f"unknown tile {tile!r}; expected one of {TILES}")
    return tile


def _preflight(tool: str, argv: List[str], outputs: List[str]) -> Dict[str, Any]:
    return {
        "dry_run": True,
        "tool": tool,
        "would_run": [str(a) for a in argv],
        "outputs_would_write": outputs,
        "note": ("Validated only. Re-call with dry_run=False to launch as a "
                 "background job (get_job_status / get_job_result to follow)."),
    }


def _preflight_sync(tool: str, argv: List[str], outputs: List[str]) -> Dict[str, Any]:
    """Dry-run reply for a MUTATING synchronous step.

    These steps need no COMSOL, so the module originally ran them immediately —
    but "needs no solver" is not the same as "changes nothing": they overwrite
    the deliverable masks (``<TILE>_layered.gds``, ``block_A.gds``, ``chip.gds``,
    ``hexlattice_*.gds``). With no ``dry_run`` argument they were invisible to
    BOTH of the agent's defences, since each keys off exactly that argument
    (``QubitDesignPipeline/agent/policy.py``): no human approval was requested
    and no upstream-record check ran before a shipped mask was rewritten.

    The genuinely read-only tools (``_gds_validity_check``, ``_gds_diff`` and
    ``_status`` — each verified to only read and print) deliberately keep no
    ``dry_run``: gating a check would only train people to click through.
    ``_verify_block`` is NOT among them, despite its name — see its docstring.
    """
    return {
        "dry_run": True,
        "tool": tool,
        "would_run": [str(a) for a in argv],
        "outputs_would_write": outputs,
        "note": ("Validated only — nothing was written. Re-call with "
                 "dry_run=False to run it, which requires human approval."),
    }


def _launch(registry: JobRegistry, tool: str, argv: List[str], cwd: Path,
            collect_dir: Path, timeout_s: float, debug: bool) -> Dict[str, Any]:
    """Run ``argv`` in a DETACHED worker; collect GDS/JSON/PNG outputs.

    Detached, not threaded: this server is spawned per MCP client and a daemon
    thread dies with it, which lost real solves mid-run. See
    :mod:`comsol_suite.job_runner`.
    """
    job = registry.submit_detached(
        tool, argv, cwd=cwd, timeout_s=timeout_s, debug=debug,
        collect_dir=collect_dir, collect_patterns=("*.gds", "*.json", "*.png"))
    return {"job_id": job.job_id, "status": job.status}


#: How much of the log the gate-report parse is allowed to look at. Generous:
#: ``block_checker.py``'s report alone runs to dozens of lines.
_PARSE_TAIL_LINES = 200


def _run_sync(tool: str, argv: List[str], timeout_s: float = 300,
             debug: bool = False) -> Dict[str, Any]:
    """Run a pure gdstk/numpy ChipConstruction script to completion
    synchronously (no COMSOL, no JobRegistry — matches ``assemble_geometry``/
    ``verify_cad``'s "no COMSOL connection required, runs synchronously"
    precedent in :mod:`cad`).

    The log goes to a path unique to this invocation (see
    :func:`comsol_suite.runner.new_log_path`); ``logs/<tool>_last.log`` is kept
    as a copy for humans only. It used to BE the log, one fixed name per tool,
    which meant two concurrent calls to the same tool truncated each other's
    only record — and since the verdict below is read back out of that file,
    one call could return the other's gate report as its own.
    """
    log_path = new_log_path(_chipcon_dir() / "logs", tool)
    res = run_command(argv, log_path=log_path, cwd=_chipcon_dir(),
                      timeout_s=timeout_s, debug=debug)
    update_last_log_pointer(log_path, tool)
    parsed, parse_error = _extract_trailing_json(res.log_tail(_PARSE_TAIL_LINES))
    return {
        "ok": res.ok,
        "returncode": res.returncode,
        "duration_s": round(res.duration_s, 2),
        "log_path": str(log_path),
        "log_tail": res.log_tail(60),
        "parsed": parsed,
        # Never a bare ``None`` with no account of itself: these docstrings
        # promise the caller a {pass, problems} gate report, and a silent null
        # is how a verdict gets lost instead of read.
        "parse_error": parse_error,
    }


def _extract_trailing_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    """This module's tail budget, applied to the suite's shared parser.

    The parse itself is :func:`comsol_suite.runner.extract_trailing_json` — it
    moved there once ``qleap_factory`` turned out to be running an older, buggy
    copy of the same idea. Only the "how much did you look at" number is local.
    """
    return extract_trailing_json(text, tail_lines=_PARSE_TAIL_LINES)


def _uv_argv(script: str, extras: List[str], args: List[str]) -> List[str]:
    return ["uv", "run", "--no-project", *extras, "python",
            str(_tools_dir() / script), *args]


# ─────────────────────────────────────────────────────────────────────────────
# P0a/P0b — schematics (foreground, no COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_build_schematics(dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """P0a/P0b: (re)build the block-internal + chip-level tiling schematics.

    No arguments — always regenerates ``Data/block_layout_schematic.json/.gds``
    and ``Data/chip_block_schematic.json/.gds`` from the confirmed tile-grid
    and vertical-stagger conventions in ``FACTORY_LAYOUT.md``. Gate: Alex
    sign-off on the rendered PNGs, done outside this tool.
    """
    argv = _uv_argv("build_schematics.py", ["--with", "gdstk", "--with", "numpy"], [])
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_build_schematics", argv, [str(_chipcon_dir() / "layouts")])
    result = _run_sync("qleap_chipconstruction_build_schematics", argv)
    result["outputs"] = [
        str(_chipcon_dir() / "Data" / "block_layout_schematic.json"),
        str(_chipcon_dir() / "Data" / "block_layout_schematic.gds"),
        str(_chipcon_dir() / "Data" / "chip_block_schematic.json"),
        str(_chipcon_dir() / "Data" / "chip_block_schematic.gds"),
    ]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# P1/P2 — COMSOL-touching steps (background, dry_run default)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_mph_preflight(
    registry: JobRegistry,
    tile: str,
    cores: int = 8,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """P1/P2 step 1: confirm a Stitching005 tile ``.mph`` is loadable and has
    the expected per-letter JJ port/lumped-element structure, before
    spending COMSOL-slot time on export."""
    tile = _tile(tile)
    argv = [*_uv_argv("check_mph_preflight.py", ["--with", "mph"],
                      ["--tile", tile, "--cores", str(cores)])]
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_mph_preflight", argv, [])
    # Foreground, NOT _launch. This server is spawned per MCP client and is
    # short-lived, so a background job's daemon thread dies with the process:
    # a real run launched this, got "running", and the next poll reported
    # `interrupted: MCP server restarted mid-run` after 20 s with an empty
    # run.log — the agent then looped hunting for output that was never
    # produced. The preflight only loads a .mph and inspects it (its own cap is
    # 300 s), so running it to completion inside the call returns a real result
    # and leaves no orphan. `build_hexlattice` already takes this shape at
    # 1800 s; this was the outlier.
    return _run_sync("qleap_chipconstruction_mph_preflight", argv,
                     timeout_s=300, debug=debug)


def qleap_chipconstruction_export_tile(
    registry: JobRegistry,
    tile: str,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """P1/P2 step 2: mph2gds export of one tile's raw geometry.

    Prerequisite: ``comsol -multi on mphserver`` already running and
    ``CLASSPATH`` set to the COMSOL plugin jars (both outside this tool's
    control — it does not start COMSOL for you)."""
    tile = _tile(tile)
    argv = _uv_argv("export_tile_gds.py", ["--with", "mph", "--with", "numpy"],
                    ["--tile", tile])
    data_dir = _chipcon_dir() / "Data"
    if dry_run:
        return _preflight("qleap_chipconstruction_export_tile", argv,
                          [str(data_dir / "tile_registry.json")])
    return _launch(registry, "qleap_chipconstruction_export_tile", argv,
                   cwd=_chipcon_dir(), collect_dir=data_dir,
                   timeout_s=1800, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# P1/P2 — JJ insertion + downstream (foreground, no COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_insert_jj(
    tile: Optional[str] = None,
    all_tiles: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """P1/P2 steps 4-6 (formalized as ``run_tile_pipeline.py``): JJ
    insertion + hooks, qubit-center measurement, Al probe pads, flux traps,
    then ``gds_validity_checker.py`` on the result. One of ``tile`` or
    ``all_tiles=True`` is required. No COMSOL — runs synchronously; the
    ``--all`` path chains 6 tiles x 4 sub-steps and can take a few minutes.

    Run ``generate_jj_configs.py`` first (no args, shared config — not
    wrapped here since it has no per-tile parameter) whenever the JJ/Al-pad/
    flux-trap config changes.
    """
    if bool(tile) == bool(all_tiles):
        raise ValueError("pass exactly one of tile= or all_tiles=True")
    args = ["--all"] if all_tiles else ["--tile", _tile(tile)]
    argv = _uv_argv("run_tile_pipeline.py", [], args)
    timeout_s = 3600 if all_tiles else 600
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_insert_jj", argv, [str(_chipcon_dir() / "<tile>" / "work" / "<tile>_layered.gds")])
    return _run_sync("qleap_chipconstruction_insert_jj", argv,
                     timeout_s=timeout_s, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# Console thumbnails (foreground, no COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_render_cell_thumbs(
    tile: Optional[str] = None,
    all_tiles: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Regenerate the Chippy Console's per-qubit and per-unit-cell line-art
    thumbnails (``layouts/cells/<TILE>_<LETTER>.png`` + ``layouts/cells/
    <TILE>.png``) from each tile's layered mask. One of ``tile`` or
    ``all_tiles=True`` is required. No COMSOL — runs synchronously.

    Gated like the builders, not like the checkers: these PNGs are what the
    Console SHOWS for a qubit, so a stale or missing one makes the UI describe
    geometry that isn't on the mask any more — the exact failure the Console's
    honest-source rule exists to prevent.

    Slow for a rendering step (~40-70 s per tile, so ~5 min for all six): the
    exported etch is strip-decomposed into ~24k slabs per tile and has to be
    unioned before it can be stroked as line art, or every internal slab edge
    draws and the thumbnail comes out as hatching. See the script's docstring.

    Prerequisites per tile: ``<TILE>/work/<TILE>_layered.gds`` and
    ``Data/<TILE>_qubit_origin.json`` (i.e. ``_insert_jj`` has run). Tiles with
    no layered GDS are reported in ``tiles_skipped`` rather than failing the
    call; a tile that HAS a mask but is missing a letter's measured centre is a
    hard error, because a Console card with a hole in it looks deliberate.

    The framing knobs (crop window, pixel size, stroke weights) are deliberately
    not exposed here — they are script flags for a human iterating on the look,
    and the Console depends on the defaults. Nor is the output directory: the
    Console reads one fixed path, and an overridable one is just a way to write
    PNGs somewhere nothing serves them.
    """
    if bool(tile) == bool(all_tiles):
        raise ValueError("pass exactly one of tile= or all_tiles=True")
    args = ["--all"] if all_tiles else ["--tile", _tile(tile)]
    argv = _uv_argv("render_cell_thumbs.py",
                    ["--with", "gdstk", "--with", "matplotlib", "--with", "numpy"],
                    args)
    cells_dir = _chipcon_dir() / "layouts" / "cells"
    if dry_run:
        return _preflight_sync(
            "qleap_chipconstruction_render_cell_thumbs", argv,
            [str(cells_dir / "<tile>_<letter>.png"), str(cells_dir / "<tile>.png")])
    result = _run_sync("qleap_chipconstruction_render_cell_thumbs", argv,
                       timeout_s=2400 if all_tiles else 600, debug=debug)
    result["out_dir"] = str(cells_dir)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Generic checkers (foreground, repo-generic per FACTORY_LAYOUT.md)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_gds_validity_check(
    gds_path: str,
    layer_config: Optional[str] = None,
    degenerate_tolerance_um2: Optional[float] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Structural GDS validity: degenerate/self-intersecting polygons, layer
    conformance against ``layer_config.json``, broken cell hierarchy. Takes
    any GDS path — not campaign-specific; candidate for promotion to
    :mod:`cad` if a second campaign wants it (see ``FACTORY_LAYOUT.md``)."""
    args = ["--gds", gds_path]
    if layer_config:
        args += ["--layer-config", layer_config]
    if degenerate_tolerance_um2 is not None:
        args += ["--degenerate-tolerance-um2", str(degenerate_tolerance_um2)]
    argv = _uv_argv("gds_validity_checker.py", ["--with", "gdstk"], args)
    return _run_sync("qleap_chipconstruction_gds_validity_check", argv, debug=debug)


def qleap_chipconstruction_gds_diff(
    gds_a: str,
    gds_b: str,
    layer: Optional[int] = None,
    allow_region: Optional[List[float]] = None,
    tolerance_um2: Optional[float] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """XOR-based geometric diff between two GDS files. ``allow_region`` is
    ``[cx, cy, w, h]`` (µm) — a region where a diff is expected (e.g. a JJ
    slit); pass multiple times by calling this tool once per region if more
    than one allow-region is needed. See ``GOTCHAS.md`` for why this isn't
    the gate for a fully-layered tile (Al pads/flux traps postdate its
    allow-region scope) — it remains the right tool for raw-vs-JJ-only and
    block/chip seam-overlap comparisons."""
    args = ["--a", gds_a, "--b", gds_b]
    if layer is not None:
        args += ["--layer", str(layer)]
    if allow_region:
        args += ["--allow-region", ",".join(str(v) for v in allow_region)]
    if tolerance_um2 is not None:
        args += ["--tolerance-um2", str(tolerance_um2)]
    argv = _uv_argv("gds_diff_checker.py", ["--with", "gdstk"], args)
    return _run_sync("qleap_chipconstruction_gds_diff", argv, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# P3/P4/P5 — assembly + verification (foreground, no COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_assemble_block(dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """P3 ONLY: merge the 6 layered per-tile GDS files into ``OptimizedModels/
    block_A.gds``, using the measured pitch from ``Data/tile_registry.json``.
    No arguments — always rebuilds from whatever ``<TILE>/work/<TILE>_layered.gds``
    currently holds for all 6 tiles.

    This is the PRE-ALIGNMENT intermediate state (internal seams not yet
    tapered — see P3.6) — useful for inspection, but a block isn't "done"
    until ``qleap_chipconstruction_build_block`` has run. Prefer that for
    any result you intend to use downstream (P4, tape-out review, etc.).
    """
    argv = _uv_argv("assemble_block.py", ["--with", "gdstk", "--with", "numpy"], [])
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_assemble_block", argv, [str(_chipcon_dir() / "OptimizedModels" / "block_A.gds")])
    result = _run_sync("qleap_chipconstruction_assemble_block", argv, timeout_s=600, debug=debug)
    result["output"] = str(_chipcon_dir() / "OptimizedModels" / "block_A.gds")
    return result


def qleap_chipconstruction_build_block(dry_run: bool = True, debug: bool = False) -> Dict[str, Any]:
    """**Canonical P3 entry point.** Chains, as one command:
    ``assemble_block.py`` (P3) -> ``align_seam_couplers.py`` (P3.6: taper
    every internal tile-tile seam's coupler rails to a shared line,
    replacing ``extend_coupler_ends.py``'s flat unaligned extension there)
    -> ``gds_validity_checker.py`` -> ``block_checker.py`` (P5). No
    arguments — always a full, deterministic rebuild from whatever
    ``<TILE>/work/<TILE>_layered.gds`` currently holds for all 6 tiles.
    Fails loudly (non-zero exit, surfaced via ``ok: false``) at the first
    failing step. Returns the P5 gate report as ``parsed``.
    """
    argv = _uv_argv("build_block.py", [], [])
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_build_block", argv, [str(_chipcon_dir() / "OptimizedModels" / "block_A.gds")])
    result = _run_sync("qleap_chipconstruction_build_block", argv, timeout_s=900, debug=debug)
    result["output"] = str(_chipcon_dir() / "OptimizedModels" / "block_A.gds")
    result["manifest"] = str(_chipcon_dir() / "OptimizedModels" / "jj_manifest.json")
    return result


def qleap_chipconstruction_verify_block(debug: bool = False) -> Dict[str, Any]:
    """P5: final block-level gate. Checks 24 JJ polygons (layer 30, one per
    qubit x 4 letters), manifest completeness and layer conformance, and returns
    the parsed ``{pass, problems, open_items}`` report.

    Read-only, and now genuinely so — it joins ``gds_validity_check`` and
    ``gds_diff`` in taking no ``dry_run``, because gating a check that changes
    nothing only trains people to click through.

    It did NOT used to be. ``block_checker.py`` unconditionally rewrote
    ``OptimizedModels/jj_manifest.json`` before printing its verdict, and that
    file is a sha256-pinned ``jj_manifest`` artifact of the SEALED F3 record, so
    merely running the gate broke every later intake on hash drift — and a
    checker that overwrites its own subject could never fail on drift in the
    first place. The script now COMPARES the published manifest against the
    aggregate of the six per-tile ``jj_geometry.json`` files and reports a
    disagreement as a problem; publishing moved behind ``--write-manifest``,
    which only ``build_block.py`` passes.
    """
    argv = _uv_argv("block_checker.py", ["--with", "gdstk", "--with", "numpy"], [])
    result = _run_sync("qleap_chipconstruction_verify_block", argv, debug=debug)
    result["manifest"] = str(_chipcon_dir() / "OptimizedModels" / "jj_manifest.json")
    return result


def qleap_chipconstruction_tile_chip(
    block_cols: Optional[int] = None,
    block_rows: Optional[int] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """P4 ONLY: multi-block chip tiling with the confirmed vertical stagger
    (even block-columns at baseline y, odd block-columns shifted down by
    half a block's height). Sequenced after P5 passes for the constituent
    block, despite the P4 numbering. Writes ``OptimizedModels/chip.gds``.

    This is the PRE-ALIGNMENT intermediate state (each block's own outer
    perimeter tiles not yet tapered to their neighbors — see P4.5). Prefer
    ``qleap_chipconstruction_build_chip`` for a result with continuous
    couplers across block-to-block seams.
    """
    args = []
    if block_cols is not None:
        args += ["--block-cols", str(block_cols)]
    if block_rows is not None:
        args += ["--block-rows", str(block_rows)]
    argv = _uv_argv("tile_chip.py", ["--with", "gdstk", "--with", "numpy"], args)
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_tile_chip", argv, [str(_chipcon_dir() / "OptimizedModels" / "chip.gds")])
    result = _run_sync("qleap_chipconstruction_tile_chip", argv, timeout_s=1200, debug=debug)
    result["output"] = str(_chipcon_dir() / "OptimizedModels" / "chip.gds")
    return result


def qleap_chipconstruction_build_chip(
    block_cols: Optional[int] = None,
    block_rows: Optional[int] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """**Canonical P4 entry point.** Chains, as one command: ``tile_chip.py``
    (P4) -> ``align_chip_seams.py`` (P4.5: taper every cross-block-instance
    seam's coupler rails to a shared line -- a block's own outer-perimeter
    tiles carry P3.5's raw extension, unaligned, until this step) ->
    ``gds_validity_checker.py``. Requires ``OptimizedModels/block_A.gds`` to
    already be fully stitched (run ``qleap_chipconstruction_build_block``
    first). Fails loudly at the first failing step.
    """
    args = []
    if block_cols is not None:
        args += ["--block-cols", str(block_cols)]
    if block_rows is not None:
        args += ["--block-rows", str(block_rows)]
    argv = _uv_argv("build_chip.py", [], args)
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_build_chip", argv, [str(_chipcon_dir() / "OptimizedModels" / "chip.gds")])
    result = _run_sync("qleap_chipconstruction_build_chip", argv, timeout_s=1800, debug=debug)
    result["output"] = str(_chipcon_dir() / "OptimizedModels" / "chip.gds")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# P7 — generalized hex-lattice chip sizes (foreground, no COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_build_hexlattice(qubits: int, dry_run: bool = True, debug: bool = False,
                                            out_dir: Optional[str] = None) -> Dict[str, Any]:
    """P7: assemble + fully seam-align a direct ``nx_unit x ny_unit`` grid of
    the 6 unit-cell tiles (no "block" grouping, no vertical stagger -- a
    plain periodic grid tiles cleanly with no parity mismatch, unlike the
    3-tile-wide "block"). ``qubits`` must be a multiple of 4 whose
    ``qubits/4`` is a perfect square (16/64/144/256 -> 2x2/4x4/6x6/8x8 unit
    tiles), matching ``resources/qleap_qubit_layout/config/chip_types.json``'s
    own convention. Preview with
    ``tools/render_schematic.py --view hexlattice --qubits N`` (writes
    ``layouts/hexlattice_{qubits}qubit_layout.gds/png``) and get sign-off
    before calling this -- same "show the diagram before burning time"
    practice as P0a/P0b. Self-gates with ``gds_validity_checker.py``;
    fails loudly if it doesn't pass. Writes
    ``OptimizedModels/hexlattice_{qubits}qubit.gds``.

    ``out_dir`` redirects the mask, its seams manifest and its gate reports
    elsewhere INSIDE the campaign (the script's own ``assert_write_allowed``
    refuses anything outside it). Use it to REBUILD a mask for comparison
    without overwriting the sealed original: the F3 record pins each mask by
    sha256, so an in-place rebuild makes every later intake refuse on hash
    drift. Omit it and the default output path is unchanged.
    """
    extra = ["--qubits", str(qubits)]
    if out_dir:
        extra += ["--out-dir", str(out_dir)]
    argv = _uv_argv("build_hexlattice.py", ["--with", "gdstk"], extra)
    written = (str(Path(out_dir) / f"hexlattice_{qubits}qubit.gds") if out_dir
               else str(_chipcon_dir() / "OptimizedModels" / f"hexlattice_{qubits}qubit.gds"))
    if dry_run:
        return _preflight_sync("qleap_chipconstruction_build_hexlattice", argv, [written])
    result = _run_sync("qleap_chipconstruction_build_hexlattice", argv,
                       timeout_s=1800, debug=debug)
    result["output"] = written
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Status (foreground, pure filesystem inspection — mirrors qleap_notch_status)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_chipconstruction_status() -> Dict[str, Any]:
    """Per-tile and per-process gate status across the whole campaign,
    independent of which process is currently being worked on."""
    root = _chipcon_dir()
    out: Dict[str, Any] = {"run_dir": str(root), "tiles": {}, "block": {}, "chip": {}}

    cells_dir = root / "layouts" / "cells"
    for tile in TILES:
        d = root / tile
        work = d / "work"
        st = {
            "raw_gds": (work / f"{tile}_raw.gds").is_file(),
            "layered_gds": (work / f"{tile}_layered.gds").is_file(),
            "jj_pos_json": (root / "Data" / f"{tile}_JJ_pos.json").is_file(),
            "coupler_extend_manifest": (root / "Data" / f"{tile}_coupler_extend_manifest.json").is_file(),
            "render": (d / "figures" / f"{tile}_jj_render.png").is_file(),
            # Console thumbnails (qleap_chipconstruction_render_cell_thumbs).
            # Counted from disk, not compared against a letter list this module
            # does not own -- the scope grammar lives in the design registry.
            "cell_thumb": (cells_dir / f"{tile}.png").is_file(),
            "qubit_thumbs": len(list(cells_dir.glob(f"{tile}_*.png"))),
        }
        out["tiles"][tile] = st

    tile_registry = root / "Data" / "tile_registry.json"
    if tile_registry.is_file():
        try:
            out["tile_registry"] = json.loads(tile_registry.read_text())
        except json.JSONDecodeError:
            out["tile_registry"] = "unreadable"

    block_a = root / "OptimizedModels" / "block_A.gds"
    manifest = root / "OptimizedModels" / "jj_manifest.json"
    out["block"] = {
        "block_A_gds": block_a.is_file(),
        "manifest": manifest.is_file(),
    }
    if manifest.is_file():
        try:
            j = json.loads(manifest.read_text())
            out["block"]["n_manifest_entries"] = len(j) if isinstance(j, dict) else None
        except json.JSONDecodeError:
            out["block"]["manifest"] = "unreadable"

    chip_gds = root / "OptimizedModels" / "chip.gds"
    out["chip"] = {"chip_gds": chip_gds.is_file()}

    out["schematics"] = {
        "block_layout_schematic": (root / "Data" / "block_layout_schematic.gds").is_file(),
        "chip_block_schematic": (root / "Data" / "chip_block_schematic.gds").is_file(),
    }

    out["hexlattice"] = {}
    for qubits in (16, 64, 144, 256):
        gds = root / "OptimizedModels" / f"hexlattice_{qubits}qubit.gds"
        layout_png = root / "layouts" / f"hexlattice_{qubits}qubit_layout.png"
        if gds.is_file() or layout_png.is_file():
            out["hexlattice"][str(qubits)] = {
                "layout_preview": layout_png.is_file(),
                "built": gds.is_file(),
            }

    out["processes_md"] = (root / "tools" / "PROCESSES.md").is_file()
    return out

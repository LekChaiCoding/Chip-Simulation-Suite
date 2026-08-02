"""qleap CS002/F1 tools — MCP wrappers for the F1 capacitance campaign
(``<repo>/simulations/F1_QubitLoop001/`` factory driver plus the
``<repo>/simulations/CapacitanceSimulation002/`` probe/promotion tools).

F1 runs the closed capacitance<->coupling PDCA loop per tile
(``run_cj_loop.py``: intake -> [PLAN -> DO -> CHECK -> ACT] x rounds ->
accept | quarantine), with ``coupling_correct.py`` as the recorded-artifact
ACT step. CS002 supplies the per-qubit direct probe and the
Optimized-Models promotion step.

Same conventions as :mod:`qleap_cct001` / :mod:`qleap_nt2` /
:mod:`qleap_chipconstruction`: thin argv-building wrappers, ``dry_run=True``
default returning the exact command via :func:`_preflight`, background
execution via the shared :class:`JobRegistry` for long/COMSOL runs, and —
critically — never importing the campaign libs (``f1_qubitloop001lib``,
``optimize_qubit``, ``qleapsim``) into the server process: they dynamically
load sibling modules (``_bootstrap``) at import time.

Launch pattern: the campaign tools expect the repo-wide
``env -u VIRTUAL_ENV uv run --no-project [--with mph --with numpy] python
<tool.py> ...`` incantation (see ``simulations/_framework/PLAYBOOK.md``);
``_uv_argv`` below builds exactly that.

Pure-offline steps (``coupling_correct.py`` — seconds, no COMSOL) run
synchronously on ``dry_run=False``, following :mod:`qleap_chipconstruction`'s
``_run_sync`` precedent; everything COMSOL-touching goes through the
JobRegistry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import load_config
from ..jobs import JobRegistry
from ..runner import run_command

TILES = ("U0_R0", "U0_R1", "U1_R0", "U1_R1", "U2_R0", "U2_R1")
LETTERS = "ABCD"

# PDCA rounds before the andon escape hatch (mirrors run_cj_loop.py's
# DEFAULT_ROUNDS — kept as a literal here so the campaign lib is never
# imported into the server process).
DEFAULT_ROUNDS = 3


# ── path helpers ─────────────────────────────────────────────────────────────
def _repo_root() -> Path:
    """The qleap repo, from the config's own marker-based resolution.

    NOT ``chip_sim_root.parent``: that read the containment root as if it were
    the legacy assets dir, so every campaign path landed one level ABOVE the
    checkout — where a status call silently created an empty shadow
    ``simulations/ChipConstruction/`` tree outside the repo.
    """
    return Path(load_config().repo_root)


def _f1_dir() -> Path:
    return _repo_root() / "simulations" / "F1_QubitLoop001"


def _cs002_dir() -> Path:
    return _repo_root() / "simulations" / "CapacitanceSimulation002"


def _tile(tile: str) -> str:
    if tile not in TILES:
        raise ValueError(f"unknown tile {tile!r}; expected one of {TILES}")
    return tile


def _letter(letter: str) -> str:
    if letter not in LETTERS:
        raise ValueError(f"letter must be one of {LETTERS}")
    return letter


#: uv extras for any CS002 script that reaches ``optimize_qubit``.
#:
#: PLAYBOOK.md's launch line is ``--with mph --with numpy``, and every CS002 tool
#: here copied it verbatim — but ``optimize_qubit`` parses the exported touchstone
#: with scikit-rf and then draws the C-matrix figure, which pulls matplotlib and
#: (via models/.../lib/spara_analysis.py) seaborn. So the COMSOL solve ran to
#: completion and the capacitance was computed, and then the tool died on an
#: import THREE times over, one package per attempt, discarding the result each
#: time. ``direct_probe``, ``nm_from_x0`` and ``finalize_optimized_model`` all
#: import optimize_qubit, so all three shared the fault.
#:
#: scikit-rf is pinned: 1.8.0 is the version this campaign's touchstone parsing
#: was validated against.
_OPTIMIZE_QUBIT_EXTRAS: List[str] = [
    "--with", "mph",
    "--with", "numpy",
    "--with", "scikit-rf==1.8.0",
    "--with", "matplotlib",
    "--with", "seaborn",
]


def _uv_argv(script: Path, extras: List[str], args: List[str]) -> List[str]:
    """Repo-standard campaign launch: ``env -u VIRTUAL_ENV uv run
    --no-project [--with ...] python <script> <args>`` (PLAYBOOK.md)."""
    return ["env", "-u", "VIRTUAL_ENV", "uv", "run", "--no-project",
            *extras, "python", str(script), *args]


# ── preflight / launch plumbing (CCT001 pattern) ─────────────────────────────
def _preflight(tool: str, argv: List[str], outputs: List[str]) -> Dict[str, Any]:
    return {
        "dry_run": True,
        "tool": tool,
        "would_run": [str(a) for a in argv],
        "outputs_would_write": outputs,
        "note": ("Validated only. Re-call with dry_run=False to launch as a "
                 "background job (get_job_status / get_job_result to follow)."),
    }


def _launch(registry: JobRegistry, tool: str, argv: List[str], cwd: Path,
            collect_dir: Path, timeout_s: float, debug: bool) -> Dict[str, Any]:
    """Run argv in a DETACHED worker; collect the optimizer's artifacts.

    Detached, not threaded: this server is spawned per MCP client and a daemon
    thread dies with it. A CS002 optimize is a Nelder-Mead loop over ~16 COMSOL
    solves, so this is exactly the length of job that was being lost. See
    :mod:`comsol_suite.job_runner`.
    """
    job = registry.submit_detached(
        tool, argv, cwd=cwd, timeout_s=timeout_s, debug=debug,
        collect_dir=collect_dir,
        collect_patterns=("*.json", "*.csv", "*.s5p", "*.png", "*.mph"))
    return {"job_id": job.job_id, "status": job.status}


def _run_sync(tool: str, argv: List[str], cwd: Path, timeout_s: float = 300,
              debug: bool = False) -> Dict[str, Any]:
    """Run a pure-offline campaign script to completion synchronously
    (no COMSOL, no JobRegistry — :mod:`qleap_chipconstruction`'s
    ``_run_sync`` precedent)."""
    log_path = cwd / "logs" / f"{tool}_last.log"
    res = run_command(argv, log_path=log_path, cwd=cwd,
                      timeout_s=timeout_s, debug=debug)
    return {
        "ok": res.ok,
        "returncode": res.returncode,
        "duration_s": round(res.duration_s, 2),
        "log_tail": res.log_tail(60),
    }


# ─────────────────────────────────────────────────────────────────────────────
# F1 PDCA optimize loop (run_cj_loop.py — per-tile)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_cs002_optimize(
    registry: JobRegistry,
    tile: Optional[str] = None,
    rounds: int = DEFAULT_ROUNDS,
    solve: bool = False,
    j_sim_path: Optional[str] = None,
    print_plan: bool = False,
    replay: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """F1 PDCA capacitance<->coupling loop for one TILE (``run_cj_loop.py``).

    Per-tile by design — the driver's own CLI has no per-letter mode; its DO
    stage runs ``f1_optimize.py`` for all four letters internally, within the
    fail-closed per-qubit solve budget.

    Modes (mutually exclusive; first match wins in the script):
    - ``replay=True``: DO-3 -> CHECK -> ACT math against the REAL CS002-era
      extractions (offline, no writes).
    - ``print_plan=True``: show the full stage plan for the tile (offline).
    - ``solve=True``: actually run the COMSOL DO stages (solve host only).
    - default (all False): CHECK/ACT against ``j_sim_path`` without solving.

    ``j_sim_path``: an existing J-extraction JSON for CHECK-without-solve.
    Intake refuses without a sealed, signed F0 record; C/J targets come from
    the factory record, never from files.
    """
    if tile:
        tile = _tile(tile)
    elif not replay:
        # Mirrors the script's own parser.error: --tile is required except
        # for --replay (which can span all tiles).
        raise ValueError("tile is required except for replay=True")
    if solve and (print_plan or replay):
        raise ValueError("solve is mutually exclusive with print_plan/replay")

    args: List[str] = []
    if tile:
        args += ["--tile", tile]
    args += ["--rounds", str(rounds)]
    if print_plan:
        args += ["--print-plan"]
    if replay:
        args += ["--replay"]
    if solve:
        args += ["--solve"]
    if j_sim_path is not None:
        args += ["--j-sim", str(j_sim_path)]

    # PLAYBOOK incantation: mph only needed when actually solving; numpy is
    # cheap insurance for the offline math paths.
    extras = (["--with", "mph", "--with", "numpy"] if solve
              else ["--with", "numpy"])
    argv = _uv_argv(_f1_dir() / "tools" / "run_cj_loop.py", extras, args)

    data_dir = _f1_dir() / "Data"
    if dry_run:
        return _preflight("qleap_cs002_optimize", argv, [str(data_dir)])
    # Solve loops are long (up to 12 solves/qubit/round x 4 letters x rounds);
    # offline modes finish in minutes but share the same background path so
    # every real action goes through the one launch chokepoint.
    timeout_s = rounds * 4 * 12 * 5400 if solve else 3600
    return _launch(registry, "qleap_cs002_optimize", argv, cwd=_repo_root(),
                   collect_dir=data_dir, timeout_s=timeout_s, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# CS002 direct probe (direct_probe.py — fixed parameter sets, COMSOL)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_cs002_direct_probe(
    registry: JobRegistry,
    tile: str,
    letter: str,
    run_id: str,
    params: Union[List[Dict[str, float]], Dict[str, float]],
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run a fixed list of parameter sets through the CS002 qubit simulator
    (``direct_probe.py`` — targeted probes where the NM optimizer failed to
    explore a region, or boundary-extension probes).

    - ``run_id``: history namespace; results append to
      ``<tile>/<letter>/Data/<run_id>_history.json`` (resume-safe).
    - ``params``: one dict or a list of dicts of CS002 geometry parameters,
      e.g. ``{"qubit_pad_r": 108.0, "qubit_readout_pad_angle": 59.2}``.
      Serialized to the script's ``--params-json``.

    COMSOL-touching: each parameter set is a capacitance solve.
    """
    tile = _tile(tile)
    letter = _letter(letter)
    if not run_id:
        raise ValueError("run_id must be a non-empty history namespace tag")
    if isinstance(params, dict):
        params = [params]
    if not params:
        raise ValueError("params must contain at least one parameter dict")

    argv = _uv_argv(
        _cs002_dir() / "tools" / "direct_probe.py",
        _OPTIMIZE_QUBIT_EXTRAS,
        ["--unit", tile, "--letter", letter, "--run-id", run_id,
         "--params-json", json.dumps(params)],
    )

    data_dir = _cs002_dir() / tile / letter / "Data"
    if dry_run:
        return _preflight("qleap_cs002_direct_probe", argv,
                          [str(data_dir / f"{run_id}_history.json")])
    return _launch(registry, "qleap_cs002_direct_probe", argv,
                   cwd=_repo_root(), collect_dir=data_dir,
                   timeout_s=len(params) * 5400, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# F1 coupling correction (coupling_correct.py — recorded artifact, offline)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_cs002_coupling_correct(
    registry: JobRegistry,
    tile: Optional[str] = None,
    j_sim_path: Optional[str] = None,
    brickwall: bool = False,
    out_path: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """First-order C<->J correction as a RECORDED ARTIFACT
    (``coupling_correct.py``): ``C_new = sign(C_old)*|C_old|*sqrt(J_target/
    J_sim)``, applied to C_qc entries only. Emits a corrected-targets JSON
    with full provenance (formula, input hashes, per-edge ratios/statuses);
    never mutates models. Edges outside the correction ratio band are
    refused (``correction_refused``), not extrapolated.

    - ``j_sim_path`` (required): the J-extraction JSON to correct against.
    - ``brickwall=True``: parse ``j_sim_path`` as a raw brickwall-estimator
      output instead of the loop's extraction format.
    - ``tile``: restrict correction to one tile's edges.
    - ``out_path``: where the correction artifact JSON is written
      (script default otherwise).

    Offline (pure math, seconds): ``dry_run=False`` runs synchronously —
    no JobRegistry needed (``registry`` accepted for signature uniformity).
    """
    if tile is not None:
        tile = _tile(tile)
    if j_sim_path is None:
        return {"ok": False,
                "error": "j_sim_path is required (the script's --j-sim flag "
                         "is mandatory) — point it at the J-extraction JSON "
                         "to correct against"}

    args = ["--j-sim", str(j_sim_path)]
    if brickwall:
        args += ["--brickwall"]
    if tile is not None:
        args += ["--tile", tile]
    if out_path is not None:
        args += ["--out", str(out_path)]

    argv = _uv_argv(_f1_dir() / "tools" / "coupling_correct.py",
                    ["--with", "numpy"], args)

    if dry_run:
        outputs = [out_path] if out_path else ["<script-default correction JSON>"]
        return _preflight("qleap_cs002_coupling_correct", argv, outputs)
    return _run_sync("qleap_cs002_coupling_correct", argv, cwd=_repo_root(),
                     timeout_s=300, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# CS002 promotion (finalize_optimized_model.py — HITL-gated real action)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_cs002_finalize(
    registry: JobRegistry,
    tile: str,
    letter: str,
    record_path: str,
    label: str,
    allow_out_of_tolerance: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Promote an accepted qubit candidate into
    ``simulations/CapacitanceSimulation002/Optimized Models/<TILE>/<TILE>_<L>/``
    (``finalize_optimized_model.py``).

    Reloads the letter-specific optimized base model, applies the candidate
    parameters from ``record_path`` (the candidate JSON record), saves a
    handoff ``.mph``, and copies the paired record, Touchstone, and figure
    into the standardized folder. ``label`` names the handoff artifacts.

    ``allow_out_of_tolerance``: skip the +/-0.2 fF capacitance-tolerance
    refusal (use only with an explicit acceptance rationale).

    COMSOL-touching (model reload + save) AND a promotion — the HITL
    approval gate fires on ``dry_run=False`` as with every real action.
    """
    tile = _tile(tile)
    letter = _letter(letter)
    if not label:
        raise ValueError("label must be a non-empty artifact tag")

    args = ["--unit", tile, "--letter", letter,
            "--record", str(record_path), "--label", label]
    if allow_out_of_tolerance:
        args += ["--allow-out-of-tolerance"]

    argv = _uv_argv(_cs002_dir() / "tools" / "finalize_optimized_model.py",
                    _OPTIMIZE_QUBIT_EXTRAS, args)

    optimized_dir = (_cs002_dir() / "Optimized Models" / tile
                     / f"{tile}_{letter}")
    if dry_run:
        return _preflight("qleap_cs002_finalize", argv, [str(optimized_dir)])
    return _launch(registry, "qleap_cs002_finalize", argv, cwd=_repo_root(),
                   collect_dir=optimized_dir, timeout_s=3600, debug=debug)

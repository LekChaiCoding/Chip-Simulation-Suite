"""qleap CCT001 tools — MCP wrappers for the cable-coupling tuning campaign
in ``<repo>/simulations/CableCouplingTuning001/``.

CCT001 tunes each qubit's cable (drive-line) decay rate gamma/2pi to 500 Hz
by sweeping the back-spoke width (and, when needed, the spoke count) on the
QCS001 cable-activated single-qubit models. Same conventions as :mod:`qleap`
and :mod:`qleap_nt2`: thin argv-building wrappers, ``dry_run=True`` default,
background jobs via the shared :class:`JobRegistry`, never import ``cctlib``
into the server process (it dynamically loads sibling modules at import
time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import run_command

TILES = ("U0_R0", "U0_R1", "U1_R0", "U1_R1", "U2_R0", "U2_R1")
LETTERS = "ABCD"


def _repo_root() -> Path:
    return Path(load_config().chip_sim_root).parent


def _cct_dir() -> Path:
    return _repo_root() / "simulations" / "CableCouplingTuning001"


def _qcs_dir() -> Path:
    return _repo_root() / "simulations" / "QubitCableSimulation001"


def _python_bin() -> str:
    return str(load_config().python_bin)


def _tile(tile: str) -> str:
    if tile not in TILES:
        raise ValueError(f"unknown tile {tile!r}; expected one of {TILES}")
    return tile


def _letter(letter: str) -> str:
    if letter not in LETTERS:
        raise ValueError(f"letter must be one of {LETTERS}")
    return letter


def _validate_width_bounds(width_bounds_um: Optional[List[float]]) -> Optional[List[float]]:
    if width_bounds_um is None:
        return None
    if len(width_bounds_um) != 2 or not (float(width_bounds_um[0]) < float(width_bounds_um[1])):
        raise ValueError("width_bounds_um must be [lo, hi] with lo < hi")
    return width_bounds_um


def _qubit_dir(tile: str, letter: str) -> Path:
    return _cct_dir() / tile / f"{tile}_{letter}"


def _pristine_model(tile: str, letter: str) -> Path:
    return _qubit_dir(tile, letter) / "work" / "pristine" / f"{tile}-{letter}_Cable.mph"


def _qcs001_source_model(tile: str, letter: str) -> Path:
    return _qcs_dir() / tile / f"{tile}_{letter}" / f"{tile}-{letter}_Cable.mph"


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
    def worker(job: Job) -> Dict[str, Any]:
        res = run_command(argv, log_path=Path(job.log_path), cwd=cwd,
                          timeout_s=timeout_s, debug=debug)
        files: List[str] = []
        if collect_dir.is_dir():
            for pat in ("*.csv", "*.json", "*.png"):
                files.extend(str(p) for p in collect_dir.rglob(pat))
        return {
            "ok": res.ok,
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "output_files": sorted(set(files)),
            "log_tail": res.log_tail(30),
            "summary": f"{tool} finished rc={res.returncode}",
            "error": None if res.ok else f"{tool} failed (see run.log)",
        }

    job = registry.submit(tool, worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


def qleap_cct001_tune_width(
    registry: JobRegistry,
    tile: str,
    letter: str,
    cores: int = 8,
    max_trials: int = 8,
    spoke_count: int = 8,
    seed_width_um: Optional[float] = None,
    width_bounds_um: Optional[List[float]] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Log-log secant sweep of back-spoke width to hit gamma/2pi = 500 Hz +/-5%.

    ``spoke_count`` != 8 (the shipped default) runs in its own trial/state
    namespace. ``width_bounds_um`` overrides the default width ceiling — used
    for the integer-ladder recovery pass on qubits unreachable at the default
    bounds.

    Prerequisite: the pristine cable-activated model must already exist
    (``work/pristine/{tile}-{letter}_Cable.mph``, copied from the QCS001
    deliverable).
    """
    tile = _tile(tile)
    letter = _letter(letter)
    width_bounds_um = _validate_width_bounds(width_bounds_um)

    pristine = _pristine_model(tile, letter)
    if not pristine.is_file():
        return {"ok": False,
                "error": f"pristine copy missing: {pristine} — copy it from "
                         f"the QCS001 deliverable first (md5-verify against "
                         f"the original), or run qleap_cct001_rollout_letter"}

    cct = _cct_dir()
    argv = [_python_bin(), str(cct / "tools" / "tune_width.py"),
            "--tile", tile, "--letter", letter, "--cores", str(cores),
            "--max-trials", str(max_trials), "--spoke-count", str(spoke_count)]
    if seed_width_um is not None:
        argv += ["--seed-width", str(seed_width_um)]
    if width_bounds_um:
        argv += ["--width-bounds-um", str(width_bounds_um[0]), str(width_bounds_um[1])]

    data_dir = _qubit_dir(tile, letter) / "Data"
    if dry_run:
        return _preflight("qleap_cct001_tune_width", argv, [str(data_dir)])
    return _launch(registry, "qleap_cct001_tune_width", argv, cwd=cct,
                   collect_dir=data_dir, timeout_s=max_trials * 5400, debug=debug)


def qleap_cct001_rollout_letter(
    registry: JobRegistry,
    tile: str,
    letter: str,
    cores: int = 8,
    force_n: Optional[int] = None,
    width_bounds_um: Optional[List[float]] = None,
    max_trials: Optional[int] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """End-to-end CCT001 rollout for one qubit: ensure pristine copy -> width
    campaign (with n+/-1 spoke-count fallback unless ``force_n`` is set) ->
    fine verify -> broad-sweep straight-line gate.

    ``force_n``: skip the n+/-1 fallback and pin the spoke count.
    ``width_bounds_um``: recovery-pass width ceiling override.

    Prerequisite: the QCS001 cable-activated source model must exist
    (the script's own ``ensure_pristine()`` copies it in; this wrapper checks
    it up front for a fast, clear error rather than a subprocess failure).
    """
    tile = _tile(tile)
    letter = _letter(letter)
    width_bounds_um = _validate_width_bounds(width_bounds_um)

    source = _qcs001_source_model(tile, letter)
    if not source.is_file():
        return {"ok": False, "error": f"QCS001 source model missing: {source}"}

    cct = _cct_dir()
    argv = [_python_bin(), str(cct / "tools" / "rollout_letter.py"),
            "--tile", tile, "--letter", letter, "--cores", str(cores)]
    if force_n is not None:
        argv += ["--force-n", str(force_n)]
    if width_bounds_um:
        argv += ["--width-bounds-um", str(width_bounds_um[0]), str(width_bounds_um[1])]
    if max_trials is not None:
        argv += ["--max-trials", str(max_trials)]

    data_dir = _qubit_dir(tile, letter) / "Data"
    if dry_run:
        return _preflight("qleap_cct001_rollout_letter", argv, [str(data_dir)])
    return _launch(registry, "qleap_cct001_rollout_letter", argv, cwd=cct,
                   collect_dir=data_dir, timeout_s=24 * 3600, debug=debug)

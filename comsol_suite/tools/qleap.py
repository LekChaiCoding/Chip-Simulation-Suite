"""qleap chip-simulation tools — MCP wrappers for the tile pipelines.

These tools expose the qleap_circuit_design per-tile simulation pipelines
(which live OUTSIDE this suite, under ``<repo>/simulations/``) through the
MCP server, following the suite's conventions:

* thin orchestrator — every tool subprocesses the validated pipeline scripts
  (never reimplements their physics/geometry logic);
* solver-launching tools default to ``dry_run=True`` (validate + report the
  exact argv; nothing runs);
* real runs go through the :class:`JobRegistry` as background jobs
  (``get_job_status`` / ``get_job_result`` to follow them).

Pipelines wrapped
-----------------
NDS001 — ``simulations/NotchDecaySimulation001``: JJ-port frequency-domain
S-parameter sweeps -> H21 = S21/(1+S11) -> kappa(omega), Purcell-notch
position/depth and T1 per qubit. Scripts: ``run_tile_pipeline.sh`` (full
tile: copy -> inspect -> prepare -> raster verify -> renders -> coarse ->
fine -> extract), ``run_sparam.py`` (one sweep), ``extract_decay.py``.

RCS001 — ``simulations/ReadoutCouplingSimulation001``: eigenfrequency runs
(JJ inductors -> qubit freqs; JJ current ports -> g_QR).

Path resolution: the qleap repo root is ``chip_sim_root.parent`` (the suite
is vendored at ``<repo>/resources/COMSOL Simulation Suite`` and
``chip_sim_root`` resolves to ``<repo>/resources``).

The subprocesses run on the suite's configured ``python_bin`` (the
chip_sim_suite venv: mph, numpy, scipy, matplotlib — everything the
pipeline scripts need), NOT the repo's broken ``.venv``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import run_command

LETTERS = "ABCD"
TILES = ["U0_R0", "U0_R1", "U1_R0", "U1_R1", "U2_R0", "U2_R1"]


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────
def _repo_root() -> Path:
    return Path(load_config().chip_sim_root).parent


def _nds_dir() -> Path:
    return _repo_root() / "simulations" / "NotchDecaySimulation001"


def _rcs_dir() -> Path:
    return _repo_root() / "simulations" / "ReadoutCouplingSimulation001"


def _python_bin() -> str:
    return str(load_config().python_bin)


def _tile(unit: str, row: str) -> str:
    tile = f"{unit}_{row}"
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


def _launch(registry: JobRegistry, tool: str, argv: List[str], cwd: Path,
            collect_dir: Path, timeout_s: float, debug: bool) -> Dict[str, Any]:
    """Submit argv as a background job; collect CSV/JSON/PNG outputs."""

    def worker(job: Job) -> Dict[str, Any]:
        res = run_command(argv, log_path=Path(job.log_path), cwd=cwd,
                          timeout_s=timeout_s, debug=debug)
        files: List[str] = []
        for pat in ("*.csv", "*.json", "*.png"):
            files.extend(str(p) for p in collect_dir.rglob(pat))
        return {
            "ok": res.ok,
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "output_files": sorted(set(files)),
            "log_tail": res.log_tail(25),
            "summary": f"{tool} finished rc={res.returncode}",
            "error": None if res.ok else f"{tool} failed (see run.log)",
        }

    job = registry.submit(tool, worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# Status (read-only, foreground)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_notch_status(unit: Optional[str] = None,
                       row: Optional[str] = None) -> Dict[str, Any]:
    """Per-tile progress of the NDS001 notch pipeline + results if present."""
    nds = _nds_dir()
    tiles = [_tile(unit, row)] if unit and row else TILES
    out: Dict[str, Any] = {"run_dir": str(nds), "tiles": {}}
    for tile in tiles:
        d = nds / tile
        data = d / "Data"
        st: Dict[str, Any] = {
            "stitched_copy": (d / "work" / f"{tile}_sparam_copy.mph").is_file(),
            "prepared_base": (d / "work" / f"{tile}_sparam_base.mph").is_file(),
            "coarse_csvs": sorted(p.name for p in
                                  data.glob(f"{tile}_?_sparam_coarse.csv")),
            "fine_csvs": sorted(p.name for p in
                                data.glob(f"{tile}_?_sparam_fine.csv")),
        }
        mv = data / f"{tile}_sparam_base_metal_verify.json"
        if mv.is_file():
            try:
                j = json.loads(mv.read_text())
                checks = j.get("checks", j)
                st["metal_verify"] = ("PASS" if str(checks).find("fail") < 0
                                      else "SEE JSON")
            except Exception:
                st["metal_verify"] = "unreadable"
        summ = data / f"{tile}_notch_summary.json"
        if summ.is_file():
            try:
                res = json.loads(summ.read_text())
                st["notch_summary"] = {
                    L: {"f_q_GHz": r["f_q_GHz"],
                        "f_notch_GHz": r["f_notch_GHz"],
                        "notch_minus_fq_MHz": r["notch_minus_fq_MHz"],
                        "kappa_fq_over_2pi_MHz": r["kappa_fq_over_2pi_MHz"],
                        "T1_us": r["T1_us"],
                        "notch_depth_dB": r["notch_depth_dB"]}
                    for L, r in res.get("results", {}).items()}
                st["problems"] = res.get("problems", [])
            except Exception:
                st["notch_summary"] = "unreadable"
        out["tiles"][tile] = st
    return out


# ─────────────────────────────────────────────────────────────────────────────
# NDS001 — full tile pipeline
# ─────────────────────────────────────────────────────────────────────────────
def qleap_run_notch_pipeline(
    registry: JobRegistry,
    unit: str,
    row: str,
    letters: str = "ABCD",
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Full NDS001 pipeline for one tile (resume-safe; existing artifacts are
    skipped): copy -> inspect (coax survey gate) -> prepare (selection
    verification gate) -> metal raster gate -> presence renders -> per-letter
    coarse sweeps -> fine windows -> fine sweeps -> notch extraction."""
    tile = _tile(unit, row)
    if not set(letters) <= set(LETTERS):
        raise ValueError(f"letters must be a subset of {LETTERS}")
    nds = _nds_dir()
    script = nds / "tools" / "run_tile_pipeline.sh"
    argv = ["bash", str(script), unit, row, letters]
    outputs = [str(nds / tile / "Data"), str(nds / tile / "figures")]
    if dry_run:
        return _preflight("qleap_run_notch_pipeline", argv, outputs)
    return _launch(registry, "qleap_run_notch_pipeline", argv, cwd=nds,
                   collect_dir=nds / tile / "Data",
                   timeout_s=12 * 3600, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# NDS001 — one S-parameter sweep
# ─────────────────────────────────────────────────────────────────────────────
def qleap_run_notch_sweep(
    registry: JobRegistry,
    unit: str,
    row: str,
    letter: str,
    pass_name: str = "coarse",
    flist: Optional[str] = None,
    others: str = "inductor",
    reciprocity: bool = False,
    cores: int = 8,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """One JJ-port S-parameter sweep (NDS001 run_sparam.py) on a prepared
    tile. ``flist`` is a COMSOL range/list expression (defaults: the coarse
    grid, or the extracted fine windows for pass_name='fine')."""
    tile = _tile(unit, row)
    if letter not in LETTERS:
        raise ValueError(f"letter must be one of {LETTERS}")
    if pass_name not in ("coarse", "fine"):
        raise ValueError("pass_name must be 'coarse' or 'fine'")
    nds = _nds_dir()
    base = nds / tile / "work" / f"{tile}_sparam_base.mph"
    if not base.is_file():
        return {"ok": False,
                "error": f"prepared model missing: {base} — run "
                         f"qleap_run_notch_pipeline first (it prepares + "
                         f"gates the tile)"}
    argv = [_python_bin(), str(nds / "tools" / "run_sparam.py"),
            "--unit", unit, "--row", row, "--letter", letter,
            "--pass", pass_name, "--others", others, "--cores", str(cores)]
    if flist:
        argv += ["--flist", flist]
    if reciprocity:
        argv += ["--reciprocity"]
    outputs = [str(nds / tile / "Data" /
                   f"{tile}_{letter}_sparam_{pass_name}.csv")]
    if dry_run:
        return _preflight("qleap_run_notch_sweep", argv, outputs)
    return _launch(registry, "qleap_run_notch_sweep", argv, cwd=nds,
                   collect_dir=nds / tile / "Data",
                   timeout_s=4 * 3600, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# NDS001 — extraction (fast, foreground)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_extract_notch(unit: str, row: str,
                        stage: str = "final") -> Dict[str, Any]:
    """Post-process NDS001 sweep CSVs (extract_decay.py): stage='window'
    writes the fine flists; stage='final' writes the notch summary + figure.
    Fast (seconds) — runs in the foreground and returns the summary."""
    tile = _tile(unit, row)
    if stage not in ("window", "final"):
        raise ValueError("stage must be 'window' or 'final'")
    nds = _nds_dir()
    argv = [_python_bin(), str(nds / "tools" / "extract_decay.py"),
            "--unit", unit, "--row", row, "--stage", stage]
    log = nds / tile / "Data" / "logs" / f"mcp_extract_{stage}.log"
    res = run_command(argv, log_path=log, cwd=nds, timeout_s=600)
    out: Dict[str, Any] = {"ok": res.ok, "returncode": res.returncode,
                           "log_tail": res.log_tail(30)}
    summ = nds / tile / "Data" / f"{tile}_notch_summary.json"
    if stage == "final" and summ.is_file():
        out["notch_summary"] = json.loads(summ.read_text())
        out["figure"] = str(nds / tile / "figures" /
                            f"{tile}_kappa_vs_freq.png")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# RCS001 — eigenfrequency / g_QR runs
# ─────────────────────────────────────────────────────────────────────────────
def qleap_run_eigen_gqr(
    registry: JobRegistry,
    unit: str,
    row: str,
    run: int,
    neigs: Optional[int] = None,
    shift: Optional[str] = None,
    cores: int = 8,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """RCS001 eigenfrequency solve on a prepared gQR base model.
    run=1: JJ inductors active -> qubit frequencies.
    run=2: JJ current ports active -> g_QR per readout mode
    (requires run 1 + qleap_extract_gqr stage='qubit-freqs' first)."""
    tile = _tile(unit, row)
    if run not in (1, 2):
        raise ValueError("run must be 1 or 2")
    rcs = _rcs_dir()
    base = rcs / tile / "work" / f"{tile}_gqr_base.mph"
    if not base.is_file():
        return {"ok": False, "error": f"prepared model missing: {base}"}
    argv = [_python_bin(), str(rcs / "tools" / "run_eigen.py"),
            "--unit", unit, "--row", row, "--run", str(run),
            "--cores", str(cores)]
    if neigs:
        argv += ["--neigs", str(neigs)]
    if shift:
        argv += ["--shift", shift]
    if dry_run:
        return _preflight("qleap_run_eigen_gqr", argv,
                          [str(rcs / tile / "Data")])
    return _launch(registry, "qleap_run_eigen_gqr", argv, cwd=rcs,
                   collect_dir=rcs / tile / "Data",
                   timeout_s=6 * 3600, debug=debug)


def qleap_extract_gqr(unit: str, row: str,
                      stage: str = "final") -> Dict[str, Any]:
    """Post-process RCS001 eigen CSVs (extract_gqr.py): stage='qubit-freqs'
    after run 1, stage='final' after run 2. Fast, foreground."""
    tile = _tile(unit, row)
    if stage not in ("qubit-freqs", "final"):
        raise ValueError("stage must be 'qubit-freqs' or 'final'")
    rcs = _rcs_dir()
    argv = [_python_bin(), str(rcs / "tools" / "extract_gqr.py"),
            "--unit", unit, "--row", row, "--stage", stage]
    log = rcs / tile / "Data" / "logs" / f"mcp_extract_{stage}.log"
    res = run_command(argv, log_path=log, cwd=rcs, timeout_s=600)
    out: Dict[str, Any] = {"ok": res.ok, "returncode": res.returncode,
                           "log_tail": res.log_tail(30)}
    for name in (f"{tile}_qubit_freqs.json", f"{tile}_gqr_summary.json"):
        p = rcs / tile / "Data" / name
        if p.is_file():
            out[name] = json.loads(p.read_text())
    return out

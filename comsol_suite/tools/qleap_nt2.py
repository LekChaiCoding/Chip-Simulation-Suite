"""qleap NT002 campaign-driver tools — MCP wrappers for the *current*
notch-retuning campaign in ``<repo>/simulations/NotchTuning002/``.

These sit one tier above the low-level per-probe tool already exposed in
:mod:`qleap` (``qleap_run_nt2_probe``, which wraps the superseded NT002A
``run_a_probe.py``): this module wraps the campaign drivers that plan and
execute a full letter retune (NT002C ``linear_retune.py``, NT002D
``ratio_retune.py`` + its geometry gates), and the merge/verify/publish
pipeline that turns four accepted per-letter models into one published tile.

Conventions, same as :mod:`qleap`:

* thin orchestrator — every tool subprocesses the validated campaign scripts
  (never reimplements their route/gate/physics logic);
* **never import** ``nt2lib.py`` into the long-lived MCP server process — it
  has import-time side effects (reads target/config JSON at import, raises if
  absent). Config JSON files are read directly here (inert data, not code)
  for prerequisite checks only;
* solver-launching tools default to ``dry_run=True``; real runs go through
  the shared :class:`JobRegistry` as background jobs;
* ``plan_only=True`` on the retune drivers routes to the script's own
  ``--dry-run`` flag, which is confirmed (by reading ``ratio_retune.py`` and
  ``linear_retune.py``) to return before any COMSOL subprocess is spawned and
  before any record file is written — safe to run synchronously in the
  foreground;
* three of these tools use a non-zero return code to carry a legitimate
  PASS/FAIL *verdict*, not a crash signal — ``ratio_geometry_gate.py`` (rc 2 =
  FAIL), ``run_ratio_trade_probe.py`` (rc 3 = completed but not verified). Both
  are treated as ``ok=True`` here (the tool ran to completion); the verdict is
  surfaced separately by parsing the script's own JSON report.
  ``ratio_gap_check.py`` always exits 0 and encodes its verdict only in JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import run_command

TILES = ("U0_R0", "U0_R1", "U1_R0", "U1_R1", "U2_R0", "U2_R1")
LETTERS = "ABCD"
_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


# ─────────────────────────────────────────────────────────────────────────────
# Path / validation helpers
# ─────────────────────────────────────────────────────────────────────────────
def _repo_root() -> Path:
    return Path(load_config().chip_sim_root).parent


def _nt2_dir() -> Path:
    return _repo_root() / "simulations" / "NotchTuning002"


def _nt1_dir() -> Path:
    return _repo_root() / "simulations" / "NotchTuning001"


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


def _letters_subset(letters: str) -> str:
    if not letters or any(ch not in LETTERS for ch in letters):
        raise ValueError(f"letters must be a non-empty subset of {LETTERS}")
    return letters


def _sanitize_tag(tag: str) -> str:
    if not tag or any(ch not in _TAG_CHARS for ch in tag):
        raise ValueError("tag may contain only letters, numbers, '_' and '-'")
    return tag


def _assert_inside_nt2(path: Path) -> Path:
    nt2 = _nt2_dir().resolve()
    p = path.resolve()
    if p != nt2 and nt2 not in p.parents:
        raise ValueError(f"path must be inside NotchTuning002: {path}")
    return p


def _parse_trailing_json(text: str) -> Optional[Any]:
    """Best-effort extraction of the final ``json.dumps(..., indent=N)`` blob
    these scripts print. Every script here logs progress through a
    timestamp-prefixed ``log()`` helper and prints exactly one top-level JSON
    object/array at the very end, so the last line that is exactly ``{`` or
    ``[`` (no leading whitespace — nested values never start a line that way)
    marks the start of that blob."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] in ("{", "["):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    return None


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
            collect_dir: Path, timeout_s: float, debug: bool,
            extra_files: Optional[List[Path]] = None,
            ok_returncodes: tuple = (0,)) -> Dict[str, Any]:
    """Submit argv as a background job; collect CSV/JSON/PNG outputs.

    ``ok_returncodes`` lists return codes that mean "ran to completion" —
    some of these gates use a non-zero exit code to carry a verdict rather
    than signal a crash (see module docstring).
    """
    def worker(job: Job) -> Dict[str, Any]:
        res = run_command(argv, log_path=Path(job.log_path), cwd=cwd,
                          timeout_s=timeout_s, debug=debug)
        files: List[str] = []
        if collect_dir.is_dir():
            for pat in ("*.csv", "*.json", "*.png"):
                files.extend(str(p) for p in collect_dir.rglob(pat))
        for f in (extra_files or []):
            if f.is_file():
                files.append(str(f))
        ok = (res.returncode in ok_returncodes) and not res.timed_out
        return {
            "ok": ok,
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "output_files": sorted(set(files)),
            "log_tail": res.log_tail(30),
            "summary": f"{tool} finished rc={res.returncode}",
            "error": None if ok else f"{tool} failed (see run.log)",
        }

    job = registry.submit(tool, worker, background=True)
    return {"job_id": job.job_id, "status": job.status}


# ─────────────────────────────────────────────────────────────────────────────
# NT002C — linear one-shot retune
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_linear_retune(
    registry: JobRegistry,
    tile: str,
    letter: str,
    force: bool = False,
    plan_only: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """NT002C one-shot linear notch retune for one (tile, letter).

    ``plan_only=True`` runs the script's own ``--dry-run`` synchronously —
    plans the knob move, touches no COMSOL, writes nothing. Otherwise a real
    run does one inductor-mode verification solve as a background job.
    """
    tile = _tile(tile)
    letter = _letter(letter)
    nt2 = _nt2_dir()
    script = nt2 / "tools" / "linear_retune.py"
    argv = [_python_bin(), str(script), "--tile", tile, "--letter", letter]
    if force:
        argv.append("--force")
    out_path = nt2 / "overnight" / f"{tile}_{letter}.LINEAR.json"

    if plan_only:
        log = nt2 / "overnight" / "logs" / f"mcp_linear_plan_{tile}_{letter}.log"
        res = run_command(argv + ["--dry-run"], log_path=log, cwd=nt2,
                          timeout_s=120, debug=debug)
        return {"ok": res.ok, "returncode": res.returncode,
                "plan": _parse_trailing_json(res.log_tail(500)),
                "log_tail": res.log_tail(30)}

    if dry_run:
        return _preflight("qleap_nt2_linear_retune", argv, [str(out_path)])
    return _launch(registry, "qleap_nt2_linear_retune", argv, cwd=nt2,
                   collect_dir=nt2 / tile / letter / "Data",
                   extra_files=[out_path], timeout_s=5400, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# Purcell T1 gate (foreground, pure post-processing)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_purcell_check(
    tile: str,
    letters: str = "ABCD",
    csv_override: Optional[str] = None,
    no_plot: bool = False,
    record_suffix: str = "LINEAR",
    debug: bool = False,
) -> Dict[str, Any]:
    """kappa(f_q) -> Purcell T1 gate from existing linear/ratio probe CSVs.

    Fast (seconds), pure post-processing — runs in the foreground.
    ``record_suffix`` selects which retune record to read (``"LINEAR"`` or
    ``"RATIO"``). ``csv_override`` (a single sweep CSV) requires exactly one
    letter, mirroring the script's own check.
    """
    tile = _tile(tile)
    letters = _letters_subset(letters)
    if record_suffix not in ("LINEAR", "RATIO"):
        raise ValueError("record_suffix must be 'LINEAR' or 'RATIO'")
    if csv_override and len(letters) != 1:
        raise ValueError("csv_override requires exactly one letter")

    nt2 = _nt2_dir()
    missing = [str(nt2 / "overnight" / f"{tile}_{L}.{record_suffix}.json")
               for L in letters
               if not (nt2 / "overnight" / f"{tile}_{L}.{record_suffix}.json").is_file()]
    if missing:
        return {"ok": False,
                "error": f"missing {record_suffix} record(s): {missing} — run "
                         f"qleap_nt2_linear_retune / qleap_nt2_ratio_retune first"}

    argv = [_python_bin(), str(nt2 / "tools" / "purcell_check.py"),
            "--tile", tile, "--letters", letters,
            "--record-suffix", record_suffix]
    if csv_override:
        argv += ["--csv", csv_override]
    if no_plot:
        argv.append("--no-plot")

    log = nt2 / "overnight" / "logs" / f"mcp_purcell_check_{tile}.log"
    res = run_command(argv, log_path=log, cwd=nt2, timeout_s=300, debug=debug)
    return {"ok": res.ok, "returncode": res.returncode,
            "rows": _parse_trailing_json(res.log_tail(1000)),
            "log_tail": res.log_tail(30)}


# ─────────────────────────────────────────────────────────────────────────────
# NT002D — ratio-trade (meander/straight arm-length) retune
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_ratio_retune(
    registry: JobRegistry,
    tile: str,
    letter: str,
    force: bool = False,
    plan_only: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """NT002D ratio-trade retune driver for one (tile, letter): resume-safe,
    budgeted route walk (map probe -> seed -> secant/bisect -> verification).

    ``plan_only=True`` runs the script's own ``--dry-run`` synchronously
    (confirmed to stop before any solve is spawned).
    """
    tile = _tile(tile)
    letter = _letter(letter)
    nt2 = _nt2_dir()
    cfg_path = nt2 / "campaign_config_nt002d.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"ok": False, "error": f"cannot read {cfg_path}: {exc}"}
    if letter not in cfg.get("strategy", {}):
        return {"ok": False, "error": f"no NT002D strategy for letter {letter}"}

    script = nt2 / "tools" / "ratio_retune.py"
    argv = [_python_bin(), str(script), "--tile", tile, "--letter", letter]
    if force:
        argv.append("--force")
    out_path = nt2 / "overnight" / f"{tile}_{letter}.RATIO.json"

    if plan_only:
        log = nt2 / "overnight" / "logs" / f"mcp_ratio_plan_{tile}_{letter}.log"
        res = run_command(argv + ["--dry-run"], log_path=log, cwd=nt2,
                          timeout_s=120, debug=debug)
        return {"ok": res.ok, "returncode": res.returncode,
                "plan": _parse_trailing_json(res.log_tail(500)),
                "log_tail": res.log_tail(30)}

    if dry_run:
        return _preflight("qleap_nt2_ratio_retune", argv, [str(out_path)])
    return _launch(registry, "qleap_nt2_ratio_retune", argv, cwd=nt2,
                   collect_dir=nt2 / tile / letter / "Data",
                   extra_files=[out_path], timeout_s=4 * 3600, debug=debug)


# ─────────────────────────────────────────────────────────────────────────────
# Ratio-trade gates (foreground, need a live COMSOL session, no solve)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_ratio_gap_check(
    tile: str,
    letter_model: str,
    param_overrides: Dict[str, str],
    letters: str = "ABCD",
    min_gap_um: float = 10.0,
    window_um: float = 800.0,
    tag: str = "candidate",
    cores: int = 4,
    no_render: bool = False,
    debug: bool = False,
) -> Dict[str, Any]:
    """10 um clearance gate for a ratio-trade candidate.

    Geometry-only (no solve) but needs a live COMSOL session to rebuild and
    trace the candidate geometry — runs in the foreground.

    ``ratio_gap_check.py`` always exits 0; the real verdict lives in the
    JSON report (``result["report"]["verdict"]``), not the return code.
    """
    tile = _tile(tile)
    letter_model = _letter(letter_model)
    letters = _letters_subset(letters)
    tag = _sanitize_tag(tag)
    nt2 = _nt2_dir()
    argv = [_python_bin(), str(nt2 / "tools" / "ratio_gap_check.py"),
            "--tile", tile, "--letter-model", letter_model,
            "--letters", letters, "--min-gap", str(min_gap_um),
            "--window-um", str(window_um), "--tag", tag,
            "--cores", str(cores)]
    for name, expr in sorted(param_overrides.items()):
        argv += ["--set", f"{name}={expr}"]
    if no_render:
        argv.append("--no-render")

    fig_dir = nt2 / tile / "Data" / "analysis" / "gap_checks"
    report_path = fig_dir / f"{tag}_report.json"
    log = fig_dir / f"mcp_{tag}.log"
    res = run_command(argv, log_path=log, cwd=nt2, timeout_s=600, debug=debug)
    report = _parse_trailing_json(res.log_tail(2000))
    if report is None and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    return {"ok": res.ok, "returncode": res.returncode, "report": report,
            "report_path": str(report_path) if report_path.is_file() else None,
            "log_tail": res.log_tail(30)}


def qleap_nt2_ratio_geometry_gate(
    tile: str,
    letter_model: str,
    param_overrides: Dict[str, str],
    letters: str = "ABCD",
    tag: str = "candidate",
    cores: int = 4,
    area_tol_um2: float = 2.0,
    length_tol_um: float = 0.5,
    no_render: bool = False,
    debug: bool = False,
) -> Dict[str, Any]:
    """Topology-aware conductor/corridor conservation gate for a ratio-trade
    candidate. Foreground; needs a live COMSOL session, no solve.

    Return code 2 is a legitimate FAIL verdict (not a crash) — this wrapper
    treats ``{0, 2}`` as "ran to completion" and surfaces the real verdict
    from the parsed JSON report.
    """
    tile = _tile(tile)
    letter_model = _letter(letter_model)
    letters = _letters_subset(letters)
    tag = _sanitize_tag(tag)
    nt2 = _nt2_dir()
    argv = [_python_bin(), str(nt2 / "tools" / "ratio_geometry_gate.py"),
            "--tile", tile, "--letter-model", letter_model,
            "--letters", letters, "--tag", tag, "--cores", str(cores),
            "--area-tol-um2", str(area_tol_um2),
            "--length-tol-um", str(length_tol_um)]
    for name, expr in sorted(param_overrides.items()):
        argv += ["--set", f"{name}={expr}"]
    if no_render:
        argv.append("--no-render")

    out_dir = nt2 / tile / "Data" / "analysis" / "geometry_gates"
    report_path = out_dir / f"{tag}_report.json"
    log = out_dir / f"mcp_{tag}.log"
    res = run_command(argv, log_path=log, cwd=nt2, timeout_s=600, debug=debug)
    ok = res.returncode in (0, 2) and not res.timed_out
    report = _parse_trailing_json(res.log_tail(2000))
    if report is None and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    return {"ok": ok, "returncode": res.returncode,
            "verdict": (report or {}).get("verdict"), "report": report,
            "report_path": str(report_path) if report_path.is_file() else None,
            "log_tail": res.log_tail(30)}


# ─────────────────────────────────────────────────────────────────────────────
# Gated ratio-trade probe (gate + probe together; only saves on PASS)
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_run_ratio_trade_probe(
    registry: JobRegistry,
    tile: str,
    letter: str,
    tag: str,
    center_ghz: float,
    param_overrides: Dict[str, str],
    others: str = "inductor",
    half_mhz: float = 250.0,
    step_mhz: float = 10.0,
    cores: int = 8,
    save_model: Optional[str] = None,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Gated ratio-trade probe: runs ``ratio_geometry_gate.py`` on the exact
    candidate first (COMSOL not solved, no model saved unless it PASSes),
    then the notch probe.

    Return code 3 means the probe completed but ``result["verified"]`` is
    False (not a crash) — surfaced via ``result["verified"]`` rather than
    ``ok``. Any other non-zero code is a real failure, most commonly the
    internal geometry gate FAILing and propagating as an uncaught
    ``CalledProcessError`` (rc 1) — check ``log_tail`` in that case.
    """
    tile = _tile(tile)
    letter = _letter(letter)
    tag = _sanitize_tag(tag)
    if not param_overrides:
        raise ValueError("ratio probe requires at least one param override")
    if others not in ("open", "inductor"):
        raise ValueError("others must be 'open' or 'inductor'")

    nt2 = _nt2_dir()
    argv = [_python_bin(), str(nt2 / "tools" / "run_ratio_trade_probe.py"),
            "--tile", tile, "--letter", letter, "--tag", tag,
            "--others", others, "--center-ghz", str(center_ghz),
            "--half-mhz", str(half_mhz), "--step-mhz", str(step_mhz),
            "--cores", str(cores)]
    for name, expr in sorted(param_overrides.items()):
        argv += ["--set", f"{name}={expr}"]

    destination: Optional[Path] = None
    if save_model:
        destination = Path(save_model)
        if not destination.is_absolute():
            destination = nt2 / destination
        destination = _assert_inside_nt2(destination)
        argv += ["--save-model", str(destination)]

    round_dir = nt2 / tile / letter / "Data" / "rounds" / tag
    outputs = [str(round_dir / f"{tag}_gated_result.json"),
               str(round_dir / f"{tag}_probe_summary.json")]
    if destination:
        outputs.append(str(destination))

    if dry_run:
        return _preflight("qleap_nt2_run_ratio_trade_probe", argv, outputs)
    return _launch(registry, "qleap_nt2_run_ratio_trade_probe", argv, cwd=nt2,
                   collect_dir=round_dir,
                   extra_files=[destination] if destination else None,
                   timeout_s=4 * 3600, debug=debug, ok_returncodes=(0, 3))


# ─────────────────────────────────────────────────────────────────────────────
# Merge / verify / publish
# ─────────────────────────────────────────────────────────────────────────────
def qleap_nt2_build_merged_model(
    registry: JobRegistry,
    tile: str,
    with_notch_finals: bool = False,
    output_path: Optional[str] = None,
    cores: int = 4,
    plan_only: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Build the per-tile merged S-parameter model from accepted per-letter
    knobs (``--with-notch-finals`` also applies the letters' accepted NT002
    filter knobs from ``overnight/*.LINEAR.json``/``*.RATIO.json``).

    ``plan_only=True`` runs the script's own ``--dry-run``, which computes
    and prints the real knob-provenance report without opening COMSOL
    (confirmed: ``import mph`` happens strictly after the dry-run return) —
    safe to run synchronously in the foreground.
    """
    tile = _tile(tile)
    nt1 = _nt1_dir()
    source = nt1 / tile / "work" / f"{tile}_sparam_psqtop.mph"
    if not source.is_file():
        return {"ok": False, "error": f"NT001 psqtop source not found: {source}"}

    nt2 = _nt2_dir()
    argv = [_python_bin(), str(nt2 / "tools" / "build_merged_sparam_model.py"),
            "--tile", tile, "--cores", str(cores)]
    if with_notch_finals:
        argv.append("--with-notch-finals")

    destination: Optional[Path] = None
    if output_path:
        destination = Path(output_path)
        if not destination.is_absolute():
            destination = nt2 / destination
        destination = _assert_inside_nt2(destination)
        argv += ["--output", str(destination)]
    else:
        destination = (nt2 / tile / "work" / f"{tile}_notch_merged_final.mph"
                       if with_notch_finals else None)

    if plan_only:
        log = nt2 / tile / "Data" / "analysis" / f"mcp_merge_plan_{tile}.log"
        res = run_command(argv + ["--dry-run"], log_path=log, cwd=nt2,
                          timeout_s=300, debug=debug)
        return {"ok": res.ok, "returncode": res.returncode,
                "plan": _parse_trailing_json(res.log_tail(2000)),
                "log_tail": res.log_tail(30)}

    outputs = [str(destination)] if destination else []
    if dry_run:
        return _preflight("qleap_nt2_build_merged_model", argv, outputs)
    return _launch(registry, "qleap_nt2_build_merged_model", argv, cwd=nt2,
                   collect_dir=nt2 / tile / "Data" / "analysis",
                   extra_files=[destination] if destination else None,
                   timeout_s=3600, debug=debug)


def qleap_nt2_verify_merged_notches(
    registry: JobRegistry,
    tile: str,
    model_path: Optional[str] = None,
    cores: int = 8,
    reanalyze: bool = False,
    skip_fr: bool = False,
    dry_run: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Final merged-context acceptance gate: re-probes each letter's notch
    (and, unless ``skip_fr``, the dressed readout band) on the merged model
    and checks Purcell T1 + notch offset per letter.

    Near-instant when ``reanalyze=True`` or all sweep CSVs already exist;
    otherwise up to 4 fresh probe solves — still routed through the job
    registry for consistency.
    """
    tile = _tile(tile)
    nt2 = _nt2_dir()
    model = (Path(model_path) if model_path
             else nt2 / tile / "work" / f"{tile}_notch_merged_final.mph")
    if not model.is_file():
        return {"ok": False,
                "error": f"merged model missing: {model} — run "
                         f"qleap_nt2_build_merged_model(with_notch_finals=True) first"}

    argv = [_python_bin(), str(nt2 / "tools" / "verify_merged_notches.py"),
            "--tile", tile, "--cores", str(cores)]
    if model_path:
        argv += ["--model", str(model)]
    if reanalyze:
        argv.append("--reanalyze")
    if skip_fr:
        argv.append("--skip-fr")

    out_path = nt2 / tile / "Data" / "analysis" / "merged_notch_verification.json"
    if dry_run:
        return _preflight("qleap_nt2_verify_merged_notches", argv, [str(out_path)])
    return _launch(registry, "qleap_nt2_verify_merged_notches", argv, cwd=nt2,
                   collect_dir=nt2 / tile / "Data" / "merged_verification",
                   extra_files=[out_path], timeout_s=4 * 4 * 5400, debug=debug)


def qleap_nt2_publish_optimized(tile: str, debug: bool = False) -> Dict[str, Any]:
    """Publish an accepted merged tile model to
    ``simulations/OptimizedModels/{tile}/`` (mph + knob manifest + README +
    figures, sha256-stamped). Foreground: file I/O + hashing, no COMSOL.

    Unlike every other tool in this module, this one writes **outside**
    NotchTuning002 by design (a publish step) — but there is no
    user-controlled path argument (only the whitelisted ``tile``), so there
    is no path-injection surface. The script itself refuses to overwrite an
    already-published tile.
    """
    tile = _tile(tile)
    nt2 = _nt2_dir()
    model = nt2 / tile / "work" / f"{tile}_notch_merged_final.mph"
    verified = nt2 / tile / "Data" / "analysis" / "merged_notch_verification.json"
    build = nt2 / tile / "Data" / "analysis" / "notch_merged_build.json"
    missing = [str(p) for p in (model, verified, build) if not p.is_file()]
    if missing:
        return {"ok": False,
                "error": f"prerequisite(s) missing: {missing} — run "
                         f"qleap_nt2_build_merged_model / "
                         f"qleap_nt2_verify_merged_notches first"}

    argv = [_python_bin(), str(nt2 / "tools" / "publish_optimized.py"),
            "--tile", tile]
    log = nt2 / tile / "Data" / "analysis" / f"mcp_publish_{tile}.log"
    res = run_command(argv, log_path=log, cwd=nt2, timeout_s=600, debug=debug)
    return {"ok": res.ok, "returncode": res.returncode,
            "result": _parse_trailing_json(res.log_tail(500)),
            "log_tail": res.log_tail(30)}

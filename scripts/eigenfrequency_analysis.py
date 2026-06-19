#!/usr/bin/env python3
"""Eigenfrequency study for the AI Simulation Stack.

Finds resonance frequencies and Q-factors without a frequency sweep.
Run this FIRST for any new device to locate resonances quickly (~5 min).

Usage:
  eigenfrequency_analysis.py [--modes N] [--freq-start GHZ] [--freq-stop GHZ]
                              [--out CSV] [--cores N] [--debug]

Inputs:
  BASE_MPH — a built (not necessarily solved) .mph from build_comsol_model or
             run_custom_comsol_build. Must have EMW physics with PEC boundaries.

Outputs:
  CSV_OUT                              — eigenfrequencies.csv
                                         columns: mode, freq_ghz, Q_factor, loss_rate_mhz
  <OUT_DIR>/eigenfrequency_result.mph  — solved model for GUI inspection

Key assumptions:
  - BASE_MPH has EMW physics defined (physics tag "emw").
  - Metals are modeled as PEC for superconducting simulations.
  - Eigenvalue solver uses GHz units to avoid rad/s confusion (eigunit=GHz).
  - n_modes eigenvalues within [freq_start_ghz, freq_stop_ghz] GHz exist;
    the script aborts with [GATE-2 FAIL] if fewer are found.

Debug flags:
  --debug    print extra COMSOL API calls and intermediate values
"""

import argparse
import csv
import math
import os
import sys
import time
from pathlib import Path

# ─── Module-level path constants (patched by run_eigenfrequency_study MCP tool) ──
ROOT               = "/mnt/smb/HSS/users/Alex/Chip Simulation"
BASE_MPH           = f"{ROOT}/python_outputs/recreation_solved.mph"
OUT_DIR            = f"{ROOT}/python_outputs"
CSV_OUT            = f"{ROOT}/python_outputs/eigenfrequencies.csv"

DEFAULT_N_MODES    = 5
DEFAULT_FREQ_START = 1.0    # GHz — search window lower bound
DEFAULT_FREQ_STOP  = 20.0   # GHz — search window upper bound

STUDY_TAG   = "stdEig"   # COMSOL study tag for the eigenfrequency study
STUDY_LABEL = "stdEig"   # mph.solve() resolves by LABEL, not tag


# ─── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str, debug: bool = False, force: bool = False) -> None:
    """Timestamped log flushed immediately for remote monitoring."""
    if force or debug:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gate_fail(msg: str) -> None:
    """Print a [GATE-2 FAIL] message and exit non-zero."""
    print(f"[GATE-2 FAIL] {msg}", flush=True)
    sys.exit(1)


# ─── Model loading ───────────────────────────────────────────────────────────────

def load_model(mph_path: str, comsol_cores: int, debug: bool):
    """Start mph client and load the model. Gate: file must exist."""
    if not Path(mph_path).is_file():
        gate_fail(f"BASE_MPH not found: {mph_path}")

    import mph
    log(f"Starting mph client (cores={comsol_cores}) ...", force=True)
    client = mph.start(cores=comsol_cores)
    log(f"Loading model: {mph_path}", force=True)
    t0 = time.time()
    pymodel = client.load(mph_path)
    log(f"Model loaded in {time.time() - t0:.1f} s", force=True)
    if debug:
        log(f"DEBUG: model tags = {[str(t) for t in pymodel / 'studies']}")
    return client, pymodel


# ─── Study creation ──────────────────────────────────────────────────────────────

def add_eigenfrequency_study(
    pymodel, n_modes: int, freq_start_ghz: float, freq_stop_ghz: float, debug: bool
) -> None:
    """Add an eigenfrequency study to the model.

    Sets eigunit=GHz so returned eigenvalues are in GHz, not rad/s.
    Shift point is set to the midpoint of the search window.
    """
    m = pymodel.java

    m.study().create(STUDY_TAG)
    m.study(STUDY_TAG).label(STUDY_LABEL)

    m.study(STUDY_TAG).create("eig", "Eigenfrequency")
    feat = m.study(STUDY_TAG).feature("eig")

    feat.set("neigsactive", "on")       # activate n-eigenvalue mode
    feat.set("neigs", str(n_modes))
    feat.set("eigunit", "GHz")          # CRITICAL: avoid rad/s unit confusion

    # Shift point: search near the midpoint of the window for best convergence.
    shift_ghz = (freq_start_ghz + freq_stop_ghz) / 2.0
    feat.set("shift", f"{shift_ghz}[GHz]")

    if debug:
        log(f"DEBUG: study {STUDY_TAG}: n_modes={n_modes}, "
            f"shift={shift_ghz:.2f} GHz, unit=GHz")


# ─── Solve ───────────────────────────────────────────────────────────────────────

def solve_eigenfrequency(pymodel, debug: bool) -> None:
    """Solve the eigenfrequency study. Times the solve for logging."""
    log("Solving eigenfrequency study ...", force=True)
    t0 = time.time()
    pymodel.solve(STUDY_LABEL)   # mph resolves study by LABEL
    elapsed = time.time() - t0
    log(f"Solve done in {elapsed:.0f} s ({elapsed/60:.1f} min)", force=True)


# ─── Extraction ──────────────────────────────────────────────────────────────────

def extract_eigenfrequencies(
    pymodel, n_modes: int, freq_start_ghz: float, freq_stop_ghz: float, debug: bool
) -> list:
    """Extract complex eigenvalues → (freq_ghz, Q_factor, loss_rate_mhz).

    COMSOL eigenvalue λ = f_r + i*f_i where both are in GHz (eigunit=GHz).
    Physical interpretation:
      f_resonance = Re(λ)          [GHz]
      Q            = Re(λ) / (2*|Im(λ)|)
      loss_rate    = Im(λ) * 2π * 1e3  [MHz]

    Returns list of dicts, one per mode, sorted by frequency.
    Gates: NaN, out-of-window, Q≤0, wrong mode count.
    """
    # Discover the solution dataset created by the eigenfrequency solve.
    all_ds = [d.name() for d in pymodel / "datasets"]
    if debug:
        log(f"DEBUG: all datasets: {all_ds}")
    ds = all_ds[-1] if all_ds else None

    # Extract real and imaginary parts of the eigenfrequency (in GHz, eigunit=GHz).
    eig_re = [float(v) for v in pymodel.evaluate("real(freq)", "GHz", dataset=ds)]
    eig_im = [float(v) for v in pymodel.evaluate("imag(freq)", "GHz", dataset=ds)]

    if debug:
        log(f"DEBUG: raw eigenvalues (GHz): re={eig_re} im={eig_im}")

    results = []
    for i, (fr, fi) in enumerate(zip(eig_re, eig_im)):
        mode_num = i + 1

        # Gate: NaN eigenfrequency
        if math.isnan(fr) or math.isnan(fi):
            gate_fail(f"Mode {mode_num}: NaN eigenfrequency — solver may have diverged")

        # Gate: spurious mode outside the search window
        if fr < freq_start_ghz:
            log(f"WARNING: Mode {mode_num}: freq {fr:.4f} GHz below search window "
                f"({freq_start_ghz} GHz) — skipping (spurious mode)")
            continue
        if fr > freq_stop_ghz:
            log(f"WARNING: Mode {mode_num}: freq {fr:.4f} GHz above search window "
                f"({freq_stop_ghz} GHz) — skipping (spurious mode)")
            continue

        # Gate: non-physical Q factor
        if abs(fi) < 1e-20:
            gate_fail(f"Mode {mode_num}: Im(freq) ≈ 0 — cannot compute Q factor")
        q_factor = fr / (2.0 * abs(fi))
        if q_factor <= 0:
            gate_fail(f"Mode {mode_num}: Q ≤ 0 ({q_factor:.3g}) — unphysical result")

        loss_rate_mhz = abs(fi) * 2.0 * math.pi * 1e3   # GHz → MHz

        results.append({
            "mode": mode_num,
            "freq_ghz": round(fr, 6),
            "Q_factor": round(q_factor, 1),
            "loss_rate_mhz": round(loss_rate_mhz, 4),
        })
        log(f"  Mode {mode_num}: f={fr:.4f} GHz  Q={q_factor:.0f}  "
            f"loss={loss_rate_mhz:.3f} MHz", force=True)

    # Gate: expected number of physical modes
    if len(results) < n_modes:
        gate_fail(f"Expected {n_modes} modes in [{freq_start_ghz}, {freq_stop_ghz}] GHz, "
                  f"got {len(results)} — widen the search window or reduce n_modes")

    return sorted(results, key=lambda r: r["freq_ghz"])


# ─── Output ──────────────────────────────────────────────────────────────────────

def write_csv(results: list, csv_out: str, debug: bool) -> None:
    """Write eigenfrequency results to CSV. Gate: file must not be empty."""
    Path(csv_out).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["mode", "freq_ghz", "Q_factor",
                                                 "loss_rate_mhz"])
        writer.writeheader()
        writer.writerows(results)

    size = Path(csv_out).stat().st_size
    if size < 10:
        gate_fail(f"CSV is empty after write: {csv_out} ({size} bytes)")
    log(f"CSV written: {csv_out} ({size} bytes, {len(results)} modes)", force=True)
    if debug:
        log(f"DEBUG: CSV contents preview:")
        with open(csv_out) as fh:
            for line in fh:
                log(f"  {line.rstrip()}")


def save_interim_mph(pymodel, out_dir: str, debug: bool) -> str:
    """Save the solved model as eigenfrequency_result.mph for GUI inspection."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    mph_path = str(Path(out_dir) / "eigenfrequency_result.mph")
    log(f"Saving solved model: {mph_path}", force=True)
    pymodel.save(mph_path)
    size_mb = Path(mph_path).stat().st_size / 1e6
    log(f"Saved {size_mb:.1f} MB: {mph_path}", force=True)
    return mph_path


# ─── Arg parsing ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="COMSOL eigenfrequency study — find resonances and Q-factors."
    )
    parser.add_argument(
        "--modes", type=int, default=DEFAULT_N_MODES,
        help=f"Number of eigenvalues to find (default: {DEFAULT_N_MODES})",
    )
    parser.add_argument(
        "--freq-start", type=float, default=DEFAULT_FREQ_START, dest="freq_start",
        help=f"Search window lower bound in GHz (default: {DEFAULT_FREQ_START})",
    )
    parser.add_argument(
        "--freq-stop", type=float, default=DEFAULT_FREQ_STOP, dest="freq_stop",
        help=f"Search window upper bound in GHz (default: {DEFAULT_FREQ_STOP})",
    )
    parser.add_argument(
        "--out", type=str, default=CSV_OUT,
        help="Output CSV path (default: CSV_OUT constant)",
    )
    parser.add_argument(
        "--cores", type=int, default=4,
        help="Number of COMSOL solver threads (default: 4)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print extra COMSOL API details",
    )
    return parser.parse_args()


def validate_inputs(args: argparse.Namespace) -> None:
    """Validate parsed arguments before connecting to COMSOL."""
    if not (1 <= args.modes <= 20):
        print(f"[GATE-1 FAIL] --modes must be 1–20, got {args.modes}", flush=True)
        sys.exit(1)
    if args.freq_start >= args.freq_stop:
        print(f"[GATE-1 FAIL] --freq-start ({args.freq_start}) must be < "
              f"--freq-stop ({args.freq_stop})", flush=True)
        sys.exit(1)
    if args.cores < 1:
        print(f"[GATE-1 FAIL] --cores must be ≥ 1, got {args.cores}", flush=True)
        sys.exit(1)


# ─── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Full eigenfrequency pipeline: load → study → solve → extract → write."""
    args = parse_args()
    validate_inputs(args)

    log("=== eigenfrequency_analysis.py ===", force=True)
    log(f"BASE_MPH    : {BASE_MPH}", force=True)
    log(f"OUT_DIR     : {OUT_DIR}", force=True)
    log(f"n_modes     : {args.modes}", force=True)
    log(f"freq window : [{args.freq_start}, {args.freq_stop}] GHz", force=True)
    log(f"output CSV  : {args.out}", force=True)

    client, pymodel = load_model(BASE_MPH, args.cores, args.debug)
    try:
        add_eigenfrequency_study(
            pymodel, args.modes, args.freq_start, args.freq_stop, args.debug
        )
        solve_eigenfrequency(pymodel, args.debug)
        results = extract_eigenfrequencies(
            pymodel, args.modes, args.freq_start, args.freq_stop, args.debug
        )
        write_csv(results, args.out, args.debug)
        save_interim_mph(pymodel, OUT_DIR, args.debug)
    finally:
        # Always remove the model from memory, even on failure.
        try:
            client.remove(pymodel)
        except Exception:
            pass

    log("=== eigenfrequency_analysis.py DONE ===", force=True)


if __name__ == "__main__":
    main()

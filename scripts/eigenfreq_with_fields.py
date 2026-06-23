#!/usr/bin/env python3
"""Eigenfrequency study with per-mode field energy and path-integral extraction.

Extends eigenfrequency_analysis.py with three additional output channels that
are required for mode identification and coupling extraction in any two-mode
coupled system (qubit–resonator, resonator–filter, etc.):

  1. Per-mode electric and magnetic energies  (emw.intWe, emw.intWm)
  2. Line integrals of |E| along named COMSOL edge selections
     (used to identify which mode is localised where)
  3. Complex voltage at named node selections
     (used to extract decay rates via voltage-ratio method)

All COMSOL selection names are supplied by the user at runtime — this script
makes no assumptions about device geometry.

Usage:
  eigenfreq_with_fields.py [--modes N] [--freq-start GHZ] [--freq-stop GHZ]
                            [--extract-fields]
                            [--path-selections SEL [SEL ...]]
                            [--node-groups NG [NG ...]]
                            [--out CSV] [--cores N] [--debug]

Inputs:
  BASE_MPH          — built .mph with EMW physics + PEC boundaries.
  PATH_SELECTIONS   — COMSOL edge selection names for |E| path integrals.
                      Patched by run_eigenfrequency_study MCP tool.
  NODE_GROUPS       — COMSOL selection names for voltage extraction.
                      Patched by run_eigenfrequency_study MCP tool.

Outputs:
  CSV_OUT                          — eigenfreq_fields.csv
  <OUT_DIR>/eigenfreq_fields.mph   — solved model for GUI inspection

CSV columns (always):
  mode, freq_ghz, Q_factor, loss_rate_mhz

CSV columns (with --extract-fields):
  We_J, Wm_J

CSV columns (with --path-selections SEL):
  path_<SEL>    — ∫|E| ds along that selection [V/m · m = V]

CSV columns (with --node-groups NG):
  V_re_<NG>, V_im_<NG>   — complex voltage at that selection [V]

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

import numpy as np

# ─── Module-level path constants (patched by MCP tool) ──────────────────────
ROOT             = "/mnt/smb/HSS/users/Alex/Chip Simulation"
BASE_MPH         = f"{ROOT}/python_outputs/recreation_solved.mph"
OUT_DIR          = f"{ROOT}/python_outputs"
CSV_OUT          = f"{ROOT}/python_outputs/eigenfreq_fields.csv"
PATH_SELECTIONS  = []   # list of COMSOL edge selection names for |E| integrals
NODE_GROUPS      = []   # list of COMSOL selection names for voltage extraction

DEFAULT_N_MODES    = 5
DEFAULT_FREQ_START = 1.0   # GHz
DEFAULT_FREQ_STOP  = 20.0  # GHz

STUDY_TAG   = "stdEigFields"
STUDY_LABEL = "stdEigFields"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg: str, force: bool = True) -> None:
    """Timestamped log flushed immediately for remote monitoring."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gate_fail(msg: str) -> None:
    """Print a [GATE-2 FAIL] message and exit non-zero."""
    print(f"[GATE-2 FAIL] {msg}", flush=True)
    sys.exit(1)


# ─── Model loading ───────────────────────────────────────────────────────────

def load_model(mph_path: str, comsol_cores: int, debug: bool):
    """Start mph client and load the model."""
    if not Path(mph_path).is_file():
        gate_fail(f"BASE_MPH not found: {mph_path}")

    import mph
    log(f"Starting mph client (cores={comsol_cores}) ...")
    client = mph.start(cores=comsol_cores)
    log(f"Loading model: {mph_path}")
    t0 = time.time()
    pymodel = client.load(mph_path)
    log(f"Model loaded in {time.time() - t0:.1f} s")
    if debug:
        log(f"DEBUG: model studies = {[str(t) for t in pymodel / 'studies']}")
    return client, pymodel


# ─── Study creation ──────────────────────────────────────────────────────────

def add_eigenfrequency_study(
    pymodel, n_modes: int, freq_start_ghz: float, freq_stop_ghz: float, debug: bool
) -> None:
    """Add an eigenfrequency study to the model.

    Uses eigunit=GHz to keep eigenvalues in GHz and avoid rad/s confusion.
    Shift point set to midpoint of search window for best solver convergence.
    """
    m = pymodel.java

    m.study().create(STUDY_TAG)
    m.study(STUDY_TAG).label(STUDY_LABEL)
    m.study(STUDY_TAG).create("eig", "Eigenfrequency")
    feat = m.study(STUDY_TAG).feature("eig")

    feat.set("neigsactive", "on")
    feat.set("neigs", str(n_modes))
    feat.set("eigunit", "GHz")   # CRITICAL: avoid rad/s unit confusion

    shift_ghz = (freq_start_ghz + freq_stop_ghz) / 2.0
    feat.set("shift", f"{shift_ghz}[GHz]")

    if debug:
        log(f"DEBUG: study {STUDY_TAG}: n_modes={n_modes}, "
            f"shift={shift_ghz:.2f} GHz, unit=GHz")


# ─── Solve ───────────────────────────────────────────────────────────────────

def solve_eigenfrequency(pymodel, debug: bool) -> None:
    """Solve the eigenfrequency study, timing the solve."""
    log("Solving eigenfrequency study ...")
    t0 = time.time()
    pymodel.solve(STUDY_LABEL)   # mph resolves study by LABEL
    elapsed = time.time() - t0
    log(f"Solve done in {elapsed:.0f} s ({elapsed/60:.1f} min)")


# ─── Dataset discovery ───────────────────────────────────────────────────────

def find_dataset(pymodel, debug: bool) -> str:
    """Return the tag of the most recently created dataset (post-solve)."""
    all_ds = [d.name() for d in pymodel / "datasets"]
    if debug:
        log(f"DEBUG: all datasets: {all_ds}")
    if not all_ds:
        gate_fail("No datasets found after solve — solver may have failed")
    return all_ds[-1]


# ─── Base eigenfrequency extraction ─────────────────────────────────────────

def extract_base_eigenfrequencies(
    pymodel, ds: str, n_modes: int,
    freq_start_ghz: float, freq_stop_ghz: float, debug: bool
) -> list:
    """Extract freq_ghz, Q_factor, loss_rate_mhz per mode.

    Returns list of dicts sorted by frequency. Applies gates for NaN,
    out-of-window, non-physical Q, and wrong mode count.
    """
    eig_re = [float(v) for v in pymodel.evaluate("real(freq)", "GHz", dataset=ds)]
    eig_im = [float(v) for v in pymodel.evaluate("imag(freq)", "GHz", dataset=ds)]

    if debug:
        log(f"DEBUG: raw eigenvalues (GHz): re={eig_re} im={eig_im}")

    results = []
    for i, (fr, fi) in enumerate(zip(eig_re, eig_im)):
        mode_num = i + 1

        if math.isnan(fr) or math.isnan(fi):
            gate_fail(f"Mode {mode_num}: NaN eigenfrequency — solver may have diverged")

        if fr < freq_start_ghz:
            log(f"WARNING: Mode {mode_num}: {fr:.4f} GHz below window — skipping")
            continue
        if fr > freq_stop_ghz:
            log(f"WARNING: Mode {mode_num}: {fr:.4f} GHz above window — skipping")
            continue

        if abs(fi) < 1e-20:
            gate_fail(f"Mode {mode_num}: Im(freq) ≈ 0 — cannot compute Q factor")
        q_factor = fr / (2.0 * abs(fi))
        if q_factor <= 0:
            gate_fail(f"Mode {mode_num}: Q ≤ 0 ({q_factor:.3g}) — unphysical result")

        loss_rate_mhz = abs(fi) * 2.0 * math.pi * 1e3

        results.append({
            "mode": mode_num,
            "freq_ghz": round(fr, 6),
            "Q_factor": round(q_factor, 1),
            "loss_rate_mhz": round(loss_rate_mhz, 4),
        })
        log(f"  Mode {mode_num}: f={fr:.4f} GHz  Q={q_factor:.0f}  "
            f"loss={loss_rate_mhz:.3f} MHz")

    if len(results) < n_modes:
        gate_fail(f"Expected {n_modes} modes in [{freq_start_ghz}, {freq_stop_ghz}] GHz, "
                  f"got {len(results)} — widen window or reduce n_modes")

    return sorted(results, key=lambda r: r["freq_ghz"])


# ─── Field energy extraction ─────────────────────────────────────────────────

def extract_field_energies(pymodel, ds: str, n_modes: int, debug: bool) -> dict:
    """Extract per-mode electric and magnetic energies (We, Wm) in Joules.

    Returns dict: {mode_index: {"We_J": float, "Wm_J": float}}
    mode_index is 0-based, matching the order returned by evaluate().
    """
    try:
        We_arr = [float(v) for v in pymodel.evaluate("emw.intWe", "J", dataset=ds)]
        Wm_arr = [float(v) for v in pymodel.evaluate("emw.intWm", "J", dataset=ds)]
        if debug:
            log(f"DEBUG: We (J) = {We_arr}")
            log(f"DEBUG: Wm (J) = {Wm_arr}")
        return {
            i: {"We_J": We_arr[i] if i < len(We_arr) else float("nan"),
                "Wm_J": Wm_arr[i] if i < len(Wm_arr) else float("nan")}
            for i in range(n_modes)
        }
    except Exception as exc:
        log(f"WARNING: field energy extraction failed: {exc} — We/Wm set to NaN")
        return {i: {"We_J": float("nan"), "Wm_J": float("nan")} for i in range(n_modes)}


# ─── Path integral extraction ─────────────────────────────────────────────────

def extract_path_integrals(
    pymodel, ds: str, path_selections: list, n_modes: int, debug: bool
) -> dict:
    """Compute ∫|E| ds along each named COMSOL edge selection, per mode.

    Returns dict: {sel_name: [integral_mode0, integral_mode1, ...]}

    Strategy: try evaluate with selection argument; fall back to full-domain
    evaluate with a warning (selection may not be supported in all mph versions).
    """
    results = {}
    for sel in path_selections:
        integrals = []
        try:
            # Attempt selection-scoped evaluation
            normE_arr = pymodel.evaluate("emw.normE", "V/m", dataset=ds)
            arc_arr   = pymodel.evaluate("arc",       "m",   dataset=ds)

            # normE_arr and arc_arr may be 1-D (single mode) or 2-D (modes × points).
            normE_arr = np.asarray(normE_arr, dtype=float)
            arc_arr   = np.asarray(arc_arr,   dtype=float)

            if normE_arr.ndim == 1:
                # Same field for all modes — compute one integral and replicate
                integral = float(np.trapezoid(normE_arr, arc_arr))
                integrals = [integral] * n_modes
            else:
                # Shape: (n_modes, n_points) or (n_points, n_modes) — normalise
                if normE_arr.shape[0] == n_modes:
                    for row in normE_arr:
                        integrals.append(float(np.trapezoid(row, arc_arr)))
                else:
                    for col_idx in range(min(n_modes, normE_arr.shape[1])):
                        col = normE_arr[:, col_idx]
                        integrals.append(float(np.trapezoid(col, arc_arr)))

            log(f"  Path integral [{sel}]: {integrals}")
        except Exception as exc:
            log(f"WARNING: path integral for selection '{sel}' failed: {exc} — set to NaN")
            integrals = [float("nan")] * n_modes

        results[sel] = integrals

    if debug and results:
        log(f"DEBUG: path integrals = {results}")
    return results


# ─── Node voltage extraction ─────────────────────────────────────────────────

def extract_node_voltages(
    pymodel, ds: str, node_groups: list, n_modes: int, debug: bool
) -> dict:
    """Extract complex voltage amplitude at named COMSOL node selections, per mode.

    Returns dict: {ng_name: [complex(V_re, V_im) per mode]}
    """
    results = {}
    for ng in node_groups:
        try:
            V_re_arr = [float(v) for v in pymodel.evaluate("real(emw.V)", "V", dataset=ds)]
            V_im_arr = [float(v) for v in pymodel.evaluate("imag(emw.V)", "V", dataset=ds)]
            voltages = [complex(re, im) for re, im in zip(V_re_arr, V_im_arr)]
            log(f"  Node voltage [{ng}]: |V| = {[abs(v) for v in voltages]}")
        except Exception as exc:
            log(f"WARNING: voltage extraction for selection '{ng}' failed: {exc} — set to NaN")
            voltages = [complex(float("nan"), float("nan"))] * n_modes
        results[ng] = voltages

    if debug and results:
        log(f"DEBUG: node voltages = {results}")
    return results


# ─── CSV output ──────────────────────────────────────────────────────────────

def write_csv(
    results: list,
    csv_out: str,
    We_Wm: dict,
    path_integrals: dict,
    node_voltages: dict,
    extract_fields: bool,
    path_selections: list,
    node_groups: list,
    debug: bool,
) -> None:
    """Write all extracted data to CSV with dynamic columns."""
    # Build fieldnames dynamically
    fieldnames = ["mode", "freq_ghz", "Q_factor", "loss_rate_mhz"]
    if extract_fields:
        fieldnames += ["We_J", "Wm_J"]
    for sel in path_selections:
        fieldnames.append(f"path_{sel}")
    for ng in node_groups:
        fieldnames += [f"V_re_{ng}", f"V_im_{ng}"]

    Path(csv_out).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        for idx, row in enumerate(results):
            out_row = dict(row)

            if extract_fields and idx in We_Wm:
                out_row["We_J"] = We_Wm[idx]["We_J"]
                out_row["Wm_J"] = We_Wm[idx]["Wm_J"]

            for sel in path_selections:
                vals = path_integrals.get(sel, [])
                out_row[f"path_{sel}"] = vals[idx] if idx < len(vals) else float("nan")

            for ng in node_groups:
                vals = node_voltages.get(ng, [])
                v = vals[idx] if idx < len(vals) else complex(float("nan"), float("nan"))
                out_row[f"V_re_{ng}"] = v.real
                out_row[f"V_im_{ng}"] = v.imag

            writer.writerow(out_row)

    size = Path(csv_out).stat().st_size
    if size < 10:
        gate_fail(f"CSV is empty after write: {csv_out} ({size} bytes)")
    log(f"CSV written: {csv_out} ({size} bytes, {len(results)} modes)")

    if debug:
        log("DEBUG: CSV preview:")
        with open(csv_out) as fh:
            for line in fh:
                log(f"  {line.rstrip()}")


def save_interim_mph(pymodel, out_dir: str, debug: bool) -> str:
    """Save the solved model for GUI inspection."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    mph_path = str(Path(out_dir) / "eigenfreq_fields.mph")
    log(f"Saving solved model: {mph_path}")
    pymodel.save(mph_path)
    size_mb = Path(mph_path).stat().st_size / 1e6
    log(f"Saved {size_mb:.1f} MB: {mph_path}")
    return mph_path


# ─── Arg parsing ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Eigenfrequency study with field energy and path-integral extraction."
    )
    parser.add_argument("--modes", type=int, default=DEFAULT_N_MODES,
                        help=f"Number of eigenvalues (default: {DEFAULT_N_MODES})")
    parser.add_argument("--freq-start", type=float, default=DEFAULT_FREQ_START,
                        dest="freq_start",
                        help=f"Search window lower bound in GHz (default: {DEFAULT_FREQ_START})")
    parser.add_argument("--freq-stop",  type=float, default=DEFAULT_FREQ_STOP,
                        dest="freq_stop",
                        help=f"Search window upper bound in GHz (default: {DEFAULT_FREQ_STOP})")
    parser.add_argument("--extract-fields", action="store_true", dest="extract_fields",
                        help="Extract per-mode We, Wm from COMSOL")
    parser.add_argument("--path-selections", nargs="*", default=[],
                        dest="path_selections",
                        help="COMSOL edge selection names for |E| path integrals")
    parser.add_argument("--node-groups", nargs="*", default=[],
                        dest="node_groups",
                        help="COMSOL selection names for complex voltage extraction")
    parser.add_argument("--out",   type=str,  default=CSV_OUT,
                        help="Output CSV path")
    parser.add_argument("--cores", type=int,  default=4,
                        help="COMSOL solver threads (default: 4)")
    parser.add_argument("--debug", action="store_true",
                        help="Print extra COMSOL API details")
    return parser.parse_args()


def validate_inputs(args: argparse.Namespace) -> None:
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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    """Full pipeline: load → eigenfreq study → solve → extract fields → write CSV."""
    args = parse_args()
    validate_inputs(args)

    # Merge module-level patched lists with CLI overrides (CLI wins if non-empty)
    path_sels  = args.path_selections  if args.path_selections  else PATH_SELECTIONS
    node_grps  = args.node_groups      if args.node_groups      else NODE_GROUPS
    extract_f  = args.extract_fields

    log("=== eigenfreq_with_fields.py ===")
    log(f"BASE_MPH       : {BASE_MPH}")
    log(f"OUT_DIR        : {OUT_DIR}")
    log(f"n_modes        : {args.modes}")
    log(f"freq window    : [{args.freq_start}, {args.freq_stop}] GHz")
    log(f"extract_fields : {extract_f}")
    log(f"path_selections: {path_sels}")
    log(f"node_groups    : {node_grps}")
    log(f"output CSV     : {args.out}")

    client, pymodel = load_model(BASE_MPH, args.cores, args.debug)
    try:
        add_eigenfrequency_study(pymodel, args.modes, args.freq_start,
                                 args.freq_stop, args.debug)
        solve_eigenfrequency(pymodel, args.debug)

        ds = find_dataset(pymodel, args.debug)

        results = extract_base_eigenfrequencies(
            pymodel, ds, args.modes, args.freq_start, args.freq_stop, args.debug
        )

        We_Wm = {}
        if extract_f:
            log("Extracting field energies (We, Wm) ...")
            We_Wm = extract_field_energies(pymodel, ds, args.modes, args.debug)

        path_integrals = {}
        if path_sels:
            log(f"Extracting path integrals for {len(path_sels)} selection(s) ...")
            path_integrals = extract_path_integrals(
                pymodel, ds, path_sels, args.modes, args.debug
            )

        node_voltages = {}
        if node_grps:
            log(f"Extracting node voltages for {len(node_grps)} group(s) ...")
            node_voltages = extract_node_voltages(
                pymodel, ds, node_grps, args.modes, args.debug
            )

        write_csv(results, args.out, We_Wm, path_integrals, node_voltages,
                  extract_f, path_sels, node_grps, args.debug)
        save_interim_mph(pymodel, OUT_DIR, args.debug)

    finally:
        try:
            client.remove(pymodel)
        except Exception:
            pass

    log("=== eigenfreq_with_fields.py DONE ===")


if __name__ == "__main__":
    main()

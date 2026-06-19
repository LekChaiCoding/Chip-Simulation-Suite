#!/usr/bin/env python3
"""Unified result plotter for the AI Simulation Stack.

Handles three data types:
  1. S-parameter CSV       (freq_Hz, S11_dB, S21_dB, ...)
  2. Stub-sweep .dat       (JTWPA: stub_length_um, freq_GHz, S11_re, ...)
  3. Eigenfrequency CSV    (mode, freq_ghz, Q_factor, loss_rate_mhz)

Usage:
  plot_results.py --input <path> --out <png>
  plot_results.py --input <path> --out <png> --target-freq 5.5
  plot_results.py --input <session_dir> --out <png> --compare

Output:
  PNG file, ≥ 10 KB (gate fails below this size).

Debug flag: --debug prints data shape info before plotting.
"""

import argparse
import csv
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# matplotlib is required; if missing the gate fires with a clear message.
try:
    import matplotlib
    matplotlib.use("Agg")   # non-interactive backend for headless servers
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm
except ImportError:
    print("[GATE-3 FAIL] matplotlib not found — install with: pip install matplotlib",
          flush=True)
    sys.exit(1)

FIGURE_DPI = 150
FIGURE_MIN_KB = 10   # gate: output PNG must be larger than this


# ─── Data loaders ────────────────────────────────────────────────────────────────

def load_sparams(csv_path: str, debug: bool = False) -> Dict:
    """Load S-parameter CSV: freq_Hz, S11_dB, S21_dB [, S12_dB, S22_dB]."""
    rows = []
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append({k: float(v) for k, v in row.items()})
    if not rows:
        print(f"[GATE-3 FAIL] S-param CSV is empty: {csv_path}", flush=True)
        sys.exit(1)
    freq_ghz = [r["freq_Hz"] / 1e9 for r in rows]
    s11 = [r.get("S11_dB", float("nan")) for r in rows]
    s21 = [r.get("S21_dB", float("nan")) for r in rows]
    if debug:
        print(f"[DEBUG] sparams: {len(rows)} rows, "
              f"freq {freq_ghz[0]:.2f}–{freq_ghz[-1]:.2f} GHz")
    return {"type": "sparams", "freq_ghz": freq_ghz, "S11_dB": s11, "S21_dB": s21}


def load_stub_sweep(dat_path: str, debug: bool = False) -> Dict:
    """Load stub-sweep .dat: CSV with columns stub_length_um, freq_Hz, S11_re, S11_im, S21_re, S21_im, ...

    Lines starting with # are comment headers (skipped). The file uses commas as
    delimiters and freq is in Hz (converted to GHz internally).
    Returns a matrix of |S21| in dB for the heatmap panel.
    """
    stubs = set()
    freqs = set()
    data_rows = []
    # Strip comment lines before passing to DictReader (DictReader uses the first
    # non-comment line as the header).
    with open(dat_path) as fh:
        non_comment_lines = [l for l in fh if not l.startswith("#")]
    reader = csv.DictReader(non_comment_lines)
    for row in reader:
        stub = float(row["stub_length_um"])
        freq_ghz = float(row["freq_Hz"]) / 1e9   # Hz → GHz
        s21_re = float(row["S21_re"])
        s21_im = float(row["S21_im"])
        # NaN rows (port-2 not measured) are skipped cleanly
        if math.isnan(s21_re) or math.isnan(s21_im):
            continue
        s21_db = 20 * math.log10(abs(complex(s21_re, s21_im)) + 1e-30)
        stubs.add(stub)
        freqs.add(freq_ghz)
        data_rows.append((stub, freq_ghz, s21_db))

    stubs_list = sorted(stubs)
    freqs_list = sorted(freqs)
    if not stubs_list or not freqs_list:
        print(f"[GATE-3 FAIL] Stub-sweep .dat is empty or malformed: {dat_path}",
              flush=True)
        sys.exit(1)
    # Build 2-D matrix (rows = stubs, cols = freqs)
    s21_matrix = [[float("nan")] * len(freqs_list) for _ in range(len(stubs_list))]
    stub_idx = {s: i for i, s in enumerate(stubs_list)}
    freq_idx = {f: i for i, f in enumerate(freqs_list)}
    for stub, freq, db in data_rows:
        s21_matrix[stub_idx[stub]][freq_idx[freq]] = db
    if debug:
        print(f"[DEBUG] stub_sweep: {len(stubs_list)} stubs × {len(freqs_list)} freqs")
    return {"type": "stub_sweep", "stubs": stubs_list, "freqs": freqs_list,
            "S21_matrix": s21_matrix}


def load_eigenfreqs(csv_path: str, debug: bool = False) -> Dict:
    """Load eigenfrequency CSV: mode, freq_ghz, Q_factor, loss_rate_mhz."""
    rows = []
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append({
                "mode": int(row["mode"]),
                "freq_ghz": float(row["freq_ghz"]),
                "Q_factor": float(row["Q_factor"]),
                "loss_rate_mhz": float(row["loss_rate_mhz"]),
            })
    if not rows:
        print(f"[GATE-3 FAIL] Eigenfrequency CSV is empty: {csv_path}", flush=True)
        sys.exit(1)
    if debug:
        print(f"[DEBUG] eigenfreqs: {len(rows)} modes, "
              f"freq range {rows[0]['freq_ghz']:.2f}–{rows[-1]['freq_ghz']:.2f} GHz")
    return {"type": "eigenfreqs", "rows": rows}


# ─── Panel plotters ──────────────────────────────────────────────────────────────

def plot_sparams_panel(
    ax, freq_ghz: List[float], s11_db: List[float], s21_db: List[float],
    target_freq_ghz: Optional[float] = None,
) -> None:
    """Plot S11 and S21 vs. frequency on a single axis."""
    ax.plot(freq_ghz, s21_db, label="S21 (dB)", color="steelblue", linewidth=1.5)
    ax.plot(freq_ghz, s11_db, label="S11 (dB)", color="tomato", linewidth=1.0,
            linestyle="--", alpha=0.8)
    if target_freq_ghz is not None:
        ax.axvline(target_freq_ghz, color="green", linestyle=":", linewidth=1.2,
                   label=f"Target {target_freq_ghz:.2f} GHz")
    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel("S-parameter (dB)")
    ax.set_title("S-parameters")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.4)


def plot_stub_heatmap_panel(
    ax, stub_lengths: List[float], freqs: List[float], s21_matrix: List[List[float]],
) -> None:
    """2D heatmap of |S21| dB vs. (stub_length, frequency)."""
    import numpy as np
    mat = np.array(s21_matrix)
    im = ax.imshow(
        mat, aspect="auto", origin="lower",
        extent=[freqs[0], freqs[-1], stub_lengths[0], stub_lengths[-1]],
        cmap="viridis", vmin=mat[~np.isnan(mat)].min() if mat.size > 0 else -60,
        vmax=0,
    )
    plt.colorbar(im, ax=ax, label="|S21| (dB)")
    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel("Stub length (µm)")
    ax.set_title("Stub-length sweep |S21|")


def plot_eigenfreq_panel(
    ax, eigenfreqs: List[Dict], target_freq_ghz: Optional[float] = None,
) -> None:
    """Bar chart of eigenfrequencies with Q-factor annotations."""
    modes = [r["mode"] for r in eigenfreqs]
    freqs = [r["freq_ghz"] for r in eigenfreqs]
    qs = [r["Q_factor"] for r in eigenfreqs]

    colors = ["steelblue" if target_freq_ghz is None
              else ("green" if abs(f - target_freq_ghz) < 0.1 else "steelblue")
              for f in freqs]
    bars = ax.bar(modes, freqs, color=colors, alpha=0.8, edgecolor="black", linewidth=0.7)
    for bar, q, f in zip(bars, qs, freqs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.05,
                f"{f:.3f}\nQ={q:.0f}", ha="center", va="bottom", fontsize=7)

    if target_freq_ghz is not None:
        ax.axhline(target_freq_ghz, color="green", linestyle=":", linewidth=1.2,
                   label=f"Target {target_freq_ghz:.2f} GHz")
        ax.legend(fontsize=8)

    ax.set_xlabel("Mode index")
    ax.set_ylabel("Frequency (GHz)")
    ax.set_title("Eigenfrequencies")
    ax.set_xticks(modes)
    ax.grid(True, axis="y", alpha=0.4)


def plot_iteration_comparison(
    ax, iterations: List[Dict], target_freq_ghz: Optional[float] = None,
) -> None:
    """Track freq and Q across design iterations from session.yaml."""
    iter_nums = [it["iter"] for it in iterations]
    freqs = [it.get("result", {}).get("mode1_freq_ghz", float("nan"))
             for it in iterations]
    qs = [it.get("result", {}).get("mode1_Q", float("nan")) for it in iterations]

    ax.plot(iter_nums, freqs, "o-", color="steelblue", label="freq_ghz")
    if target_freq_ghz is not None:
        ax.axhline(target_freq_ghz, color="green", linestyle=":", linewidth=1.2,
                   label=f"Target {target_freq_ghz:.2f} GHz")
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Frequency (GHz)", color="steelblue")
    ax.tick_params(axis="y", labelcolor="steelblue")
    ax.set_title("Design iteration history")
    ax.grid(True, alpha=0.4)
    ax.legend(fontsize=8)

    # Q factor on a secondary y-axis
    ax2 = ax.twinx()
    ax2.plot(iter_nums, qs, "s--", color="tomato", label="Q_factor")
    ax2.set_ylabel("Q factor", color="tomato")
    ax2.tick_params(axis="y", labelcolor="tomato")
    ax2.legend(fontsize=8, loc="lower right")


# ─── Data discovery ──────────────────────────────────────────────────────────────

def discover_data(input_path: str, debug: bool = False) -> Dict:
    """Auto-detect what kind of data lives at input_path.

    Accepts:
      - A CSV file  → detect by header (mode,freq_ghz = eigenfreqs; freq_Hz = sparams)
      - A .dat file → stub sweep
      - A directory → look for eigenfrequencies.csv, then sparams.csv, then *.dat
    """
    p = Path(input_path)
    if p.is_file():
        if p.suffix == ".dat":
            return load_stub_sweep(str(p), debug)
        # CSV: peek at header
        with open(p) as fh:
            header = fh.readline().strip().lower()
        if "freq_ghz" in header and "q_factor" in header:
            return load_eigenfreqs(str(p), debug)
        if "freq_hz" in header or "s11" in header.lower():
            return load_sparams(str(p), debug)
        print(f"[GATE-3 FAIL] Cannot detect data type from header: {header}", flush=True)
        sys.exit(1)

    if p.is_dir():
        for candidate in ["eigenfrequencies.csv", "sparams.csv", "stub_length_sweep.dat"]:
            c = p / candidate
            if c.is_file():
                return discover_data(str(c), debug)
        print(f"[GATE-3 FAIL] No recognised data file in directory: {p}", flush=True)
        sys.exit(1)

    print(f"[GATE-3 FAIL] Input path not found: {input_path}", flush=True)
    sys.exit(1)


def load_session(session_dir: str, debug: bool = False) -> List[Dict]:
    """Parse session.yaml and return the iterations list."""
    try:
        import yaml
    except ImportError:
        print("[GATE-3 FAIL] PyYAML not installed — pip install pyyaml", flush=True)
        sys.exit(1)
    yaml_path = Path(session_dir) / "session.yaml"
    if not yaml_path.is_file():
        print(f"[GATE-3 FAIL] session.yaml not found: {yaml_path}", flush=True)
        sys.exit(1)
    with open(yaml_path) as fh:
        sess = yaml.safe_load(fh)
    iterations = sess.get("iterations", [])
    if not iterations:
        print(f"[GATE-3 FAIL] session.yaml has no iterations: {yaml_path}", flush=True)
        sys.exit(1)
    if debug:
        print(f"[DEBUG] session: {len(iterations)} iterations")
    return iterations


# ─── Main ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot COMSOL simulation results (eigenfreqs, S-params, sweeps)."
    )
    parser.add_argument("--input", required=True,
                        help="CSV, .dat file, or session directory to plot")
    parser.add_argument("--out", required=True,
                        help="Output PNG path")
    parser.add_argument("--device", default=None,
                        help="Device type (resonator/transmon/twpa) — for plot title")
    parser.add_argument("--compare", action="store_true",
                        help="Iteration comparison mode: --input must be a session dir")
    parser.add_argument("--target-freq", type=float, default=None, dest="target_freq",
                        help="Target frequency in GHz — shown as a reference line")
    parser.add_argument("--debug", action="store_true",
                        help="Print data shape info before plotting")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    fig, ax = plt.subplots(figsize=(9, 5), dpi=FIGURE_DPI)
    title_prefix = f"{args.device} — " if args.device else ""

    if args.compare:
        # Iteration comparison mode: read session.yaml from the input directory.
        iterations = load_session(args.input, args.debug)
        plot_iteration_comparison(ax, iterations, args.target_freq)
        fig.suptitle(f"{title_prefix}Design Iteration History", fontsize=12)
    else:
        data = discover_data(args.input, args.debug)
        dtype = data["type"]

        if dtype == "eigenfreqs":
            plot_eigenfreq_panel(ax, data["rows"], args.target_freq)
            fig.suptitle(f"{title_prefix}Eigenfrequency Results", fontsize=12)

        elif dtype == "sparams":
            plot_sparams_panel(ax, data["freq_ghz"], data["S11_dB"], data["S21_dB"],
                               args.target_freq)
            fig.suptitle(f"{title_prefix}S-parameter Sweep", fontsize=12)

        elif dtype == "stub_sweep":
            # For stub sweep, replace the single axis with a full-figure imshow.
            plt.close(fig)
            fig, ax = plt.subplots(figsize=(10, 6), dpi=FIGURE_DPI)
            plot_stub_heatmap_panel(ax, data["stubs"], data["freqs"], data["S21_matrix"])
            fig.suptitle(f"{title_prefix}Stub-Length Sweep", fontsize=12)

        else:
            print(f"[GATE-3 FAIL] Unknown data type: {dtype}", flush=True)
            sys.exit(1)

    fig.tight_layout()

    # Write PNG and gate on file size.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=FIGURE_DPI, bbox_inches="tight")
    plt.close(fig)

    size_kb = out_path.stat().st_size / 1024
    if size_kb < FIGURE_MIN_KB:
        print(f"[GATE-3 FAIL] Output PNG suspiciously small: {size_kb:.1f} KB < "
              f"{FIGURE_MIN_KB} KB — matplotlib may have written an empty figure",
              flush=True)
        sys.exit(1)

    print(f"[plot_results] Saved {size_kb:.0f} KB → {out_path}", flush=True)


if __name__ == "__main__":
    main()

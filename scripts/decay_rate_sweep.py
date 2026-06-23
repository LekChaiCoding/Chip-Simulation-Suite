#!/usr/bin/env python3
"""Decay rate sweep: extract κ (kappa) and T₁ for a lumped-element port coupling.

For each sweep value this script:
  1. Sets sweep_param = val[sweep_unit] in the COMSOL parameter table.
  2. Rebuilds geometry and mesh.
  3. Runs a frequency-domain study at freq_ghz (or the model default).
  4. Extracts complex voltage at junction_selection and port_selection.
  5. Computes:
       kappa [rad/s] = |V_port / V_junction|^2 / (Z0 * C_shunt)
       T1    [s]     = 1 / kappa
  6. Appends a row to CSV_OUT and saves an interim .mph.

Device-agnostic: works for qubit Purcell decay (sweep LJJ), drive-port coupling
(sweep coupling inductance), resonator external Q (sweep coupling gap), or any
admittance-dominated decay channel.

Usage:
  decay_rate_sweep.py
      --sweep-param  NAME        COMSOL parameter to sweep
      --sweep-values V1 V2 ...   Values to sweep
      --sweep-unit   UNIT        COMSOL unit string (e.g. H, um, 1)
      --junction-selection  SEL  COMSOL selection for lumped element voltage node
      --port-selection      SEL  COMSOL selection for output port voltage node
      --shunt-cap-fF        C    Shunt capacitance in fF
      --z0                  OHM  Port impedance in Ω (default 50)
      --freq-ghz            GHZ  Drive frequency (optional; uses model default if omitted)
      --resume                   Skip values already in CSV
      --out             PATH     Output CSV path
      --cores           N        COMSOL solver threads (default: 4)
      --debug                    Extra diagnostic output

Inputs (patched by run_decay_rate_sweep MCP tool):
  BASE_MPH  — built .mph with the sweep parameter and both selections defined
  OUT_DIR   — directory for interim .mph files
  CSV_OUT   — output CSV path

Outputs:
  CSV_OUT  — sweep results: sweep_param, freq_ghz, V_junc_mag, V_port_mag,
             kappa_rad_s, kappa_mhz, T1_us, T1_ns
  <OUT_DIR>/<sweep_param>_<val>.mph  — model at each sweep point
"""

import argparse
import csv
import cmath
import os
import sys
import time
from pathlib import Path

# ─── Module-level path constants (patched by MCP tool) ──────────────────────
ROOT     = "/mnt/smb/HSS/users/Alex/Chip Simulation"
BASE_MPH = f"{ROOT}/python_outputs/recreation_solved.mph"
OUT_DIR  = f"{ROOT}/python_outputs/decay_sweep"
CSV_OUT  = f"{ROOT}/python_outputs/decay_sweep/decay_sweep.csv"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gate_fail(msg: str) -> None:
    print(f"[GATE FAIL] {msg}", flush=True)
    sys.exit(1)


def _safe_list(vals) -> list:
    try:
        return [float(v) for v in vals]
    except Exception:
        return list(vals)


# ─── Geometry / mesh rebuild ─────────────────────────────────────────────────

def rebuild_geometry_mesh(pymodel, sweep_param, val, sweep_unit, debug):
    m = pymodel.java
    val_str = f"{val}[{sweep_unit}]"
    log(f"  Setting {sweep_param} = {val_str}")
    m.param().set(sweep_param, val_str)

    log("  Rebuilding geometry ...")
    t0 = time.time()
    try:
        m.geom("geom1").run()
    except Exception as exc:
        gate_fail(f"Geometry rebuild failed for {sweep_param}={val_str}: {exc}")
    log(f"  Geometry done in {time.time()-t0:.1f} s")

    log("  Remeshing ...")
    t0 = time.time()
    try:
        m.mesh("mesh1").run()
    except Exception as exc:
        gate_fail(f"Mesh rebuild failed for {sweep_param}={val_str}: {exc}")
    log(f"  Mesh done in {time.time()-t0:.1f} s")


# ─── Frequency-domain solve ──────────────────────────────────────────────────

def run_freq_domain(pymodel, freq_ghz, debug):
    """Add and run a single-frequency frequency-domain study. Returns dataset tag."""
    m = pymodel.java
    tag = "stdFreq"
    try:
        m.study().remove(tag)
    except Exception:
        pass

    m.study().create(tag)
    m.study(tag).label(tag)
    m.study(tag).create("freq", "Frequency")
    feat = m.study(tag).feature("freq")

    if freq_ghz is not None:
        feat.set("plist", f"{freq_ghz}[GHz]")

    log(f"  Solving frequency-domain @ {freq_ghz} GHz ...")
    t0 = time.time()
    pymodel.solve(tag)
    log(f"  Solve done in {time.time()-t0:.0f} s")

    all_ds = [d.name() for d in pymodel / "datasets"]
    return all_ds[-1] if all_ds else None


# ─── Voltage extraction + kappa calculation ──────────────────────────────────

def extract_voltages(pymodel, ds, junction_sel, port_sel, debug):
    """Return (V_junc_complex, V_port_complex) as Python complex numbers."""
    def get_v(sel_name):
        try:
            v_re = _safe_list(pymodel.evaluate("real(emw.V)", "V", dataset=ds, selection=sel_name))
            v_im = _safe_list(pymodel.evaluate("imag(emw.V)", "V", dataset=ds, selection=sel_name))
            re_val = v_re[0] if v_re else float("nan")
            im_val = v_im[0] if v_im else float("nan")
            return complex(re_val, im_val)
        except Exception as exc:
            log(f"  WARNING: voltage extraction failed for '{sel_name}': {exc}")
            return complex(float("nan"), float("nan"))

    v_junc = get_v(junction_sel)
    v_port = get_v(port_sel)

    if debug:
        log(f"  DEBUG: V_junc = {v_junc:.4e}  V_port = {v_port:.4e}")

    return v_junc, v_port


def compute_kappa(v_junc: complex, v_port: complex, C_F: float, Z0_Ohm: float) -> dict:
    """Compute kappa from voltage ratio.

    kappa = |V_port / V_junc|^2 / (Z0 * C)   [rad/s]
    T1    = 1 / kappa                           [s]
    """
    v_junc_mag = abs(v_junc)
    v_port_mag = abs(v_port)

    if v_junc_mag < 1e-30:
        return {
            "V_junc_mag_V": v_junc_mag,
            "V_port_mag_V": v_port_mag,
            "kappa_rad_s": float("nan"),
            "kappa_MHz": float("nan"),
            "T1_us": float("nan"),
            "T1_ns": float("nan"),
        }

    ratio_sq = (v_port_mag / v_junc_mag) ** 2
    kappa = ratio_sq / (Z0_Ohm * C_F)   # rad/s

    T1_s = 1.0 / kappa if kappa > 0 else float("nan")

    return {
        "V_junc_mag_V": v_junc_mag,
        "V_port_mag_V": v_port_mag,
        "kappa_rad_s": kappa,
        "kappa_MHz": kappa / (2.0 * 3.14159265358979 * 1e6),
        "T1_us": T1_s * 1e6 if not (T1_s != T1_s) else float("nan"),
        "T1_ns": T1_s * 1e9 if not (T1_s != T1_s) else float("nan"),
    }


# ─── CSV helpers ─────────────────────────────────────────────────────────────

def load_existing_values(csv_out: str, sweep_param: str) -> set:
    p = Path(csv_out)
    if not p.is_file():
        return set()
    done = set()
    with open(p, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                done.add(float(row[sweep_param]))
            except (KeyError, ValueError):
                pass
    return done


def append_row(csv_out: str, row: dict, fieldnames: list) -> None:
    p = Path(csv_out)
    write_header = not p.is_file()
    with open(p, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


# ─── Arg parsing ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Generic COMSOL decay rate sweep.")
    p.add_argument("--sweep-param",       required=True,  dest="sweep_param")
    p.add_argument("--sweep-values",      nargs="+",      dest="sweep_values",  type=float, required=True)
    p.add_argument("--sweep-unit",        required=True,  dest="sweep_unit")
    p.add_argument("--junction-selection", required=True, dest="junction_sel")
    p.add_argument("--port-selection",    required=True,  dest="port_sel")
    p.add_argument("--shunt-cap-fF",      required=True,  dest="shunt_cap_fF", type=float)
    p.add_argument("--z0",                default=50.0,   dest="z0",           type=float)
    p.add_argument("--freq-ghz",          default=None,   dest="freq_ghz",     type=float)
    p.add_argument("--resume",            action="store_true")
    p.add_argument("--out",               default=CSV_OUT)
    p.add_argument("--cores",             type=int, default=4)
    p.add_argument("--debug",             action="store_true")
    return p.parse_args()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    C_F = args.shunt_cap_fF * 1e-15   # fF → F

    log("=== decay_rate_sweep.py ===")
    log(f"BASE_MPH         : {BASE_MPH}")
    log(f"sweep_param      : {args.sweep_param}")
    log(f"sweep_values     : {args.sweep_values}")
    log(f"sweep_unit       : {args.sweep_unit}")
    log(f"junction_sel     : {args.junction_sel}")
    log(f"port_sel         : {args.port_sel}")
    log(f"shunt_cap_fF     : {args.shunt_cap_fF} fF  ({C_F:.3e} F)")
    log(f"Z0               : {args.z0} Ω")
    log(f"freq_ghz         : {args.freq_ghz}")
    log(f"out              : {args.out}")

    if not Path(BASE_MPH).is_file():
        gate_fail(f"BASE_MPH not found: {BASE_MPH}")

    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    done_vals = load_existing_values(args.out, args.sweep_param) if args.resume else set()

    FIELDNAMES = [
        args.sweep_param, "freq_ghz",
        "V_junc_mag_V", "V_port_mag_V",
        "kappa_rad_s", "kappa_MHz", "T1_us", "T1_ns",
    ]

    import mph
    log(f"Starting mph client (cores={args.cores}) ...")
    client = mph.start(cores=args.cores)
    log(f"Loading model: {BASE_MPH}")
    t0 = time.time()
    pymodel = client.load(BASE_MPH)
    log(f"Model loaded in {time.time()-t0:.1f} s")

    try:
        for val in args.sweep_values:
            if args.resume and val in done_vals:
                log(f"  Skipping {args.sweep_param}={val} (already in CSV)")
                continue

            log(f"\n--- Sweep point: {args.sweep_param} = {val} [{args.sweep_unit}] ---")
            rebuild_geometry_mesh(pymodel, args.sweep_param, val, args.sweep_unit, args.debug)
            ds = run_freq_domain(pymodel, args.freq_ghz, args.debug)

            v_junc, v_port = extract_voltages(
                pymodel, ds, args.junction_sel, args.port_sel, args.debug
            )
            kappa_dict = compute_kappa(v_junc, v_port, C_F, args.z0)

            row = {
                args.sweep_param: val,
                "freq_ghz": args.freq_ghz if args.freq_ghz is not None else float("nan"),
                **kappa_dict,
            }
            append_row(args.out, row, FIELDNAMES)
            log(f"  kappa = {kappa_dict['kappa_MHz']:.4f} MHz  T1 = {kappa_dict['T1_us']:.2f} µs")

            mph_out = str(Path(OUT_DIR) / f"{args.sweep_param}_{val}.mph")
            pymodel.save(mph_out)
            log(f"  Saved interim .mph: {mph_out}")

    finally:
        try:
            client.remove(pymodel)
        except Exception:
            pass

    log(f"=== decay_rate_sweep.py DONE  CSV: {args.out} ===")


if __name__ == "__main__":
    main()

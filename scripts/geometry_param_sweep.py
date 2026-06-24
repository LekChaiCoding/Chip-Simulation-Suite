#!/usr/bin/env python3
"""Generic geometry parameter sweep for COMSOL.

Sweeps any named COMSOL parameter (l_slider_single, delta_angle_coupler,
stub_length, gap_width, junction_radius, …) and runs either:
  - eigenfrequency study: extract f, Q, loss_rate, and optionally We/Wm/path integrals
  - frequency_domain study: extract S-parameter magnitudes at specified frequencies

For each parameter value the script:
  1. Sets param_name = val[param_unit] in the COMSOL parameter table.
  2. Rebuilds geometry and mesh.
  3. Runs the configured study.
  4. Extracts results and appends a row to the output CSV.
  5. Saves an interim .mph for GUI inspection.

Device-agnostic: no geometry names hardcoded. The COMSOL model carries all
device-specific geometry; this script only drives the parametric loop.

Usage:
  geometry_param_sweep.py
      --param-name NAME        COMSOL parameter name to sweep
      --param-values V1 V2 ... Values to sweep
      --param-unit  UNIT       COMSOL unit string (default: um)
      --study-type  TYPE       eigenfrequency|frequency_domain (default: eigenfrequency)
      --n-modes     N          Eigenvalues per solve (eigenfreq only, default: 5)
      --freq-start  GHZ        Search window start (eigenfreq only, default: 1.0)
      --freq-stop   GHZ        Search window end   (eigenfreq only, default: 20.0)
      --extract-fields         Also extract We/Wm/path integrals (eigenfreq only)
      --path-selections SEL…   COMSOL selections for |E| path integrals
      --node-groups GRP…       COMSOL node groups for voltage extraction
      --freq-points F1 F2 ...  Evaluation frequencies in GHz (freq_domain only)
      --port        PORT        Port excitation: 1|2|both (freq_domain, default: both)
      --resume                 Skip param values already in CSV
      --out         PATH       Output CSV path
      --cores       N          COMSOL solver threads (default: 4)
      --debug                  Extra diagnostic output

Inputs (patched by run_geometry_param_sweep MCP tool):
  BASE_MPH  — built (not solved) .mph with the target parameter defined
  OUT_DIR   — directory for interim .mph files and logs
  CSV_OUT   — output CSV path

Outputs:
  CSV_OUT  — sweep results (one row per param value)
  <OUT_DIR>/<param_name>_<val>.mph  — solved model at each sweep point
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
ROOT     = "/mnt/smb/HSS/users/Alex/Chip Simulation"
BASE_MPH = f"{ROOT}/python_outputs/recreation_solved.mph"
OUT_DIR  = f"{ROOT}/python_outputs/geom_sweep"
CSV_OUT  = f"{ROOT}/python_outputs/geom_sweep/param_sweep.csv"


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


# ─── Eigenfrequency extraction ───────────────────────────────────────────────

def _run_eigenfreq_point(pymodel, n_modes, freq_start, freq_stop, debug):
    """Add eigenfrequency study, solve, extract base modes. Returns (ds, modes)."""
    m = pymodel.java
    tag = "stdEig"
    # Remove previous study if present (handles re-use from prior loop iterations).
    try:
        m.study().remove(tag)
    except Exception:
        pass

    m.study().create(tag)
    m.study(tag).label(tag)
    m.study(tag).create("eig", "Eigenfrequency")
    feat = m.study(tag).feature("eig")
    feat.set("neigsactive", "on")
    feat.set("neigs", str(n_modes))
    feat.set("eigunit", "GHz")
    shift = (freq_start + freq_stop) / 2.0
    feat.set("shift", f"{shift}[GHz]")

    log(f"  Solving eigenfrequency (n={n_modes}, shift={shift:.2f} GHz) ...")
    t0 = time.time()
    pymodel.solve(tag)
    log(f"  Solve done in {time.time() - t0:.0f} s")

    all_ds = [d.name() for d in pymodel / "datasets"]
    ds = all_ds[-1] if all_ds else None

    eig_re = _safe_list(pymodel.evaluate("real(freq)", "GHz", dataset=ds))
    eig_im = _safe_list(pymodel.evaluate("imag(freq)", "GHz", dataset=ds))

    modes = []
    for i, (fr, fi) in enumerate(zip(eig_re, eig_im)):
        if math.isnan(fr) or math.isnan(fi):
            continue
        if fr < freq_start or fr > freq_stop:
            continue
        # Im(freq) = 0 is physically valid for PEC-bounded lossless models;
        # silently skipping them would produce an empty CSV.  Use Q=inf instead.
        if abs(fi) < 1e-20:
            q = float("inf")
            loss_mhz = 0.0
        else:
            q = fr / (2.0 * abs(fi))
            loss_mhz = abs(fi) * 2.0 * math.pi * 1e3
        modes.append({
            "mode": i + 1,
            "freq_ghz": round(fr, 6),
            "Q_factor": q if math.isinf(q) else round(q, 1),
            "loss_rate_mhz": round(loss_mhz, 4),
        })

    return ds, sorted(modes, key=lambda r: r["freq_ghz"])


def _extract_fields(pymodel, ds, modes, path_sels, node_grps, debug):
    """Append We/Wm and optional path integrals + node voltages to modes list."""
    # Field energies.
    try:
        we_vals = _safe_list(pymodel.evaluate("emw.intWe", "J", dataset=ds))
        wm_vals = _safe_list(pymodel.evaluate("emw.intWm", "J", dataset=ds))
        for i, m in enumerate(modes):
            m["We_J"] = we_vals[i] if i < len(we_vals) else float("nan")
            m["Wm_J"] = wm_vals[i] if i < len(wm_vals) else float("nan")
    except Exception as exc:
        log(f"  WARNING: field energy extraction failed: {exc}")
        for m in modes:
            m.setdefault("We_J", float("nan"))
            m.setdefault("Wm_J", float("nan"))

    # |E| path integrals.
    for sel_name in path_sels:
        col = f"path_{sel_name}"
        try:
            arc    = _safe_list(pymodel.evaluate("arc",      "m",   dataset=ds, selection=sel_name))
            normE  = _safe_list(pymodel.evaluate("emw.normE","V/m", dataset=ds, selection=sel_name))
            intval = float(np.trapezoid(normE, arc))
        except Exception as exc:
            log(f"  WARNING: path integral '{sel_name}' failed: {exc}")
            intval = float("nan")
        for m in modes:
            m[col] = intval

    # Node voltages.
    for ng_name in node_grps:
        col_re = f"V_re_{ng_name}"
        col_im = f"V_im_{ng_name}"
        try:
            v_re = _safe_list(pymodel.evaluate("real(emw.V)", "V", dataset=ds, selection=ng_name))
            v_im = _safe_list(pymodel.evaluate("imag(emw.V)", "V", dataset=ds, selection=ng_name))
            v_re_val = v_re[0] if v_re else float("nan")
            v_im_val = v_im[0] if v_im else float("nan")
        except Exception as exc:
            log(f"  WARNING: node voltage '{ng_name}' failed: {exc}")
            v_re_val = float("nan")
            v_im_val = float("nan")
        for m in modes:
            m[col_re] = v_re_val
            m[col_im] = v_im_val


# ─── Frequency-domain extraction ────────────────────────────────────────────

def _run_freqdomain_point(pymodel, freq_points_ghz, port, debug):
    """Run frequency-domain study, return list of S-parameter rows."""
    m = pymodel.java
    tag = "stdQ"
    try:
        m.study().remove(tag)
    except Exception:
        pass

    m.study().create(tag)
    m.study(tag).label(tag)
    m.study(tag).create("freq", "Frequency")
    feat = m.study(tag).feature("freq")
    freq_str = " ".join(f"{f}[GHz]" for f in freq_points_ghz)
    feat.set("plist", freq_str)
    if port in ("1", "2"):
        feat.set("port", port)

    log(f"  Solving frequency-domain ({len(freq_points_ghz)} points) ...")
    t0 = time.time()
    pymodel.solve(tag)
    log(f"  Solve done in {time.time() - t0:.0f} s")

    all_ds = [d.name() for d in pymodel / "datasets"]
    ds = all_ds[-1] if all_ds else None

    rows = []
    for f_ghz in freq_points_ghz:
        row = {"freq_ghz": f_ghz}
        for sij in ("S11", "S21", "S12", "S22"):
            if port == "1" and sij in ("S12", "S22"):
                continue
            if port == "2" and sij in ("S11", "S21"):
                continue
            try:
                expr = f"abs(emw.S{sij[1]}1)" if sij[2] == "1" else f"abs(emw.S{sij[1]}2)"
                val_list = _safe_list(pymodel.evaluate(expr, "1", dataset=ds))
                # Evaluate at the frequency nearest to f_ghz.
                row[f"|{sij}|"] = val_list[0] if val_list else float("nan")
            except Exception:
                row[f"|{sij}|"] = float("nan")
        rows.append(row)

    return rows


# ─── Model geometry + mesh rebuild ──────────────────────────────────────────

def rebuild_geometry_mesh(pymodel, param_name, val, param_unit, debug):
    """Set parameter, rebuild geometry and mesh. Aborts on exception."""
    m = pymodel.java
    val_str = f"{val}[{param_unit}]"
    log(f"  Setting {param_name} = {val_str}")
    m.param().set(param_name, val_str)

    log("  Rebuilding geometry ...")
    t0 = time.time()
    try:
        m.geom("geom1").run()
    except Exception as exc:
        gate_fail(f"Geometry rebuild failed for {param_name}={val_str}: {exc}")
    log(f"  Geometry done in {time.time()-t0:.1f} s")

    log("  Remeshing ...")
    t0 = time.time()
    try:
        m.mesh("mesh1").run()
    except Exception as exc:
        gate_fail(f"Mesh rebuild failed for {param_name}={val_str}: {exc}")
    log(f"  Mesh done in {time.time()-t0:.1f} s")


# ─── CSV helpers ─────────────────────────────────────────────────────────────

def load_existing_values(csv_out: str, param_name: str) -> set:
    """Return set of already-computed param values from the CSV (for --resume)."""
    p = Path(csv_out)
    if not p.is_file():
        return set()
    done = set()
    with open(p, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                done.add(float(row[param_name]))
            except (KeyError, ValueError):
                pass
    return done


def append_rows(csv_out: str, rows: list, fieldnames: list) -> None:
    p = Path(csv_out)
    write_header = not p.is_file()
    with open(p, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


# ─── Arg parsing ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Generic COMSOL geometry parameter sweep.")
    p.add_argument("--param-name",    required=True,  dest="param_name")
    p.add_argument("--param-values",  nargs="+",      dest="param_values",  type=float, required=True)
    p.add_argument("--param-unit",    default="um",   dest="param_unit")
    p.add_argument("--study-type",    default="eigenfrequency", dest="study_type",
                   choices=["eigenfrequency", "frequency_domain"])
    p.add_argument("--n-modes",       type=int,   default=5,    dest="n_modes")
    p.add_argument("--freq-start",    type=float, default=1.0,  dest="freq_start")
    p.add_argument("--freq-stop",     type=float, default=20.0, dest="freq_stop")
    p.add_argument("--extract-fields", action="store_true", dest="extract_fields")
    p.add_argument("--path-selections", nargs="*", default=[], dest="path_selections")
    p.add_argument("--node-groups",     nargs="*", default=[], dest="node_groups")
    p.add_argument("--freq-points",   nargs="*", type=float, default=[], dest="freq_points")
    p.add_argument("--port",          default="both")
    p.add_argument("--resume",        action="store_true")
    p.add_argument("--out",           default=CSV_OUT)
    p.add_argument("--cores",         type=int, default=4)
    p.add_argument("--debug",         action="store_true")
    return p.parse_args()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.study_type == "frequency_domain" and not args.freq_points:
        gate_fail("--freq-points required for frequency_domain sweep")

    log("=== geometry_param_sweep.py ===")
    log(f"BASE_MPH    : {BASE_MPH}")
    log(f"param_name  : {args.param_name}")
    log(f"param_values: {args.param_values}")
    log(f"param_unit  : {args.param_unit}")
    log(f"study_type  : {args.study_type}")
    log(f"out         : {args.out}")

    if not Path(BASE_MPH).is_file():
        gate_fail(f"BASE_MPH not found: {BASE_MPH}")

    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    # --resume: skip values already in the CSV.
    done_vals = load_existing_values(args.out, args.param_name) if args.resume else set()

    import mph
    log(f"Starting mph client (cores={args.cores}) ...")
    client = mph.start(cores=args.cores)
    log(f"Loading model: {BASE_MPH}")
    t0 = time.time()
    pymodel = client.load(BASE_MPH)
    log(f"Model loaded in {time.time()-t0:.1f} s")

    fieldnames = None

    try:
        for val in args.param_values:
            if args.resume and val in done_vals:
                log(f"  Skipping {args.param_name}={val} (already in CSV)")
                continue

            log(f"\n--- Sweep point: {args.param_name} = {val} [{args.param_unit}] ---")
            rebuild_geometry_mesh(pymodel, args.param_name, val, args.param_unit, args.debug)

            if args.study_type == "eigenfrequency":
                ds, modes = _run_eigenfreq_point(
                    pymodel, args.n_modes, args.freq_start, args.freq_stop, args.debug
                )
                if args.extract_fields:
                    _extract_fields(pymodel, ds, modes, args.path_selections,
                                    args.node_groups, args.debug)
                rows = [{args.param_name: val, **m} for m in modes]
            else:
                s_rows = _run_freqdomain_point(pymodel, args.freq_points, args.port, args.debug)
                rows = [{args.param_name: val, **r} for r in s_rows]

            if rows:
                if fieldnames is None:
                    fieldnames = list(rows[0].keys())
                append_rows(args.out, rows, fieldnames)
                log(f"  Appended {len(rows)} rows to {args.out}")

            # Save interim .mph for GUI inspection.
            mph_out = str(Path(OUT_DIR) / f"{args.param_name}_{val}.mph")
            pymodel.save(mph_out)
            log(f"  Saved interim .mph: {mph_out}")

    finally:
        try:
            client.remove(pymodel)
        except Exception:
            pass

    log(f"=== geometry_param_sweep.py DONE  CSV: {args.out} ===")


if __name__ == "__main__":
    main()

"""CAD stage tools: generate the chip GDS and verify it.

Three tools live here:

  * :func:`generate_cad` — runs the project's ``converter_group_recreation.py``
    to emit the 21-junction JTWPA GDS layout (the exact device imported into
    COMSOL). The upstream script hard-codes a Linux output path, so we run a
    *path-redirected copy* of it (see :func:`comsol_suite.runner.patch_script`)
    whose only difference is where the ``.gds`` / ``.png`` are written.

  * :func:`verify_cad` — reuses the project's ``cad_verify_gds.py`` checker
    *in-process* (it is import-safe and pure-``gdstk``/``numpy``). The checker
    compares a GDS against the vertex-validated reference geometry pins measured
    from the built COMSOL model, returning pass/fail per geometric feature.

  * :func:`run_custom_cad` — runs *any* user-supplied GDS generation script
    with path redirection, using the same patch-and-run approach as the built-in
    CAD tool. The variable names used for the GDS and PNG output paths can be
    configured, so scripts written for different devices plug in without
    modification.

Together they prove the first link of the pipeline: that the suite reproduces
the precise CAD that feeds COMSOL — and they give users a drop-in slot for
custom devices.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import load_config
from ..runner import patch_script, run_command


def _new_output_dir(prefix: str, output_dir: Optional[str]) -> Path:
    """Return a fresh output directory under runs/ (or honour an explicit one)."""
    cfg = load_config()
    if output_dir:
        out = Path(output_dir)
    else:
        out = cfg.runs_dir / f"{prefix}-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def generate_cad(output_dir: Optional[str] = None, debug: bool = False) -> Dict[str, Any]:
    """Generate the chip GDS layout (and a preview PNG).

    Parameters
    ----------
    output_dir
        Directory to write ``converter_group_recreation.gds`` / ``.png`` into.
        Defaults to a timestamped folder under ``runs/``.
    debug
        Echo the exact command line into the run log.

    Returns
    -------
    dict
        ``{ok, gds_path, preview_png, log_path, log_tail}``.
    """
    cfg = load_config()
    src = cfg.script("cad_generator")
    if not src.is_file():
        return {"ok": False, "error": f"CAD generator not found: {src}"}

    out = _new_output_dir("cad", output_dir)
    gds_path = out / "converter_group_recreation.gds"
    png_path = out / "converter_group_recreation.png"

    # Redirect only the two output-path assignments; geometry code is untouched.
    # Forward slashes keep the embedded string a valid Python literal on Windows.
    patched = patch_script(
        src,
        out / "_generate_cad_patched.py",
        {
            r"^OUT_GDS\s*=.*$": f'OUT_GDS = r"{gds_path.as_posix()}"',
            r"^OUT_PNG\s*=.*$": f'OUT_PNG = r"{png_path.as_posix()}"',
        },
    )

    res = run_command(
        [cfg.python_bin, patched],
        log_path=out / "generate_cad.log",
        cwd=out,
        timeout_s=180,
        debug=debug,
    )

    ok = res.ok and gds_path.is_file()
    return {
        "ok": ok,
        "gds_path": str(gds_path) if gds_path.is_file() else None,
        "preview_png": str(png_path) if png_path.is_file() else None,
        "returncode": res.returncode,
        "duration_s": round(res.duration_s, 2),
        "log_path": str(res.log_path),
        "log_tail": res.log_tail(20),
        "error": None if ok else "generator did not produce a GDS (see log_tail)",
    }


def _load_checker_module():
    """Import the upstream ``cad_verify_gds.py`` from its file path.

    It is ``__main__``-guarded and only defines functions + string constants at
    import time, so importing it has no side effects. We import by path (rather
    than by package name) because it lives outside this package's import roots.
    """
    cfg = load_config()
    verifier = cfg.script("cad_verifier")
    if not verifier.is_file():
        raise FileNotFoundError(f"CAD verifier not found: {verifier}")
    spec = importlib.util.spec_from_file_location("cad_verify_gds", verifier)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load spec for {verifier}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_cad(gds_path: Optional[str] = None, debug: bool = False) -> Dict[str, Any]:
    """Verify a GDS against the vertex-validated reference geometry pins.

    Reuses the project's own checker so the pass/fail criteria stay identical to
    what the CAD team uses by hand. The checker's ``main()`` returns ``0`` when
    every geometric check passes.

    Parameters
    ----------
    gds_path
        GDS file to verify. Defaults to the reference GDS in the repo.
    debug
        Include the full checker report in ``report`` regardless of pass/fail.

    Returns
    -------
    dict
        ``{passed, gds_path, n_failures, report}``.
    """
    cfg = load_config()
    target = Path(gds_path) if gds_path else cfg.datum("reference_gds")
    if not target.is_file():
        return {"passed": False, "error": f"GDS not found: {target}"}

    try:
        checker = _load_checker_module()
    except (FileNotFoundError, ImportError) as exc:
        return {"passed": False, "error": str(exc)}

    # Point the checker at our target GDS (its RECR constant is the file it
    # validates) and capture its printed report.
    checker.RECR = str(target)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = checker.main()
    except Exception as exc:  # pragma: no cover - upstream check error
        return {"passed": False, "gds_path": str(target),
                "error": f"checker raised: {type(exc).__name__}: {exc}",
                "report": buf.getvalue()}

    report = buf.getvalue()
    n_failures = report.count("[FAIL]")
    return {
        "passed": rc == 0,
        "gds_path": str(target),
        "n_failures": n_failures,
        "report": report if (debug or rc != 0) else report.splitlines()[-1],
    }


def run_custom_cad(
    cad_script: str,
    output_dir: Optional[str] = None,
    out_gds_var: str = "OUT_GDS",
    out_png_var: Optional[str] = "OUT_PNG",
    gds_filename: str = "output.gds",
    debug: bool = False,
) -> Dict[str, Any]:
    """Run a user-supplied GDS generation script with path redirection.

    Applies the same patch-and-run pattern as :func:`generate_cad`, but accepts
    any Python script rather than the built-in ``converter_group_recreation.py``.
    This lets users drop in scripts for custom chip geometries (different junction
    counts, different CPW parameters, different device topologies) without
    modifying this package.

    The script is copied to the output directory and the two output-path
    assignment lines are replaced with absolute paths before running. The
    original is never touched.

    Parameters
    ----------
    cad_script
        Absolute path to the CAD generation Python script.
    output_dir
        Directory to write outputs into. Defaults to a timestamped folder under
        ``runs/``.
    out_gds_var
        Name of the variable in the script that holds the GDS output path.
        Defaults to ``OUT_GDS`` (the convention used by
        ``converter_group_recreation.py``).
    out_png_var
        Name of the variable in the script that holds the PNG preview path.
        Set to ``None`` if the script does not produce a PNG preview.
        Defaults to ``OUT_PNG``.
    gds_filename
        Filename for the output GDS file (written inside ``output_dir``).
        Defaults to ``output.gds``.
    debug
        Echo the patched command into the run log.

    Returns
    -------
    dict
        ``{ok, gds_path, preview_png, duration_s, log_path, log_tail, error}``.
    """
    cfg = load_config()
    src = Path(cad_script)
    if not src.is_file():
        return {"ok": False, "error": f"CAD script not found: {src}"}

    out = _new_output_dir(f"cad_custom_{src.stem}", output_dir)
    gds_path = out / gds_filename
    png_path = out / (Path(gds_filename).stem + ".png")

    # Build patches: always redirect the GDS var; optionally the PNG var too.
    patches: Dict[str, str] = {
        rf"^{re.escape(out_gds_var)}\s*=.*$": (
            f'{out_gds_var} = r"{gds_path.as_posix()}"'
        ),
    }
    if out_png_var:
        patches[rf"^{re.escape(out_png_var)}\s*=.*$"] = (
            f'{out_png_var} = r"{png_path.as_posix()}"'
        )

    try:
        patched = patch_script(
            src,
            out / f"_{src.stem}_patched.py",
            patches,
            require_all=False,  # custom scripts may not have both vars
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    res = run_command(
        [cfg.python_bin, patched],
        log_path=out / f"{src.stem}.log",
        cwd=out,
        timeout_s=180,
        debug=debug,
    )

    ok = res.ok and gds_path.is_file()
    return {
        "ok": ok,
        "gds_path": str(gds_path) if gds_path.is_file() else None,
        "preview_png": str(png_path) if png_path.is_file() else None,
        "returncode": res.returncode,
        "duration_s": round(res.duration_s, 2),
        "log_path": str(res.log_path),
        "log_tail": res.log_tail(20),
        "error": None if ok else "CAD script did not produce a GDS (see log_tail)",
    }

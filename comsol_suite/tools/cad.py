"""CAD stage tools: generate, verify, assemble GDS layouts.

All tools here are **device-agnostic** — they accept a script path or checker
script rather than hardcoding any particular geometry.  The default fallbacks
(no script supplied) retain backward compatibility with the original JTWPA
scripts.

Four tools live here:

  * :func:`generate_cad` — run *any* GDS generation script with path
    redirection. When ``cad_script`` is omitted it falls back to the config
    default (``cad_generator``, currently the JTWPA script).

  * :func:`verify_cad` — run *any* geometry checker script in-process.
    The checker must define a module-level string constant (``gds_var``,
    default ``"RECR"``) that the tool overrides with the target GDS path,
    and a ``main() → int`` that returns 0 on pass.  When ``checker_script``
    is omitted it falls back to the project's JTWPA checker for backward
    compatibility.

  * :func:`run_custom_cad` — identical to :func:`generate_cad` but with an
    explicit ``cad_script`` argument (kept for backward compatibility).

  * :func:`assemble_geometry` — merge multiple GDS files into a single layout
    using gdstk References (or optionally flatten).  Works for unit-cell
    assembly, resonator arrays, multi-chip tapeouts, or any combination of
    sub-GDS components.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def generate_cad(
    cad_script: Optional[str] = None,
    out_gds_var: str = "OUT_GDS",
    out_png_var: Optional[str] = "OUT_PNG",
    output_dir: Optional[str] = None,
    gds_filename: Optional[str] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Generate a chip GDS layout from any GDS generation script.

    Device-agnostic: accepts any Python script that writes a GDS file.
    When ``cad_script`` is omitted the config default is used (backward
    compatible with the original JTWPA workflow).

    Parameters
    ----------
    cad_script
        Absolute path to the GDS generation script.  When ``None`` the config
        key ``cad_generator`` is used (falls back to the JTWPA script).
    out_gds_var
        Name of the module-level variable in the script that holds the output
        GDS path.  Defaults to ``OUT_GDS``.
    out_png_var
        Name of the module-level variable for the PNG preview.  Set to
        ``None`` if the script does not produce a PNG.  Defaults to ``OUT_PNG``.
    output_dir
        Directory to write outputs into.  Defaults to a timestamped folder
        under ``runs/``.
    gds_filename
        Filename for the output GDS (inside ``output_dir``).  When ``None``
        the script's stem is used (e.g. ``my_device.gds``).
    debug
        Echo the patched command into the run log.

    Returns
    -------
    dict
        ``{ok, gds_path, preview_png, duration_s, log_path, log_tail, error}``.
    """
    cfg = load_config()

    # Resolve source script: explicit arg > config default.
    if cad_script is not None:
        src = Path(cad_script)
    else:
        src = cfg.script("cad_generator")

    if not src.is_file():
        return {"ok": False, "error": f"CAD script not found: {src}"}

    out = _new_output_dir(f"cad_{src.stem}", output_dir)
    fname = gds_filename or f"{src.stem}.gds"
    gds_path = out / fname
    png_path = out / (Path(fname).stem + ".png")

    patches: Dict[str, str] = {
        rf"^{re.escape(out_gds_var)}\s*=.*$": f'{out_gds_var} = r"{gds_path.as_posix()}"',
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
            require_all=False,  # scripts may not define both vars
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

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
        "error": None if ok else "script did not produce a GDS (see log_tail)",
    }


def _load_checker_module(checker_path: Path, module_name: str = "cad_checker"):
    """Import a checker script from its file path.

    Checker scripts are ``__main__``-guarded and only define functions +
    string constants at import time, so importing them has no side effects.
    We import by path (not package name) because they live outside this
    package's import roots.
    """
    if not checker_path.is_file():
        raise FileNotFoundError(f"checker script not found: {checker_path}")
    spec = importlib.util.spec_from_file_location(module_name, checker_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load spec for {checker_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_cad(
    gds_path: Optional[str] = None,
    checker_script: Optional[str] = None,
    gds_var: str = "RECR",
    debug: bool = False,
) -> Dict[str, Any]:
    """Verify a GDS against any geometry checker script.

    Device-agnostic: accepts any checker script that follows the interface
    (module-level GDS path constant + ``main() → int``).  When
    ``checker_script`` is omitted the config default ``cad_verifier`` is used
    (backward compatible with the JTWPA workflow).

    Checker script interface
    ------------------------
    A valid checker script must:

    1. Define a module-level string constant (name given by ``gds_var``,
       default ``"RECR"``) that this tool overrides with ``gds_path``.
    2. Define a ``main() → int`` function that returns ``0`` when all checks
       pass and prints ``[FAIL] <description>`` for each failure.
    3. Be import-safe (no side effects at module level beyond constant
       definitions).

    Use ``scripts/checker_template.py`` as a starting point.

    Parameters
    ----------
    gds_path
        GDS file to verify.  Defaults to the config ``reference_gds`` asset
        when omitted (backward compatible).
    checker_script
        Absolute path to a checker script.  When ``None`` the config key
        ``cad_verifier`` is used (falls back to the JTWPA checker).
    gds_var
        Name of the module-level string constant in the checker that holds
        the GDS path.  Default ``"RECR"`` matches the JTWPA checker.
        Use ``"GDS_PATH"`` for scripts based on ``checker_template.py``.
    debug
        Include the full checker report in ``report`` regardless of pass/fail.

    Returns
    -------
    dict
        ``{passed, gds_path, n_failures, report}``.
    """
    cfg = load_config()

    # Resolve GDS target.
    target = Path(gds_path) if gds_path else cfg.datum("reference_gds")
    if not target.is_file():
        return {"passed": False, "error": f"GDS not found: {target}"}

    # Resolve checker script.
    checker_path = (
        Path(checker_script) if checker_script else cfg.script("cad_verifier")
    )

    try:
        checker = _load_checker_module(checker_path)
    except (FileNotFoundError, ImportError) as exc:
        return {"passed": False, "error": str(exc)}

    # Override the checker's GDS path constant and capture its printed report.
    if not hasattr(checker, gds_var):
        return {
            "passed": False,
            "error": (
                f"checker script '{checker_path.name}' does not define "
                f"'{gds_var}'. Pass the correct gds_var name."
            ),
        }
    setattr(checker, gds_var, str(target))

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
        "checker_script": str(checker_path),
        "n_failures": n_failures,
        "report": report if (debug or rc != 0) else (report.splitlines() or [""])[-1],
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


def assemble_geometry(
    components: List[Dict[str, Any]],
    output_path: str,
    top_cell_name: str = "assembly",
    merge_refs: bool = True,
) -> Dict[str, Any]:
    """Assemble multiple GDS files into a single layout using gdstk.

    Device-agnostic: works for 4-qubit unit cells, resonator arrays,
    multi-chip tapeouts, or any combination of sub-GDS components.
    No COMSOL connection required — runs synchronously.

    Parameters
    ----------
    components
        List of component dicts, each with keys:

        - ``gds_path`` *(str, required)* — absolute path to the source GDS.
        - ``cell_name`` *(str, required)* — name of the top cell inside that GDS.
        - ``x_um`` *(float, default 0)* — x placement offset in µm.
        - ``y_um`` *(float, default 0)* — y placement offset in µm.
        - ``rotation_deg`` *(float, default 0)* — rotation in degrees (CCW).
        - ``magnification`` *(float, default 1)* — scale factor.
        - ``x_reflection`` *(bool, default False)* — mirror about x-axis.

    output_path
        Absolute path for the output GDS file.
    top_cell_name
        Name of the top-level cell in the assembled GDS.
    merge_refs
        If ``True`` (default), sub-cells are placed as gdstk
        :class:`~gdstk.Reference` objects (compact, preserves hierarchy).
        If ``False``, all geometry is flattened into the top cell.

    Returns
    -------
    dict
        ``{ok, output_path, n_components, bbox, error}``.
        ``bbox`` is ``[[xmin, ymin], [xmax, ymax]]`` in µm, or ``None``
        if the assembly is empty.
    """
    import math

    try:
        import gdstk
    except ImportError:
        return {"ok": False, "error": "gdstk is not installed (pip install gdstk)"}

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lib = gdstk.Library()
    top = lib.new_cell(top_cell_name)

    loaded: Dict[str, Any] = {}  # cache: gds_path → gdstk.Library

    for i, comp in enumerate(components):
        src_path = comp.get("gds_path")
        cell_name = comp.get("cell_name")
        if not src_path or not cell_name:
            return {
                "ok": False,
                "error": f"component[{i}] missing required 'gds_path' or 'cell_name'",
            }

        src_path = str(src_path)
        if not Path(src_path).is_file():
            return {"ok": False, "error": f"component[{i}]: GDS not found: {src_path}"}

        if src_path not in loaded:
            loaded[src_path] = gdstk.read_gds(src_path)
        src_lib = loaded[src_path]

        # Find the requested cell.
        src_cell = next((c for c in src_lib.cells if c.name == cell_name), None)
        if src_cell is None:
            available = [c.name for c in src_lib.cells]
            return {
                "ok": False,
                "error": (
                    f"component[{i}]: cell '{cell_name}' not found in {src_path}. "
                    f"Available cells: {available}"
                ),
            }

        # Add the source cell (and its dependencies) to our library if not present.
        existing_names = {c.name for c in lib.cells}
        if src_cell.name not in existing_names:
            # Copy over this cell and all cells it references.
            for dep in src_lib.cells:
                if dep.name not in {c.name for c in lib.cells}:
                    lib.add(dep)

        x_um = float(comp.get("x_um", 0.0))
        y_um = float(comp.get("y_um", 0.0))
        rot_rad = math.radians(float(comp.get("rotation_deg", 0.0)))
        mag = float(comp.get("magnification", 1.0))
        x_ref = bool(comp.get("x_reflection", False))

        cell_in_lib = next(c for c in lib.cells if c.name == src_cell.name)

        if merge_refs:
            ref = gdstk.Reference(
                cell_in_lib,
                origin=(x_um, y_um),
                rotation=rot_rad,
                magnification=mag,
                x_reflection=x_ref,
            )
            top.add(ref)
        else:
            # Flatten: copy all polygons/paths with the transform applied.
            temp_ref = gdstk.Reference(
                cell_in_lib,
                origin=(x_um, y_um),
                rotation=rot_rad,
                magnification=mag,
                x_reflection=x_ref,
            )
            for poly in temp_ref.get_polygons():
                top.add(poly)
            for path in temp_ref.get_paths():
                top.add(path)

    lib.write_gds(str(out))

    # Compute bounding box.
    bbox = top.bounding_box()
    bbox_out = [list(bbox[0]), list(bbox[1])] if bbox is not None else None

    return {
        "ok": True,
        "output_path": str(out),
        "n_components": len(components),
        "bbox": bbox_out,
        "error": None,
    }

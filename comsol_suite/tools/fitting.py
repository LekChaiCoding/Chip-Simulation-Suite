"""Device-agnostic fitting: run whatever fitting script the caller names.

The JTWPA-specific surface moved to `jtwpa/fitting.py` on 2026-08-04 —
`run_abcd_fit`, `run_abcd_fit_parallel`, `fit_stub_sweep` and
`analyze_dispersion` all assume one device's data layout or call its Julia
scripts from the vendored `JosephsonCircuit/` tree. What stays here is
`run_generic_fit`, which takes a `fit_script` path and redirects its data/output
constants; it has no device assumptions at all.

`server.py` still exposes both sets, unchanged. The split is about being able to
tell which is which from the import path rather than by reading the body.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config
from ..jobs import Job, JobRegistry
from ..runner import patch_script, run_command


def _collect(out_dir: Path, patterns: List[str]) -> List[str]:
    """Return sorted file paths under ``out_dir`` matching any glob pattern."""
    found: List[str] = []
    for pat in patterns:
        found.extend(str(p) for p in out_dir.rglob(pat))
    return sorted(set(found))



# ─────────────────────────────────────────────────────────────────────────────
# Generic fitting tool  (any user-supplied Python fitting script)
# ─────────────────────────────────────────────────────────────────────────────
def run_generic_fit(
    registry: JobRegistry,
    fit_script: str,
    data_path: str,
    output_dir: Optional[str] = None,
    dat_path_var: str = "DAT_PATH",
    out_base_var: str = "OUT_BASE",
    extra_patches: Optional[Dict[str, str]] = None,
    background: bool = True,
    debug: bool = False,
) -> Dict[str, Any]:
    """Run a user-supplied fitting script with path redirection.

    Patches the user's script so it reads from ``data_path`` and writes to
    ``output_dir``, then launches it as a background job. This lets any script
    that follows the ``DAT_PATH`` / ``OUT_BASE`` convention (or uses custom
    variable names supplied via ``dat_path_var`` / ``out_base_var``) plug in
    without modification.

    For scripts that need additional substitutions (e.g. a different topology
    parameter, number of junctions, port impedance), pass ``extra_patches`` as
    a ``{regex_pattern: replacement_line}`` dict — the same format accepted by
    :func:`~comsol_suite.runner.patch_script`.

    Parameters
    ----------
    registry
        Shared :class:`~comsol_suite.jobs.JobRegistry`.
    fit_script
        Absolute path to the fitting Python script.
    data_path
        Absolute path to the S-parameter data file the script should read.
    output_dir
        Where to write results. Defaults to ``runs/<job_id>/fit_out/``.
    dat_path_var
        Variable name in the script that holds the data-file path.
        Defaults to ``DAT_PATH`` (the convention used by ``abcd_fit.py``).
    out_base_var
        Variable name in the script that holds the output root path.
        Defaults to ``OUT_BASE``.
    extra_patches
        Additional ``{regex_pattern: replacement}`` pairs forwarded to
        :func:`~comsol_suite.runner.patch_script` (``require_all=False``).
    background
        Submit as a background job (default) and return ``{job_id, status}``
        immediately.
    debug
        Echo the command line into the job log.

    Returns
    -------
    dict
        ``{job_id, status}`` — poll with ``get_job_result`` for ``output_files``
        and ``figures``.
    """
    cfg = load_config()
    src = Path(fit_script)
    data = Path(data_path)

    if not src.is_file():
        return {"ok": False, "error": f"fit script not found: {src}"}
    if not data.is_file():
        return {"ok": False, "error": f"data file not found: {data}"}

    def worker(job: Job) -> Dict[str, Any]:
        out = Path(output_dir) if output_dir else Path(job.run_dir) / "fit_out"
        out.mkdir(parents=True, exist_ok=True)

        patches: Dict[str, str] = {
            rf"^{re.escape(dat_path_var)}\s*=.*$": (
                f'{dat_path_var} = r"{data.as_posix()}"'
            ),
            rf"^{re.escape(out_base_var)}\s*=.*$": (
                f'{out_base_var} = r"{out.as_posix()}"'
            ),
        }
        if extra_patches:
            patches.update(extra_patches)

        patched = patch_script(
            src,
            Path(job.run_dir) / "_generic_fit_patched.py",
            patches,
            require_all=False,  # custom scripts may omit some vars
        )
        res = run_command(
            [cfg.python_bin, patched],
            log_path=Path(job.log_path),
            cwd=out,
            timeout_s=900,
            debug=debug,
        )
        results = _collect(out, ["*.csv"])
        return {
            "ok": res.ok,
            "output_files": results,
            "figures": _collect(out, ["*.png"]),
            "returncode": res.returncode,
            "duration_s": round(res.duration_s, 2),
            "summary": f"generic_fit finished rc={res.returncode}",
            "error": None if res.ok else "script returned non-zero (see run.log)",
        }

    job = registry.submit("generic_fit", worker, background=background)
    return {"job_id": job.job_id, "status": job.status}



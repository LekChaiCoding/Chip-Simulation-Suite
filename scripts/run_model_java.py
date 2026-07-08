"""
COMSOL Java-model wrapper — compile and run a COMSOL-exported ``.java`` model
file in batch mode, rebuilding the model tree and saving the result as ``.mph``.

Why this exists
---------------
COMSOL's *File → Save As → Model File for Java* export is a chronological
recording of an entire GUI session: every parameter edit, geometry feature,
physics setting — and, crucially, every **solver run** (``model.sol(..)
.runAll()``). Replaying such a file verbatim with ``comsol batch`` re-runs all
of those solves, which can take hours and is pointless when you only want the
model *definition* (geometry + physics + studies) rebuilt from source.

This wrapper:

1. Reads the exported ``.java`` file and comments out every recorded solver
   invocation (``sol*.runAll()``, ``sol*.run()``, ``batch*.run()``,
   ``study*.run()``) unless ``--keep-solvers`` is given. Mesh runs are kept —
   they are cheap relative to solves and some later features reference them.
2. Injects an explicit ``model.save(<out>)`` (plus a ``SAVED_OK`` sentinel
   print) at the end of ``main`` so the run is verifiable, instead of relying
   on batch-mode auto-save behaviour.
3. Writes the transformed source to a job directory under ``runs/``,
   compiles it with ``comsol compile`` and executes it with
   ``comsol batch -inputfile <class>``, streaming all output to a log file.

The original ``.java`` file is never modified.

Typical use (rebuild the U0 full-pattern chip model without solving):

    python run_model_java.py /path/to/U0_full.java \
        --out /path/to/U0_full_rebuilt.mph

Notes
-----
* ``insertFile``/``geom().load`` calls inside the export reference absolute
  paths (part libraries, linked mph files). The run must happen on a machine
  where those paths resolve — this wrapper does not rewrite them, but it does
  scan and print them up front (``--list-deps`` exits after the scan) so a
  missing dependency fails loudly *before* a 10-minute rebuild, not during.
* The public class name must match the file name; the transformed copy keeps
  the original class name and file name inside the job directory.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SUITE_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = SUITE_ROOT / "runs"

# Recorded solver invocations to neutralise (matched against whole statements
# collapsed to one line). Covers full runs and partial ones (runFromTo,
# individual solver-feature runs).
SOLVER_RUN_RE = re.compile(
    r"^model\.(?:sol\(\"[^\"]+\"\)(?:\.feature\(\"[^\"]+\"\))?\.(?:runAll|runFromTo|run)|"
    r"batch\(\"[^\"]+\"\)\.run|study\(\"[^\"]+\"\)\.run)\(")

# Recorded file writes (results-table exports etc.). These target the ORIGINAL
# author's directories and would overwrite real data with tables from an
# unsolved model — always stripped. (An explicit model.save is injected later.)
FILE_WRITE_RE = re.compile(r"\.save\(\"")

# Result-node statements (plot groups, tables, numerical evaluations, exports).
# In definition-only mode ALL of these are stripped: evaluations need solution
# data ("Undefined variable: lambda"), and even plain settings like
# set("looplevel", ...) validate against parametric solution structure that
# does not exist without a solve. Result nodes contribute nothing to the model
# definition (geometry/physics/mesh/studies), so dropping them is safe.
RESULT_EVAL_RE = re.compile(r"^model\.result\b")

# Component geometry / mesh build triggers. These must be KEPT (physics
# selections in exports use hardcoded entity numbers that only validate
# against built geometry) but made FAULT-TOLERANT: exports record parameter
# renames and the propagation of those renames to dependent expressions as
# separate statements that can be far apart, and a build inside that window
# sees stale references ("Unknown model parameter" on a selection) and would
# otherwise abort the whole replay. Each build is wrapped in try/catch; a
# failed intermediate build is reported and skipped, and a later (or the
# injected final) build brings the geometry current.
GEOM_MESH_RUN_RE = re.compile(
    r"^model\.component\(\"[^\"]+\"\)\.(?:geom|mesh)\(\"[^\"]+\"\).*\.run\((?:\"[^\"]*\")?\);$")

# External files the replay will need (part libraries, linked models, GDS...).
DEP_PATTERN = re.compile(r'"(/[^"]+\.(?:mph|gds|dxf|txt|csv))"')


def find_class_name(source: str) -> str:
    m = re.search(r"public\s+class\s+(\w+)", source)
    if not m:
        raise SystemExit("error: no 'public class' declaration found in input")
    return m.group(1)


def strip_statements(source: str, *, strip_solvers: bool) -> tuple[str, int, int]:
    """Comment out recorded solver runs and file-write statements.

    Statements in COMSOL exports can span several lines (method chains with the
    ``.save("...")`` on a continuation line), so lines are buffered until the
    terminating ``;`` and the whole statement is handled as a unit.
    Returns (new_source, n_solver_stripped, n_write_stripped, n_build_wrapped).
    """
    out_lines: list[str] = []
    buf: list[str] = []
    n_solve = n_write = 0
    n_wrap = [0]

    def flush() -> None:
        nonlocal n_solve, n_write
        if not buf:
            return
        joined = " ".join(l.strip() for l in buf)
        if strip_solvers and (SOLVER_RUN_RE.match(joined)
                              or RESULT_EVAL_RE.match(joined)):
            n_solve += 1
            out_lines.extend(f"// SKIPPED (no solve): {l.rstrip()}\n" for l in buf)
        elif FILE_WRITE_RE.search(joined):
            n_write += 1
            out_lines.extend(f"// SKIPPED (file write): {l.rstrip()}\n" for l in buf)
        elif strip_solvers and GEOM_MESH_RUN_RE.match(joined):
            n_wrap[0] += 1
            indent = buf[0][: len(buf[0]) - len(buf[0].lstrip())]
            out_lines.append(f"{indent}try {{\n")
            out_lines.extend(buf)
            out_lines.append(
                f'{indent}}} catch (Exception e) {{ System.out.println('
                f'"BUILD_SKIP: " + e.getMessage()); }}\n')
        else:
            out_lines.extend(buf)
        buf.clear()

    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        # only buffer inside statement bodies; structural lines pass through
        if buf or (stripped.startswith("model") and not stripped.endswith("{")):
            buf.append(line)
            if stripped.endswith(";"):
                flush()
        else:
            out_lines.append(line)
    flush()
    return "".join(out_lines), n_solve, n_write, n_wrap[0]


def inject_save(source: str, out_mph: Path) -> str:
    """Make main() capture the final model and save it explicitly.

    Exports end main() with a chain like ``model = runN(model); ... runM(model);``
    (last call's return value discarded) or, for short sessions, just ``run();``.
    We rewrite the last run call to keep the reference, then append the save.
    """
    # final geometry build for every component geometry seen in the source —
    # deferred to here so all parameter renames/re-sets have been replayed.
    # A build failure is reported but does not prevent the save (the model
    # definition is still valuable for diagnosis / downstream re-runs).
    geoms = sorted(set(re.findall(
        r'model\.component\("([^"]+)"\)\.geom\("([^"]+)"\)', source)))
    build_lines = "".join(
        "    try {\n"
        f'      model.component("{c}").geom("{g}").run("fin");\n'
        f'      System.out.println("GEOM_BUILD_OK {c}/{g}");\n'
        "    } catch (Exception e) {\n"
        f'      System.out.println("GEOM_BUILD_FAILED {c}/{g}: " + e.getMessage());\n'
        "    }\n"
        for c, g in geoms
    )
    save_block = (
        build_lines
        + "    try {\n"
        f'      model.save("{out_mph}");\n'
        '      System.out.println("SAVED_OK");\n'
        "    } catch (Exception e) {\n"
        "      e.printStackTrace();\n"
        "      System.exit(1);\n"
        "    }\n"
        "  }"
    )
    main_m = re.search(r"public\s+static\s+void\s+main\([^)]*\)\s*\{", source)
    if not main_m:
        raise SystemExit("error: no main() found in input java")
    head, body = source[: main_m.end()], source[main_m.end():]

    # last `runN(model);` (return value discarded) → capture it
    last_call = None
    for last_call in re.finditer(r"^(\s*)(run\d*)\(model\);\s*$", body, flags=re.M):
        pass
    if last_call:
        indent, fn = last_call.groups()
        body = (
            body[: last_call.start()]
            + f"{indent}model = {fn}(model);\n"
            + body[last_call.end():].lstrip("\n")
        )
    elif re.search(r"^\s*run\(\);\s*$", body, flags=re.M):
        body = re.sub(r"^(\s*)run\(\);\s*$", r"\1Model model = run();\n", body,
                      count=1, flags=re.M)
    elif not re.search(r"\bModel\s+model\s*=", body):
        raise SystemExit("error: could not locate a model reference in main()")

    # replace the closing brace of main (first lone '}' after the body start)
    brace = re.search(r"\n  \}", body)
    if not brace:
        raise SystemExit("error: could not find end of main()")
    body = body[: brace.start()] + "\n" + save_block + body[brace.end():]
    return head + body


def scan_dependencies(source: str) -> tuple[list[Path], list[Path]]:
    """Return (inputs, outputs): absolute file paths read vs written by the export.

    A path on a ``.save("...")`` statement is an output (write target); all
    other referenced paths are treated as inputs that must exist.
    """
    outputs: set[Path] = set()
    for stmt in source.split(";"):
        if FILE_WRITE_RE.search(stmt):
            outputs.update(Path(p) for p in DEP_PATTERN.findall(stmt))
    inputs = {Path(p) for p in DEP_PATTERN.findall(source)} - outputs
    return sorted(inputs), sorted(outputs)


def run_streaming(cmd: list[str], log_path: Path, timeout_s: int) -> int:
    """Run cmd, tee combined output to log file and stdout, enforce timeout."""
    print(f"$ {' '.join(cmd)}")
    with open(log_path, "a") as log:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        start = time.time()
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            if time.time() - start > timeout_s:
                proc.kill()
                raise SystemExit(f"error: timed out after {timeout_s}s ({cmd[0]})")
        return proc.wait()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("java_file", type=Path, help="COMSOL-exported .java model file")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .mph path (default: runs/<job>/<Class>_rebuilt.mph)")
    ap.add_argument("--keep-solvers", action="store_true",
                    help="do NOT strip recorded solver runs (may take hours)")
    ap.add_argument("--list-deps", action="store_true",
                    help="only scan and print external file dependencies, then exit")
    ap.add_argument("--comsol-bin", default="comsol", help="comsol launcher binary")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="per-step timeout in seconds (compile / batch run)")
    args = ap.parse_args()

    src_path = args.java_file.resolve()
    if not src_path.is_file():
        raise SystemExit(f"error: {src_path} not found")
    source = src_path.read_text()
    cls = find_class_name(source)

    inputs, outputs = scan_dependencies(source)
    missing = [d for d in inputs if not d.exists()]
    print(f"input file dependencies ({len(inputs)}):")
    for d in inputs:
        print(f"  [{'OK' if d.exists() else 'MISSING'}] {d}")
    print(f"recorded write targets ({len(outputs)}) — statements will be stripped:")
    for d in outputs:
        print(f"  [stripped] {d}")
    if args.list_deps:
        sys.exit(0)
    if missing:
        raise SystemExit(f"error: {len(missing)} dependency file(s) missing — aborting "
                         "before an expensive rebuild. Fix paths or copy files first.")

    job = time.strftime(f"java_rebuild_{cls}_%Y%m%d_%H%M%S")
    job_dir = RUNS_DIR / job
    job_dir.mkdir(parents=True, exist_ok=True)
    out_mph = (args.out or job_dir / f"{cls}_rebuilt.mph").resolve()
    out_mph.parent.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "run.log"

    stripped, n_solve, n_write, n_wrap = strip_statements(
        source, strip_solvers=not args.keep_solvers)
    final_src = inject_save(stripped, out_mph)

    work_java = job_dir / f"{cls}.java"
    work_java.write_text(final_src)
    print(f"job dir : {job_dir}")
    print(f"stripped: {n_solve} solver/result statement(s), "
          f"{n_write} recorded file write(s); "
          f"{n_wrap} geometry/mesh build(s) made fault-tolerant")
    print(f"output  : {out_mph}")

    rc = run_streaming([args.comsol_bin, "compile", str(work_java)], log_path,
                       args.timeout)
    if rc != 0:
        raise SystemExit(f"error: comsol compile failed (exit {rc}), see {log_path}")

    work_class = work_java.with_suffix(".class")
    if not work_class.exists():
        raise SystemExit(f"error: {work_class} not produced by comsol compile")

    rc = run_streaming([args.comsol_bin, "batch", "-inputfile", str(work_class)],
                       log_path, args.timeout)
    saved = out_mph.exists() and "SAVED_OK" in log_path.read_text()
    if rc != 0 or not saved:
        raise SystemExit(f"error: batch run failed (exit {rc}, saved={saved}), "
                         f"see {log_path}")
    print(f"OK: model rebuilt and saved → {out_mph} "
          f"({out_mph.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

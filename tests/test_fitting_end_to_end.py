"""End-to-end fitting tests: sequential ABCD fit, parallel ABCD fit,
and generic custom-fit tool — all using the real bridge/003 sweep data.

The bridge/003 ``stub_length_sweep.dat`` stands in for what COMSOL emits, so
these tests prove the S-parameters -> circuit-extraction half of the pipeline
using the user's real data. All runs are synchronous (background=False) for
determinism in CI.
"""

from __future__ import annotations

import csv

from comsol_suite.config import load_config
from comsol_suite.jobs import JobRegistry
from comsol_suite.tools.fitting import run_abcd_fit, run_abcd_fit_parallel, run_generic_fit


def _check_abcd_csv(csv_path: str) -> None:
    """Common sanity checks applied to both sequential and parallel results."""
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, f"results CSV is empty: {csv_path}"

    for r in rows:
        cg = float(r["Cg_fit_fF"])
        assert cg == cg and cg > 0, f"bad Cg: {cg}"  # not NaN, positive

    # Canonical physics check: topoA/fitA must give Z0 ≈ 40-60 Ω.
    canonical = [r for r in rows
                 if r["topology"] == "topoA" and r["fit_method"] == "fitA"]
    assert canonical, "no topoA/fitA rows present"
    for r in canonical:
        z0 = float(r["Z0_implied_ohm"])
        assert 40.0 < z0 < 60.0, (
            f"canonical fit Z0 off 50 ohm at stub {r['stub_um']}: {z0}"
        )


def test_abcd_fit_produces_sane_results(tmp_path):
    """Sequential ABCD fit: all stubs in one subprocess."""
    cfg = load_config()
    registry = JobRegistry(tmp_path / "runs")

    out = run_abcd_fit(registry, output_dir=str(tmp_path / "abcd"),
                       background=False, debug=True)
    job = registry.get(out["job_id"])
    assert job is not None
    assert job.status == "completed", f"fit failed: {job.error}"

    results_csv = [p for p in job.result["output_files"]
                   if p.endswith("abcd_fit_results.csv")]
    assert results_csv, "no abcd_fit_results.csv produced"
    _check_abcd_csv(results_csv[0])


def test_abcd_fit_single_stub(tmp_path):
    """run_abcd_fit with stub_filter_um processes exactly one stub."""
    registry = JobRegistry(tmp_path / "runs")

    out = run_abcd_fit(registry, output_dir=str(tmp_path / "single"),
                       stub_filter_um=300.0, background=False, debug=True)
    job = registry.get(out["job_id"])
    assert job is not None
    assert job.status == "completed", f"single-stub fit failed: {job.error}\n{job.result}"

    results_csv = [p for p in job.result["output_files"]
                   if p.endswith("abcd_fit_results.csv")]
    assert results_csv, "no abcd_fit_results.csv for single stub"

    with open(results_csv[0], newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "single-stub CSV is empty"
    stubs_seen = {float(r["stub_um"]) for r in rows}
    assert stubs_seen == {300.0}, f"unexpected stubs in filtered run: {stubs_seen}"


def test_abcd_fit_parallel_produces_sane_merged_results(tmp_path):
    """Parallel ABCD fit: N concurrent subprocesses, merged CSV."""
    registry = JobRegistry(tmp_path / "runs")

    out = run_abcd_fit_parallel(
        registry,
        output_dir=str(tmp_path / "parallel"),
        background=False,  # wait for all stubs inside the supervisor
        debug=True,
    )
    assert out["n_stubs"] == 6, f"expected 6 stubs in bridge/003, got {out['n_stubs']}"

    job = registry.get(out["job_id"])
    assert job is not None
    assert job.status == "completed", f"parallel fit supervisor failed: {job.error}"

    result = job.result
    assert result.get("ok"), f"parallel fit not ok: {result}"
    assert result.get("merged_csv"), "no merged_csv in result"

    # All 6 stubs must have succeeded.
    stubs_ok = result["stubs_ok"]
    assert all(stubs_ok.values()), f"some stubs failed: {stubs_ok}"

    # The merged CSV must contain valid rows for every stub.
    _check_abcd_csv(result["merged_csv"])
    with open(result["merged_csv"], newline="") as fh:
        rows = list(csv.DictReader(fh))
    stubs_seen = {float(r["stub_um"]) for r in rows}
    assert len(stubs_seen) == 6, f"not all stubs in merged CSV: {stubs_seen}"


def test_run_generic_fit_runs_abcd_script(tmp_path):
    """run_generic_fit can drive the existing abcd_fit.py as a custom script."""
    cfg = load_config()
    registry = JobRegistry(tmp_path / "runs")

    # Use abcd_fit.py as the 'custom' script — tests the generic path end-to-end.
    out = run_generic_fit(
        registry,
        fit_script=str(cfg.script("abcd_fit")),
        data_path=str(cfg.datum("bridge003_sweep")),
        output_dir=str(tmp_path / "generic_out"),
        dat_path_var="DAT_PATH",
        out_base_var="OUT_BASE",
        background=False,
        debug=True,
    )
    job = registry.get(out["job_id"])
    assert job is not None
    assert job.status == "completed", f"generic fit failed: {job.error}"

    results_csv = [p for p in job.result["output_files"]
                   if p.endswith("abcd_fit_results.csv")]
    assert results_csv, "generic_fit produced no abcd_fit_results.csv"
    _check_abcd_csv(results_csv[0])

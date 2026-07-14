"""Dry-run / unit tests for comsol.validate_geometry.

No live COMSOL connection required — dry_run=True never subprocesses
anything.
"""

from __future__ import annotations

from pathlib import Path

from comsol_suite.jobs import JobRegistry
from comsol_suite.tools import comsol


def _write_checker(path: Path, *, positional: bool) -> None:
    if positional:
        path.write_text(
            "import sys\n"
            "def main() -> int:\n"
            "    print(sys.argv[1])\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )
    else:
        path.write_text(
            "MPH_PATH = '/default.mph'\n"
            "def main() -> int:\n"
            "    print(MPH_PATH)\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    import sys; sys.exit(main())\n"
        )


def test_validate_geometry_dry_run_variable_convention(tmp_path):
    """Dry-run with mph_path_var set reports the patch plan; nothing runs."""
    checker = tmp_path / "checker.py"
    _write_checker(checker, positional=False)
    mph = tmp_path / "model.mph"
    mph.write_text("")

    reg = JobRegistry(tmp_path / "runs")
    out = comsol.validate_geometry(
        reg, mph_path=str(mph), checker_script=str(checker),
        mph_path_var="MPH_PATH", dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tool"] == "validate_geometry"
    patches = out["patches_applied"]
    assert any("MPH_PATH" in k for k in patches), f"MPH_PATH not patched: {patches}"
    assert str(mph) in patches[next(k for k in patches if "MPH_PATH" in k)]


def test_validate_geometry_dry_run_positional_convention(tmp_path):
    """mph_path_var=None appends mph_path as the first positional arg."""
    checker = tmp_path / "checker_positional.py"
    _write_checker(checker, positional=True)
    mph = tmp_path / "model.mph"
    mph.write_text("")

    reg = JobRegistry(tmp_path / "runs")
    out = comsol.validate_geometry(
        reg, mph_path=str(mph), checker_script=str(checker),
        mph_path_var=None, extra_args=["--format", "arc"], dry_run=True,
    )
    assert out["dry_run"] is True
    argv = out["would_run"]
    assert str(mph) in argv
    assert "--format" in argv and "arc" in argv
    assert out["patches_applied"] == {}


def test_validate_geometry_rejects_missing_checker(tmp_path):
    mph = tmp_path / "model.mph"
    mph.write_text("")
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.validate_geometry(
        reg, mph_path=str(mph),
        checker_script=str(tmp_path / "nonexistent.py"), dry_run=True,
    )
    assert out.get("ok") is False
    assert "not found" in out["error"]


def test_validate_geometry_rejects_missing_mph(tmp_path):
    checker = tmp_path / "checker.py"
    _write_checker(checker, positional=False)
    reg = JobRegistry(tmp_path / "runs")
    out = comsol.validate_geometry(
        reg, mph_path=str(tmp_path / "nonexistent.mph"),
        checker_script=str(checker), dry_run=True,
    )
    assert out.get("ok") is False
    assert "not found" in out["error"]

"""Unit tests for the poka-yoke path guard (comsol_suite.containment).

``ensure_contained`` resolves the allowed root through the cached
:func:`comsol_suite.config.load_config`, so each test that redirects
``CHIP_SIM_ROOT`` must clear that cache (and clear it again on teardown so
later tests see the real machine config, not the tmp root).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comsol_suite import config as suite_config
from comsol_suite.containment import ensure_contained


@pytest.fixture
def tmp_root(monkeypatch, tmp_path: Path):
    """A temporary CHIP_SIM_ROOT the guard resolves against."""
    root = tmp_path / "chip_sim_root"
    root.mkdir()
    monkeypatch.setenv("CHIP_SIM_ROOT", str(root))
    suite_config.load_config.cache_clear()
    yield root
    # monkeypatch restores the env after the test; drop the cached config
    # built from the tmp root so later tests re-resolve the real one.
    suite_config.load_config.cache_clear()


def test_accepts_path_inside_root(tmp_root: Path):
    inside = tmp_root / "models" / "tile.mph"
    resolved = ensure_contained(str(inside), arg="mph_path", tool="t")
    assert resolved == inside.resolve()


def test_accepts_root_itself_and_nonexistent_children(tmp_root: Path):
    # The guard is about containment, not existence — output paths that do
    # not exist yet must pass.
    assert ensure_contained(str(tmp_root), arg="p", tool="t") == tmp_root.resolve()
    ghost = tmp_root / "does" / "not" / "exist.gds"
    assert ensure_contained(str(ghost), arg="p", tool="t") == ghost.resolve()


def test_rejects_path_outside_root(tmp_root: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere" / "leak.gds"
    with pytest.raises(ValueError) as exc:
        ensure_contained(str(outside), arg="gds_path", tool="verify_cad")
    msg = str(exc.value)
    # The error must name the tool, the argument, the path, and the root.
    assert "verify_cad" in msg
    assert "gds_path" in msg
    assert str(outside) in msg
    assert str(tmp_root.resolve()) in msg


def test_rejects_dotdot_traversal_that_escapes(tmp_root: Path):
    # Textually under the root, but .. components resolve outside it.
    sneaky = tmp_root / "sub" / ".." / ".." / "escaped.yaml"
    with pytest.raises(ValueError, match="outside the allowed root"):
        ensure_contained(str(sneaky), arg="yaml_path", tool="design_params_write")


def test_accepts_dotdot_traversal_that_stays_inside(tmp_root: Path):
    # .. that resolves back inside the root is fine.
    wobbly = tmp_root / "a" / ".." / "b" / "file.csv"
    assert ensure_contained(str(wobbly), arg="csv_path", tool="t") == \
        (tmp_root / "b" / "file.csv").resolve()


def test_rejects_sibling_with_root_as_prefix(tmp_root: Path):
    # /x/chip_sim_root_evil must not pass a naive startswith() check on
    # /x/chip_sim_root — is_relative_to() gets this right.
    evil = tmp_root.parent / (tmp_root.name + "_evil") / "f.gds"
    with pytest.raises(ValueError):
        ensure_contained(str(evil), arg="p", tool="t")


def test_rejects_symlink_escape(tmp_root: Path, tmp_path: Path):
    # A symlink inside the root pointing outside must be rejected, since
    # resolve() follows it.
    target = tmp_path / "outside_dir"
    target.mkdir()
    link = tmp_root / "innocent_link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(ValueError):
        ensure_contained(str(link / "f.mph"), arg="mph_path", tool="t")


def test_expands_user_home(tmp_root: Path, monkeypatch):
    # ~ paths are expanded before the check; a home dir outside the tmp
    # root must therefore be rejected.
    with pytest.raises(ValueError):
        ensure_contained("~/leak.gds", arg="gds_path", tool="t")

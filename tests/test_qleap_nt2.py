from pathlib import Path
import sys

import pytest

SUITE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE_ROOT))

from comsol_suite.tools import qleap


def test_nt2_probe_dry_run_builds_bounded_wrapper_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qleap, "_nt2_dir", lambda: tmp_path)
    monkeypatch.setattr(qleap, "_python_bin", lambda: "/venv/python")

    result = qleap.qleap_run_nt2_probe(
        registry=None,
        tag="r1lend590_repro",
        param_overrides={"g_readout1_l_end": "590[um]"},
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["tool"] == "qleap_run_nt2_probe"
    assert "--set" in result["would_run"]
    assert "g_readout1_l_end=590[um]" in result["would_run"]


def test_nt2_probe_rejects_unsafe_tag() -> None:
    with pytest.raises(ValueError, match="tag"):
        qleap.qleap_run_nt2_probe(
            registry=None,
            tag="../escape",
            dry_run=True,
        )

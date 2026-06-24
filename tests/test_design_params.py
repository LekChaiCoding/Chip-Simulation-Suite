"""Validation tests for design_params.py using the AlNtransmon reference yaml.

Tests read operations use the (read-only) COPY - AlNtransmon/design_params.yaml
as a fixture of known values. Write operations use tmp_path to avoid mutating
the reference file.
"""

from __future__ import annotations

import os
import yaml
import pytest

from comsol_suite.tools.design_params import (
    read_param,
    write_param,
    get_session_plan,
)

# Absolute path to the completed reference design
REFERENCE_YAML = (
    "/mnt/smb/HSS/users/Alex/Chip Simulation"
    "/COPY - AlNtransmon/design_params.yaml"
)


# ── read_param against known AlNtransmon values ───────────────────────────────

def test_read_qubit_d_q_Q0():
    """Q0 qubit pad diameter is 350 µm."""
    val = read_param(REFERENCE_YAML, "design_Q0.qubit.d_q")
    assert abs(val - 350.0) < 0.1, f"d_q Q0: expected 350 µm, got {val}"


def test_read_qubit_d_q_Q1():
    """Q1 qubit pad diameter is 271 µm."""
    val = read_param(REFERENCE_YAML, "design_Q1.qubit.d_q")
    assert abs(val - 271.0) < 0.1, f"d_q Q1: expected 271 µm, got {val}"


def test_read_qubit_d_q_Q2():
    """Q2 qubit pad diameter is 271 µm (same unit cell as Q1 in this design)."""
    val = read_param(REFERENCE_YAML, "design_Q2.qubit.d_q")
    assert abs(val - 271.0) < 0.1, f"d_q Q2: expected 271 µm, got {val}"


def test_read_delta_angle_coupler_Q0():
    """Q0 coupler angle is 42.3°, giving g = 180 MHz per yaml comment."""
    val = read_param(REFERENCE_YAML, "design_Q0.qr_coupler.delta_angle_coupler")
    assert abs(val - 42.3) < 0.1, f"Q0 delta_angle_coupler: expected 42.3, got {val}"


def test_read_delta_angle_coupler_Q1():
    """Q1 coupler angle is 42.3° in this reference design."""
    val = read_param(REFERENCE_YAML, "design_Q1.qr_coupler.delta_angle_coupler")
    assert abs(val - 42.3) < 0.1, f"Q1 delta_angle_coupler: expected 42.3, got {val}"


def test_read_spiral_turns():
    """All qubits share spiral_turns = 1.75."""
    for q in range(4):
        val = read_param(REFERENCE_YAML, f"design_Q{q}.readout_port.spiral_turns")
        assert abs(val - 1.75) < 0.01, f"Q{q} spiral_turns: expected 1.75, got {val}"


def test_read_filter_l_end_Q0():
    """Q0 filter l_end = 450 µm."""
    val = read_param(REFERENCE_YAML, "design_Q0.filter.l_end")
    assert abs(val - 450.0) < 0.1


def test_read_LJJ_list():
    """design_common.LJJ_list has 16 elements, first = 11.2 nH."""
    ljj = read_param(REFERENCE_YAML, "design_common.LJJ_list")
    assert isinstance(ljj, list)
    assert len(ljj) == 16
    assert abs(ljj[0] - 11.2) < 0.01


def test_read_missing_key_raises():
    """Reading a nonexistent key raises KeyError."""
    with pytest.raises(KeyError):
        read_param(REFERENCE_YAML, "design_Q0.nonexistent.param")


def test_read_missing_file_raises():
    """Reading from a nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_param("/nonexistent/path/params.yaml", "some.key")


# ── write_param to a temp yaml ────────────────────────────────────────────────

def test_write_creates_file(tmp_path):
    """write_param creates the file if it doesn't exist."""
    yaml_file = str(tmp_path / "params.yaml")
    assert not os.path.exists(yaml_file)
    write_param(yaml_file, "design_Q0.qubit.d_q", 271.0)
    assert os.path.exists(yaml_file)


def test_write_then_read_roundtrip(tmp_path):
    """A written value must be read back identically."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "design_Q0.readout_resonator.l_slider_single", 234.2)
    val = read_param(yaml_file, "design_Q0.readout_resonator.l_slider_single")
    assert abs(val - 234.2) < 1e-9


def test_write_preserves_existing_keys(tmp_path):
    """Writing one key does not destroy adjacent keys."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "design_Q0.qubit.d_q", 350.0)
    write_param(yaml_file, "design_Q0.qubit.LJJ", 11.2)
    assert abs(read_param(yaml_file, "design_Q0.qubit.d_q") - 350.0) < 1e-9
    assert abs(read_param(yaml_file, "design_Q0.qubit.LJJ") - 11.2) < 1e-9


def test_write_overwrites_existing_value(tmp_path):
    """A second write to the same key replaces the first value."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "design_Q0.filter.l_end", 400.0)
    write_param(yaml_file, "design_Q0.filter.l_end", 450.0)
    val = read_param(yaml_file, "design_Q0.filter.l_end")
    assert abs(val - 450.0) < 1e-9


def test_write_creates_nested_path(tmp_path):
    """write_param creates intermediate dicts automatically."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "a.b.c.d", 42)
    val = read_param(yaml_file, "a.b.c.d")
    assert val == 42


def test_write_is_atomic(tmp_path):
    """After write, no .tmp file should remain (atomic replace succeeded)."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "design_Q0.qubit.d_q", 271.0)
    tmp_file = yaml_file + ".tmp"
    assert not os.path.exists(tmp_file), ".tmp file left behind after write"


def test_write_produces_valid_yaml(tmp_path):
    """Output file must be parseable by yaml.safe_load."""
    yaml_file = str(tmp_path / "params.yaml")
    write_param(yaml_file, "design_Q0.qubit.d_q", 350.0)
    write_param(yaml_file, "design_common.fr_list", [6.56, 6.64, 6.72, 6.80])
    with open(yaml_file) as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    assert data["design_Q0"]["qubit"]["d_q"] == 350.0


# ── get_session_plan ──────────────────────────────────────────────────────────

# Stage map reflecting the AlNtransmon D0–D5 pipeline
ALNTRANSMON_STAGE_MAP = {
    "D0":  ["design_Q0.qubit.d_q", "design_Q1.qubit.d_q",
             "design_Q2.qubit.d_q", "design_Q3.qubit.d_q"],
    "D1":  ["design_Q0.qr_coupler.delta_angle_coupler",
             "design_Q1.qr_coupler.delta_angle_coupler"],
    "D1.1":["design_Q0.drive_spokes.n", "design_Q1.drive_spokes.n"],
    "D2":  ["design_Q0.readout_resonator.l_slider_single",
             "design_Q1.readout_resonator.l_slider_single"],
    "D3":  ["design_Q0.filter.l_end", "design_Q1.filter.l_end"],
    "D4":  ["design_Q0.filter.l_slider_single",
             "design_Q1.filter.l_slider_single"],
    "D5":  ["design_Q0.readout_port.spiral_turns",
             "design_Q1.readout_port.spiral_turns"],
}


def test_session_plan_all_done_on_reference_yaml():
    """AlNtransmon reference yaml reports all_done=True (full pipeline complete)."""
    plan = get_session_plan(REFERENCE_YAML, ALNTRANSMON_STAGE_MAP)
    assert plan["all_done"] is True
    assert plan["next_stage"] is None
    assert plan["missing_params"] == []
    assert set(plan["completed_stages"]) == set(ALNTRANSMON_STAGE_MAP.keys())


def test_session_plan_detects_incomplete_stage(tmp_path):
    """A partial yaml (only D0 done) returns D1 as next stage."""
    yaml_file = str(tmp_path / "params.yaml")
    # Only populate D0 keys
    write_param(yaml_file, "design_Q0.qubit.d_q", 350.0)
    write_param(yaml_file, "design_Q1.qubit.d_q", 271.0)
    write_param(yaml_file, "design_Q2.qubit.d_q", 214.0)
    write_param(yaml_file, "design_Q3.qubit.d_q", 350.0)

    plan = get_session_plan(yaml_file, ALNTRANSMON_STAGE_MAP)
    assert plan["all_done"] is False
    assert plan["next_stage"] == "D1"
    assert "D0" in plan["completed_stages"]
    assert "design_Q0.qr_coupler.delta_angle_coupler" in plan["missing_params"]


def test_session_plan_empty_yaml_returns_first_stage(tmp_path):
    """An empty yaml returns D0 as the first stage to run."""
    yaml_file = str(tmp_path / "empty.yaml")
    plan = get_session_plan(yaml_file, ALNTRANSMON_STAGE_MAP)
    assert plan["all_done"] is False
    assert plan["next_stage"] == "D0"
    assert plan["completed_stages"] == []


def test_session_plan_scope_string_mentions_stage(tmp_path):
    """session_scope string mentions the stage name."""
    yaml_file = str(tmp_path / "empty.yaml")
    plan = get_session_plan(yaml_file, ALNTRANSMON_STAGE_MAP)
    assert "D0" in plan["session_scope"]

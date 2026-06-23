"""Generic design parameter manager for hierarchical YAML pipelines.

Provides atomic read/write access to nested YAML parameter files and session
state inference for any design pipeline that uses a YAML file as its single
source of truth (e.g. an AlNtransmon chip, a resonator array, a TWPA).

The YAML structure is completely user-defined — this module imposes no schema.
Keys are addressed by dot-separated paths, e.g.:

    "design_Q0.readout_resonator.l_slider_single"
    "res_bank.resonator_A.coupling_gap_um"
    "chip.global.substrate_thickness_um"

Writes are atomic (write to .tmp then os.replace) so a crash mid-write never
leaves a partial file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_yaml(yaml_path: str) -> Dict[str, Any]:
    """Load a YAML file, returning an empty dict if the file does not exist."""
    p = Path(yaml_path)
    if not p.is_file():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _save_yaml_atomic(yaml_path: str, data: Dict[str, Any]) -> None:
    """Write *data* to *yaml_path* atomically via a .tmp file.

    If the write fails partway through the original file is untouched.
    """
    p = Path(yaml_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(yaml_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)
    os.replace(tmp, str(yaml_path))


def _get_nested(data: Dict[str, Any], key_path: str) -> Any:
    """Traverse *data* by dot-separated *key_path*. Raises KeyError if missing."""
    keys = key_path.split(".")
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(
                f"Key '{k}' not found in path '{key_path}' "
                f"(available at this level: {list(node.keys()) if isinstance(node, dict) else '<not a dict>'})"
            )
        node = node[k]
    return node


def _set_nested(data: Dict[str, Any], key_path: str, value: Any) -> None:
    """Set a value in *data* at the dot-separated *key_path*, creating dicts as needed."""
    keys = key_path.split(".")
    node = data
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


# ── Public API ────────────────────────────────────────────────────────────────

def read_param(yaml_path: str, key_path: str) -> Any:
    """Read a single value from a hierarchical design parameter YAML.

    Parameters
    ----------
    yaml_path : str
        Absolute or relative path to the YAML file.
    key_path : str
        Dot-separated path to the target key, e.g.
        ``"design_Q0.readout_resonator.l_slider_single"``.

    Returns
    -------
    Any
        The value stored at that key.

    Raises
    ------
    KeyError
        If any segment of *key_path* is missing in the file.
    FileNotFoundError
        If *yaml_path* does not exist.
    """
    p = Path(yaml_path)
    if not p.is_file():
        raise FileNotFoundError(f"Design params file not found: {yaml_path}")
    data = _load_yaml(yaml_path)
    return _get_nested(data, key_path)


def write_param(yaml_path: str, key_path: str, value: Any) -> None:
    """Write a single value to a hierarchical design parameter YAML atomically.

    Creates the YAML file and any missing parent keys if they do not exist.
    Preserves all existing keys and values in the file.

    Parameters
    ----------
    yaml_path : str
        Absolute or relative path to the YAML file.
    key_path : str
        Dot-separated path to the target key, e.g.
        ``"design_Q0.readout_resonator.l_slider_single"``.
    value : Any
        Value to write (must be YAML-serialisable: float, int, str, list, dict, None).
    """
    data = _load_yaml(yaml_path)
    _set_nested(data, key_path, value)
    _save_yaml_atomic(yaml_path, data)


def get_session_plan(
    yaml_path: str,
    stage_map: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Determine which pipeline stages are complete and what to do next.

    Reads the current YAML state and checks each stage in *stage_map* order.
    A stage is considered **complete** when ALL of its key paths have non-None
    values in the YAML. The function returns the first incomplete stage as the
    recommended next action.

    Parameters
    ----------
    yaml_path : str
        Path to the design parameter YAML file.
    stage_map : dict
        Ordered mapping of ``stage_name → list_of_key_paths``.  The order
        determines pipeline sequence — earlier entries are checked first.

        Example for an AlNtransmon chip::

            {
                "D0": ["design_Q0.qubit.d_q", "design_Q1.qubit.d_q"],
                "D1": ["design_Q0.qr_coupler.delta_angle_coupler"],
                "D2": ["design_Q0.readout_resonator.l_slider_single"],
            }

        Example for a resonator bank::

            {
                "freq_tune":  ["res_A.l_slider_um", "res_B.l_slider_um"],
                "Q_tune":     ["res_A.coupling_gap_um"],
            }

    Returns
    -------
    dict with keys:
        ``completed_stages`` : list of stage names already done
        ``next_stage``       : name of the next stage to run (None if all done)
        ``missing_params``   : list of key paths not yet set in the next stage
        ``all_done``         : True when every stage is complete
        ``session_scope``    : human-readable description of recommended action
    """
    data = _load_yaml(yaml_path)

    completed: List[str] = []
    next_stage: Optional[str] = None
    missing_params: List[str] = []

    for stage_name, key_paths in stage_map.items():
        stage_missing = []
        for kp in key_paths:
            try:
                val = _get_nested(data, kp)
                if val is None:
                    stage_missing.append(kp)
            except KeyError:
                stage_missing.append(kp)

        if stage_missing:
            # First incomplete stage — this is the next one to run
            next_stage = stage_name
            missing_params = stage_missing
            break
        else:
            completed.append(stage_name)

    all_done = next_stage is None

    if all_done:
        scope = "All pipeline stages complete. Ready for final assembly or tapeout."
    else:
        n_missing = len(missing_params)
        scope = (
            f"Run stage '{next_stage}': determine {n_missing} parameter(s) — "
            + ", ".join(missing_params[:3])
            + (" ..." if n_missing > 3 else "")
        )

    return {
        "completed_stages": completed,
        "next_stage": next_stage,
        "missing_params": missing_params,
        "all_done": all_done,
        "session_scope": scope,
    }

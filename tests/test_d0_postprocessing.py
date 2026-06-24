"""Phase 2 validation: D0 post-processing against AlNtransmon reference.

The AlNtransmon D0 notebook (COPY - AlNtransmon/D0_capacitance_extraction.ipynb)
solved COMSOL eigenfrequency studies for three qubit pad diameters and reported:

    d_q=150 µm  → f0_COMSOL = 6.887 GHz, fge = 6.452 GHz, anh = -481 MHz
    d_q=250 µm  → f0_COMSOL = 5.168 GHz, fge = 4.928 GHz, anh = -258 MHz
    d_q=350 µm  → f0_COMSOL = 4.215 GHz, fge = 4.057 GHz, anh = -167 MHz

All three use LJJ = 11.2 nH (Q0 design value from design_common.LJJ_list[0]).

This test verifies that compute_circuit_params() (the Suite's Phase-1 physics
library) reproduces those notebook results within 1%, confirming that the
circuit_physics module is correctly wired up to the D0 post-processing chain.

No COMSOL connection is needed — the eigenfrequencies are used as known inputs.
"""

from __future__ import annotations

import pytest
from comsol_suite.tools.circuit_physics import compute_circuit_params

# Q0 Josephson inductance from design_common.LJJ_list[0]
LJJ_H = 11.2e-9   # Henry

# Reference data from the D0 notebook output
# (d_q_um, f0_comsol_GHz, fge_GHz, anh_MHz)
D0_REFERENCE = [
    (150, 6.887e9, 6.452e9, -481e6),
    (250, 5.168e9, 4.928e9, -258e6),
    (350, 4.215e9, 4.057e9, -167e6),
]

# Tolerance: 1% for frequencies, 2% for anharmonicity
#   (anharmonicity is a small difference of large numbers → slightly looser)
FREQ_TOL  = 0.01
ANH_TOL   = 0.02


@pytest.mark.parametrize("d_q_um,f0_comsol,fge_ref,anh_ref", D0_REFERENCE)
def test_d0_transmon_frequency(d_q_um, f0_comsol, fge_ref, anh_ref):
    """compute_circuit_params reproduces the D0 notebook fge within 1%."""
    out = compute_circuit_params(L_H=LJJ_H, f0_Hz=f0_comsol)

    assert "fq_Hz" in out, f"d_q={d_q_um}: fq_Hz missing from output"
    fge_suite = out["fq_Hz"]

    err = abs(fge_suite - fge_ref) / fge_ref
    assert err < FREQ_TOL, (
        f"d_q={d_q_um} µm: fge mismatch — "
        f"suite={fge_suite/1e9:.4f} GHz vs reference={fge_ref/1e9:.4f} GHz "
        f"({err*100:.2f}% > {FREQ_TOL*100}%)"
    )


@pytest.mark.parametrize("d_q_um,f0_comsol,fge_ref,anh_ref", D0_REFERENCE)
def test_d0_anharmonicity(d_q_um, f0_comsol, fge_ref, anh_ref):
    """compute_circuit_params reproduces the D0 notebook anharmonicity within 2%."""
    out = compute_circuit_params(L_H=LJJ_H, f0_Hz=f0_comsol)

    assert "anh_Hz" in out, f"d_q={d_q_um}: anh_Hz missing from output"
    anh_suite = out["anh_Hz"]

    assert anh_suite < 0, (
        f"d_q={d_q_um}: anharmonicity should be negative (transmon), got {anh_suite/1e6:.1f} MHz"
    )

    err = abs(anh_suite - anh_ref) / abs(anh_ref)
    assert err < ANH_TOL, (
        f"d_q={d_q_um} µm: anharmonicity mismatch — "
        f"suite={anh_suite/1e6:.1f} MHz vs reference={anh_ref/1e6:.1f} MHz "
        f"({err*100:.2f}% > {ANH_TOL*100}%)"
    )


@pytest.mark.parametrize("d_q_um,f0_comsol,fge_ref,anh_ref", D0_REFERENCE)
def test_d0_shunt_capacitance_range(d_q_um, f0_comsol, fge_ref, anh_ref):
    """Shunt capacitance Cs inferred from f0 grows with qubit pad size."""
    out = compute_circuit_params(L_H=LJJ_H, f0_Hz=f0_comsol)

    assert "C_F" in out, f"d_q={d_q_um}: C_F missing from output"
    cs_fF = out["C_F"] * 1e15

    # Cs must be in the physically sensible range for this geometry
    # Notebook: d_q=150→~34fF, d_q=250→~60fF, d_q=350→~93fF
    assert 10 < cs_fF < 200, (
        f"d_q={d_q_um}: Cs={cs_fF:.1f} fF outside plausible range 10–200 fF"
    )


def test_d0_capacitance_increases_with_d_q():
    """Cs(d_q=150) < Cs(d_q=250) < Cs(d_q=350) — larger pad → more capacitance."""
    cs_list = []
    for _, f0, _, _ in D0_REFERENCE:
        out = compute_circuit_params(L_H=LJJ_H, f0_Hz=f0)
        cs_list.append(out["C_F"])

    assert cs_list[0] < cs_list[1] < cs_list[2], (
        f"Cs should increase with d_q: got {[f'{c*1e15:.1f} fF' for c in cs_list]}"
    )


def test_d0_full_output_chain():
    """For d_q=350 (Q0 design point), full output chain checks out."""
    # Q0 reference: f0_COMSOL=4.215 GHz → fge=4.057 GHz, anh=-167 MHz, Cs≈93 fF
    f0 = 4.215e9
    out = compute_circuit_params(L_H=LJJ_H, f0_Hz=f0)

    # All expected keys present
    for key in ["L_H", "Ic_A", "EJ_Hz", "C_F", "EC_Hz", "fq_Hz", "anh_Hz"]:
        assert key in out, f"Missing key: {key}"

    # EJ/EC ratio in deep transmon regime (should be >> 1 for transmon)
    ej_ec = out["EJ_Hz"] / out["EC_Hz"]
    assert ej_ec > 30, f"EJ/EC={ej_ec:.1f} too low for transmon regime (>30 expected)"

    # Frequency within 1% of notebook reference (4.057 GHz)
    assert abs(out["fq_Hz"] - 4.057e9) / 4.057e9 < 0.01

    # Anharmonicity within 2% of -167 MHz
    assert abs(out["anh_Hz"] - (-167e6)) / 167e6 < 0.02

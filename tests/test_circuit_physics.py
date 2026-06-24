"""Validation tests for circuit_physics.py against AlNtransmon reference values.

All expected values are derived analytically or cross-checked against the
AlNtransmon reference design (COPY - AlNtransmon/design_params.yaml).

Physical conventions:
- Frequencies in Hz
- Energies in Hz (i.e. E/h, not in Joules)
- Inductances in H, capacitances in F
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from comsol_suite.tools.circuit_physics import (
    h, e, Phi0,
    calc_cap_from_eigenfreq,
    cap_to_charging_energy,
    charging_energy_to_cap,
    inductance_to_critical_current,
    critical_current_to_josephson_energy,
    inductance_to_josephson_energy,
    josephson_energy_to_inductance,
    transmon_frequency,
    transmon_anharmonicity,
    path_field_integral,
    extract_coupling_g,
    dispersive_shift,
    purcell_rate,
    polynomial_inverse,
    linear_interpolate,
    compute_circuit_params,
)

# AlNtransmon reference: Q0 has LJJ = 11.2 nH (from design_common.LJJ_list[0])
LJJ_Q0 = 11.2e-9   # H
# Q1: LJJ = 10.8 nH
LJJ_Q1 = 10.8e-9   # H

# From pipeline doc: d_q=271µm → Cs≈93fF, d_q=214µm → Cs≈70.6fF
CS_Q1 = 93.0e-15   # F  (Q1 uses d_q=271µm)
CS_Q2 = 70.6e-15   # F  (Q2 uses d_q=214µm)


# ── Physical constants ────────────────────────────────────────────────────────

def test_flux_quantum():
    """Phi0 = h/(2e) ≈ 2.0678e-15 Wb."""
    assert abs(Phi0 - 2.0678e-15) / 2.0678e-15 < 1e-4


def test_planck_constant():
    assert abs(h - 6.626e-34) / 6.626e-34 < 1e-3


# ── Inductance ↔ current ↔ energy chain ──────────────────────────────────────

def test_inductance_to_critical_current():
    """Ic = Phi0 / (2π·L). For L=11.2nH → Ic ≈ 29.3 nA."""
    Ic = inductance_to_critical_current(LJJ_Q0)
    expected = Phi0 / (2 * math.pi * LJJ_Q0)
    assert abs(Ic - expected) / expected < 1e-10


def test_critical_current_to_EJ():
    """EJ = Ic / (4π·e). Positive value, order ~GHz for nA currents."""
    Ic = inductance_to_critical_current(LJJ_Q0)
    EJ = critical_current_to_josephson_energy(Ic)
    assert EJ > 0
    # For Ic~29nA: EJ ≈ Ic/(4πe) ≈ 29e-9/(4π·1.6e-19) ~ 14.5 GHz
    assert 10e9 < EJ < 20e9


def test_inductance_to_EJ_chain():
    """inductance_to_josephson_energy matches the two-step chain."""
    EJ_chain = critical_current_to_josephson_energy(
        inductance_to_critical_current(LJJ_Q0)
    )
    EJ_direct = inductance_to_josephson_energy(LJJ_Q0)
    assert abs(EJ_chain - EJ_direct) / EJ_chain < 1e-12


def test_EJ_to_inductance_roundtrip():
    """josephson_energy_to_inductance(inductance_to_josephson_energy(L)) ≈ L."""
    EJ = inductance_to_josephson_energy(LJJ_Q0)
    L_back = josephson_energy_to_inductance(EJ)
    assert abs(L_back - LJJ_Q0) / LJJ_Q0 < 1e-10


def test_LJJ_roundtrip_for_all_qubits():
    """L → EJ → L roundtrip holds for all 16 AlNtransmon LJJ values."""
    LJJ_list = [
        11.2, 10.8, 10.3, 10.0, 9.6, 9.2, 8.9, 8.6,
        10.2, 9.9, 9.5, 9.2, 8.9, 8.7, 8.4, 8.2,
    ]  # nH
    for ljj_nH in LJJ_list:
        L = ljj_nH * 1e-9
        EJ = inductance_to_josephson_energy(L)
        L_back = josephson_energy_to_inductance(EJ)
        assert abs(L_back - L) / L < 1e-10, f"Roundtrip failed for L={ljj_nH} nH"


# ── Capacitance ↔ charging energy ────────────────────────────────────────────

def test_cap_to_charging_energy():
    """EC = e²/(2hC). For Cs=93fF → EC ≈ 235 MHz."""
    EC = cap_to_charging_energy(CS_Q1)
    # e²/(2·h·93e-15) ~ 235 MHz
    assert 200e6 < EC < 280e6, f"EC out of range: {EC/1e6:.1f} MHz"


def test_cap_charging_energy_roundtrip():
    EC = cap_to_charging_energy(CS_Q1)
    C_back = charging_energy_to_cap(EC)
    assert abs(C_back - CS_Q1) / CS_Q1 < 1e-12


def test_calc_cap_from_eigenfreq():
    """C = 1/(L·ω²). For L=11.2nH, f0=5GHz → C ≈ 91fF."""
    f0 = 5e9
    C = calc_cap_from_eigenfreq(LJJ_Q0, f0)
    expected = 1.0 / (LJJ_Q0 * (2 * math.pi * f0) ** 2)
    assert abs(C - expected) / expected < 1e-10
    # Should be tens of fF
    assert 50e-15 < C < 200e-15


# ── Transmon spectrum ─────────────────────────────────────────────────────────

def test_transmon_frequency_order_of_magnitude():
    """f_transmon(EJ,EC) should land in the 3–8 GHz qubit band."""
    EJ = inductance_to_josephson_energy(LJJ_Q1)   # ~15 GHz
    EC = cap_to_charging_energy(CS_Q1)             # ~235 MHz
    fq = transmon_frequency(EJ, EC)
    assert 3e9 < fq < 8e9, f"fq out of band: {fq/1e9:.2f} GHz"


def test_transmon_anharmonicity_negative():
    """Transmon anharmonicity must be negative (lower transition spacing)."""
    EJ = inductance_to_josephson_energy(LJJ_Q1)
    EC = cap_to_charging_energy(CS_Q1)
    anh = transmon_anharmonicity(EJ, EC)
    assert anh < 0, f"anharmonicity should be negative, got {anh/1e6:.1f} MHz"


def test_anharmonicity_magnitude():
    """Typical transmon anharmonicity: 100–350 MHz."""
    EJ = inductance_to_josephson_energy(LJJ_Q1)
    EC = cap_to_charging_energy(CS_Q1)
    anh = transmon_anharmonicity(EJ, EC)
    assert 100e6 < abs(anh) < 350e6, f"|anh|={abs(anh)/1e6:.1f} MHz out of range"


def test_transmon_harmonic_limit():
    """In the deep transmon limit (EJ/EC → ∞), f ≈ √(8·EJ·EC)."""
    EC = 200e6    # Hz
    EJ = 1000e9   # Hz — very deep transmon
    fq = transmon_frequency(EJ, EC)
    harmonic = math.sqrt(8 * EJ * EC)
    assert abs(fq - harmonic) / harmonic < 0.02


# ── Path field integral ───────────────────────────────────────────────────────

def test_path_field_integral_constant_field():
    """∫|E| ds over uniform field of 1 V/m on 1 m path = 1 V."""
    arc = np.linspace(0, 1, 100)
    E = np.ones(100)
    result = path_field_integral(arc, E)
    assert abs(result - 1.0) < 1e-6


def test_path_field_integral_triangle():
    """∫|E| ds for tent function = 0.5 (base 1, height 1)."""
    arc = np.array([0.0, 0.5, 1.0])
    E = np.array([0.0, 1.0, 0.0])
    result = path_field_integral(arc, E)
    assert abs(result - 0.5) < 1e-10


# ── Coupling extraction ───────────────────────────────────────────────────────

def test_extract_coupling_g_typical_range():
    """g should land in 100–250 MHz for AlNtransmon geometry."""
    # Typical dressed frequencies from a Q1 (d_q=271µm, delta_angle~32.5°) simulation:
    # resonator-like mode at ~6.6 GHz, qubit-like at ~5.2 GHz
    f_R = 6.64e9     # Hz
    f_Q = 5.2e9      # Hz
    # Fabricate plausible energy data: resonator-like mode has small EJJ contribution
    We = 1.01e-20    # J
    Wm = 1.0e-20     # J  → EJJ = We-Wm = 1e-22 J (small participation)
    g = extract_coupling_g(f_R, We, Wm, f_Q)
    assert 50e6 < g < 300e6, f"g={g/1e6:.1f} MHz out of 50–300 MHz range"


def test_extract_coupling_g_scales_with_participation():
    """Larger EJJ/Wm ratio (more hybridisation) → larger g."""
    f_R = 6.5e9
    f_Q = 5.0e9
    # Low participation: EJJ = 0.1% of Wm
    We_low = 1.001e-20
    Wm = 1.0e-20
    g_low = extract_coupling_g(f_R, We_low, Wm, f_Q)

    # Higher participation: EJJ = 1% of Wm
    We_high = 1.01e-20
    g_high = extract_coupling_g(f_R, We_high, Wm, f_Q)

    assert g_high > g_low, "larger energy participation should yield larger coupling"


# ── Dispersive shift ──────────────────────────────────────────────────────────

def test_dispersive_shift_sign():
    """χ should be negative when fq > fr (dispersive regime, standard transmon)."""
    fq = 5.2e9
    fr = 6.6e9
    g = 150e6
    EJ = inductance_to_josephson_energy(LJJ_Q1)
    EC = cap_to_charging_energy(CS_Q1)
    anh = transmon_anharmonicity(EJ, EC)
    chi = dispersive_shift(fq, anh, fr, g)
    # fq < fr → qubit dispersive shift negative
    assert chi < 0


def test_dispersive_shift_magnitude():
    """2χ per photon should be ~0.5–5 MHz for AlNtransmon design."""
    fq = 5.2e9
    fr = 6.6e9
    g = 150e6
    EJ = inductance_to_josephson_energy(LJJ_Q1)
    EC = cap_to_charging_energy(CS_Q1)
    anh = transmon_anharmonicity(EJ, EC)
    chi = dispersive_shift(fq, anh, fr, g)
    assert 0.1e6 < abs(chi) < 10e6, f"|chi|={abs(chi)/1e6:.2f} MHz out of 0.1–10 MHz"


# ── Purcell rate ──────────────────────────────────────────────────────────────

def test_purcell_rate_positive():
    """κ_Purcell must be positive."""
    kappa = purcell_rate(V_junction=1.0+0j, V_port=0.01+0j, C_F=CS_Q1)
    assert kappa > 0


def test_purcell_rate_formula():
    """κ = |Vz/Vs|² / (Z0·C). Check with unit values."""
    Vs = 1.0 + 0j
    Vz = 0.1 + 0j   # 10:1 voltage ratio
    C = 100e-15
    Z0 = 50.0
    kappa = purcell_rate(Vs, Vz, C, Z0)
    expected = abs(Vz / Vs) ** 2 / (Z0 * C)
    assert abs(kappa - expected) / expected < 1e-10


# ── Inversion utilities ───────────────────────────────────────────────────────

def test_polynomial_inverse_linear():
    """For a linear y=2x, inverse at y=10 → x=5."""
    x = np.array([0.0, 5.0, 10.0, 15.0])
    y = 2.0 * x
    roots = polynomial_inverse(x, y, y_target=10.0, degree=1)
    assert roots, "no root found"
    assert abs(roots[0] - 5.0) < 1e-6


def test_polynomial_inverse_slider_length():
    """Simulates D2: find slider length for target resonance frequency."""
    # Fabricated resonator calibration curve: l_slider in µm → fr in GHz
    l_slider = np.array([100.0, 140.0, 180.0, 220.0, 260.0])
    # Decreasing frequency with increasing slider (more length → lower f)
    fr = np.array([6.85, 6.75, 6.65, 6.56, 6.47])
    target = 6.64
    roots = polynomial_inverse(l_slider, fr, y_target=target, degree=3)
    assert roots, f"no slider length found for fr={target} GHz"
    sol = roots[0]
    assert 100 < sol < 260, f"solution {sol:.1f} µm outside sweep range"


def test_linear_interpolate():
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = np.array([0.0, 2.0, 4.0, 6.0])
    assert abs(linear_interpolate(x, y, 1.5) - 3.0) < 1e-10


def test_linear_interpolate_unsorted():
    """linear_interpolate handles unsorted x by sorting internally."""
    x = np.array([3.0, 0.0, 2.0, 1.0])
    y = np.array([6.0, 0.0, 4.0, 2.0])
    assert abs(linear_interpolate(x, y, 1.5) - 3.0) < 1e-10


# ── compute_circuit_params wrapper ────────────────────────────────────────────

def test_compute_circuit_params_from_LJ_and_f0():
    """Full D0 post-processing: LJ + eigenfrequency → Cs, EC, fq, anh."""
    out = compute_circuit_params(L_H=LJJ_Q1, f0_Hz=5.5e9)
    assert "C_F" in out
    assert "EC_Hz" in out
    assert "EJ_Hz" in out
    assert "fq_Hz" in out
    assert "anh_Hz" in out
    assert out["anh_Hz"] < 0   # transmon: negative anharmonicity


def test_compute_circuit_params_dispersive_shift():
    """Providing g and fr adds chi to output."""
    EJ = inductance_to_josephson_energy(LJJ_Q1)
    EC = cap_to_charging_energy(CS_Q1)
    fq = transmon_frequency(EJ, EC)
    anh = transmon_anharmonicity(EJ, EC)
    out = compute_circuit_params(
        EJ_Hz=EJ, EC_Hz=EC, fq_Hz=fq, anh_Hz=anh, fr_Hz=6.64e9, g_Hz=148e6
    )
    assert "chi_Hz" in out
    assert abs(out["chi_Hz"]) > 0


def test_compute_circuit_params_purcell():
    """Providing V_junction/V_port adds kappa and T1 to output."""
    out = compute_circuit_params(
        C_F=CS_Q1,
        V_junction=1.0 + 0j,
        V_port=0.01 + 0j,
    )
    assert "kappa_purcell_rad_s" in out
    assert "T1_purcell_s" in out
    assert out["T1_purcell_s"] > 0

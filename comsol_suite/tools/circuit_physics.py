"""Superconducting circuit physics library.

Pure-Python math for any lumped-element superconducting circuit (transmon,
fluxonium, charge qubit, resonator, Purcell filter, etc.). No COMSOL, no GDS
dependencies — only numpy and scipy.

All frequencies are in Hz unless the function name says otherwise.
All energies are in Hz (i.e. E/h, not E in Joules).
All inductances in H, capacitances in F, currents in A, voltages in V.

Reference: Koch et al., arXiv:1706.06566 (transmon perturbation series).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Union

import numpy as np
import scipy.optimize

# ── Physical constants ────────────────────────────────────────────────────────
h    = 6.62607015e-34       # J·s  — Planck constant
hbar = h / (2 * math.pi)   # J·s  — reduced Planck constant
e    = 1.602176634e-19      # C    — elementary charge
Phi0 = h / (2 * e)         # V·s  — magnetic flux quantum  Φ₀ = h/(2e)
ep0  = 8.8541878128e-12     # F/m  — vacuum permittivity
mu0  = 1.25663706212e-6     # H/m  — vacuum permeability


# ── LC oscillator / circuit conversions ──────────────────────────────────────

def calc_cap_from_eigenfreq(L_H: float, f0_Hz: float) -> float:
    """Infer shunt capacitance from an LC eigenfrequency.

    Inverts  f₀ = 1/(2π√(LC))  →  C = 1/(L·(2π·f₀)²).

    Parameters
    ----------
    L_H : float
        Inductance in Henry.
    f0_Hz : float
        Eigenfrequency in Hz.

    Returns
    -------
    float
        Capacitance in Farads.
    """
    omega = 2.0 * math.pi * f0_Hz
    return 1.0 / (L_H * omega ** 2)


def cap_to_charging_energy(C_F: float) -> float:
    """Charging energy EC = e²/(2·h·C) in Hz.

    Parameters
    ----------
    C_F : float
        Capacitance in Farads.

    Returns
    -------
    float
        Charging energy EC in Hz.
    """
    return e ** 2 / (2.0 * h * C_F)


def charging_energy_to_cap(EC_Hz: float) -> float:
    """Capacitance from charging energy  C = e²/(2·h·EC).

    Parameters
    ----------
    EC_Hz : float
        Charging energy in Hz.

    Returns
    -------
    float
        Capacitance in Farads.
    """
    return e ** 2 / (2.0 * h * EC_Hz)


def inductance_to_critical_current(L_H: float) -> float:
    """Critical current from Josephson inductance  Ic = Φ₀/(2π·L).

    Parameters
    ----------
    L_H : float
        Josephson inductance in Henry.

    Returns
    -------
    float
        Critical current in Amperes.
    """
    return Phi0 / (2.0 * math.pi * L_H)


def critical_current_to_josephson_energy(Ic_A: float) -> float:
    """Josephson energy EJ = Ic·Φ₀/(2π·h) = Ic/(4π·e) in Hz.

    Parameters
    ----------
    Ic_A : float
        Critical current in Amperes.

    Returns
    -------
    float
        Josephson energy EJ in Hz.
    """
    return Ic_A / (4.0 * math.pi * e)


def inductance_to_josephson_energy(L_H: float) -> float:
    """Josephson energy from inductance — chain L → Ic → EJ.

    Parameters
    ----------
    L_H : float
        Josephson inductance in Henry.

    Returns
    -------
    float
        Josephson energy EJ in Hz.
    """
    return critical_current_to_josephson_energy(inductance_to_critical_current(L_H))


def josephson_energy_to_inductance(EJ_Hz: float) -> float:
    """Josephson inductance from energy — chain EJ → Ic → L.

    Ic = EJ · 4π·e / 1  →  L = Φ₀/(2π·Ic).

    Parameters
    ----------
    EJ_Hz : float
        Josephson energy in Hz.

    Returns
    -------
    float
        Josephson inductance in Henry.
    """
    Ic = EJ_Hz * 4.0 * math.pi * e
    return Phi0 / (2.0 * math.pi * Ic)


# ── Transmon spectrum (perturbation series) ───────────────────────────────────

def transmon_frequency(EJ_Hz: float, EC_Hz: float) -> float:
    """Transmon 0→1 transition frequency f₀₁ in Hz.

    Perturbation series from Koch et al. arXiv:1706.06566, valid for EJ/EC >> 1.

        ξ = √(2·EC/EJ)
        f ≈ √(8·EJ·EC) - EC·(1 + ξ/4 + 21ξ²/128 + 19ξ³/128 + 5319ξ⁴/32768)

    Parameters
    ----------
    EJ_Hz : float
        Josephson energy in Hz.
    EC_Hz : float
        Charging energy in Hz.

    Returns
    -------
    float
        0→1 transition frequency in Hz.
    """
    xi = math.sqrt(2.0 * EC_Hz / EJ_Hz)
    harmonic = math.sqrt(8.0 * EJ_Hz * EC_Hz)
    correction = EC_Hz * (
        1.0
        + xi / 4.0
        + 21.0 * xi ** 2 / 128.0
        + 19.0 * xi ** 3 / 128.0
        + 5319.0 * xi ** 4 / 32768.0
    )
    return harmonic - correction


def transmon_anharmonicity(EJ_Hz: float, EC_Hz: float) -> float:
    """Transmon anharmonicity α = f₁₂ - f₀₁ in Hz.

    For a transmon α is negative.

        ξ = √(2·EC/EJ)
        α ≈ -EC·(1 + 9ξ/16 + 81ξ²/128 + 3645ξ³/4096 + 46899ξ⁴/32768)

    Parameters
    ----------
    EJ_Hz : float
        Josephson energy in Hz.
    EC_Hz : float
        Charging energy in Hz.

    Returns
    -------
    float
        Anharmonicity in Hz (negative for transmon).
    """
    xi = math.sqrt(2.0 * EC_Hz / EJ_Hz)
    return -EC_Hz * (
        1.0
        + 9.0 * xi / 16.0
        + 81.0 * xi ** 2 / 128.0
        + 3645.0 * xi ** 3 / 4096.0
        + 46899.0 * xi ** 4 / 32768.0
    )


# ── Mode coupling from field data ─────────────────────────────────────────────

def path_field_integral(arc_m, normE_Vm) -> float:
    """Integrate |E| along a path using the trapezoidal rule.

    Used to compute ∫|E(s)| ds along a CPW or other path selection from a
    COMSOL eigenfrequency solution. Larger value means more field energy
    localised along that path → used for mode identification.

    Parameters
    ----------
    arc_m : array-like
        Arc-length coordinates along the path in metres.
    normE_Vm : array-like
        |E| field magnitudes at the corresponding points in V/m.

    Returns
    -------
    float
        ∫|E| ds in V (scalar).
    """
    arc = np.asarray(arc_m, dtype=float)
    E   = np.asarray(normE_Vm, dtype=float)
    return float(np.trapezoid(E, x=arc))


def field_energy_ratio(W_electric_J: float, W_magnetic_J: float) -> float:
    """Energy participation ratio r = √(EJJ / Wm).

    EJJ = We - Wm is the energy stored in the lumped element (JJ or port).
    Wm is the total magnetic energy in the mode.

    Parameters
    ----------
    W_electric_J : float
        Total electric energy of the mode in Joules.
    W_magnetic_J : float
        Total magnetic energy of the mode in Joules.

    Returns
    -------
    float
        Dimensionless participation ratio r.
    """
    EJJ = W_electric_J - W_magnetic_J
    return math.sqrt(EJJ / W_magnetic_J)


def extract_coupling_g(
    f_mode1_Hz: float,
    We_J: float,
    Wm_J: float,
    f_mode2_Hz: float,
) -> float:
    """Extract coupling g between two modes from eigenfrequency field data.

    Uses the Jaynes-Cummings energy partition method.  In a two-mode
    Jaynes-Cummings system the dressed eigenfrequencies satisfy:

        f± = ½(f₁+f₂) ± ½√((f₁-f₂)² + 4g²)

    The energy partition between the lumped element and the stored field:

        r = √(EJJ/Wm) = -√(f₂/f₁) · tan(Λ)

    gives an implicit equation for g, solved numerically via Brent's method.

    Parameters
    ----------
    f_mode1_Hz : float
        Dressed eigenfrequency of mode 1 (resonator-like) in Hz.
    We_J : float
        Total electric energy of mode 1 in Joules.
    Wm_J : float
        Total magnetic energy of mode 1 in Joules.
    f_mode2_Hz : float
        Dressed eigenfrequency of mode 2 (qubit-like) in Hz.

    Returns
    -------
    float
        Coupling g in Hz.
    """
    EJJ = We_J - Wm_J
    r   = math.sqrt(EJJ / Wm_J)
    delta = f_mode1_Hz - f_mode2_Hz
    sign  = 1 if f_mode1_Hz > f_mode2_Hz else -1

    def residual(g: float) -> float:
        disc = (f_mode1_Hz - f_mode2_Hz) ** 2 - 4.0 * g ** 2
        if disc < 0:
            return float("nan")
        f1_bare = 0.5 * ((f_mode1_Hz + f_mode2_Hz) + sign * math.sqrt(disc))
        f2_bare = 0.5 * ((f_mode1_Hz + f_mode2_Hz) - sign * math.sqrt(disc))
        k = math.sqrt(f2_bare / f1_bare)
        return g + r * k * delta / (r ** 2 - k ** 2)

    half_delta = abs(delta) / 2.0
    return scipy.optimize.brentq(
        residual, -half_delta + 1.0, half_delta - 1.0, xtol=2e-12
    )


def dispersive_shift(
    fq_Hz: float,
    anh_Hz: float,
    fr_Hz: float,
    g_Hz: float,
) -> float:
    """Second-order dispersive shift χ in Hz.

    χ = g²/(fq - fr) · anh/(fq + anh - fr)

    The observable shift of the qubit frequency per readout photon is 2χ.

    Parameters
    ----------
    fq_Hz : float
        Qubit frequency in Hz.
    anh_Hz : float
        Qubit anharmonicity (f₁₂ - f₀₁) in Hz; negative for transmon.
    fr_Hz : float
        Resonator frequency in Hz.
    g_Hz : float
        Qubit-resonator coupling in Hz.

    Returns
    -------
    float
        Dispersive shift χ in Hz.
    """
    return (g_Hz ** 2 / (fq_Hz - fr_Hz)) * (anh_Hz / (fq_Hz + anh_Hz - fr_Hz))


# ── Decay rates ───────────────────────────────────────────────────────────────

def purcell_rate(
    V_junction: complex,
    V_port: complex,
    C_F: float,
    Z0_Ohm: float = 50.0,
) -> float:
    """Purcell (radiative) decay rate of a qubit through an output port.

    κ_Purcell = Re[Y(ωq)] / C  ≈ (1/Z₀)|Vz/Vs|² / C

    where Vs is the voltage across the lumped element (junction) and Vz is the
    voltage at the output port, both extracted from a COMSOL eigenmode or
    frequency-domain solution.

    Ref: Phys. Rev. Applied 17, 044016

    Parameters
    ----------
    V_junction : complex
        Complex voltage at the lumped element (junction/qubit) node in V.
    V_port : complex
        Complex voltage at the readout / output port node in V.
    C_F : float
        Shunt capacitance of the qubit (or mode) in Farads.
    Z0_Ohm : float
        Port impedance in Ohms (default 50 Ω).

    Returns
    -------
    float
        Decay rate κ_Purcell in rad/s.
    """
    r = V_port / V_junction
    ReY = abs(r) ** 2 / Z0_Ohm
    return ReY / C_F


# ── Inversion utilities ───────────────────────────────────────────────────────

def polynomial_inverse(
    x_data,
    y_data,
    y_target: float,
    degree: int = 3,
) -> List[float]:
    """Find x such that poly_fit(x) ≈ y_target.

    Fits y = p(x) with a polynomial of given degree, then returns all real
    roots of p(x) - y_target = 0 that lie within [min(x_data), max(x_data)].

    Typical use: find the slider length that hits a target resonance frequency
    after sweeping over a calibration curve.

    Parameters
    ----------
    x_data : array-like
        Independent variable values (e.g. slider lengths in µm).
    y_data : array-like
        Corresponding dependent variable values (e.g. resonance frequencies).
    y_target : float
        Target value of y.
    degree : int
        Polynomial degree for the fit (default 3).

    Returns
    -------
    list of float
        Real roots within the data range.  Empty if none found.
    """
    x = np.asarray(x_data, dtype=float)
    y = np.asarray(y_data, dtype=float)

    coeffs = np.polyfit(x, y, degree)
    # Shift polynomial down by y_target so roots are the solutions
    shifted = coeffs.copy()
    shifted[-1] -= y_target

    roots = np.roots(shifted)
    x_min, x_max = float(x.min()), float(x.max())
    real_roots = [
        float(r.real)
        for r in roots
        if abs(r.imag) < 1e-10 * abs(r.real + 1e-30)
        and x_min <= r.real <= x_max
    ]
    return sorted(real_roots)


def linear_interpolate(x_data, y_data, x_new):
    """Linear interpolation via np.interp.

    Sorts by x before interpolating so unsorted input is handled correctly.

    Parameters
    ----------
    x_data : array-like
        Known x values.
    y_data : array-like
        Known y values.
    x_new : float or array-like
        x value(s) at which to evaluate the interpolant.

    Returns
    -------
    float or ndarray
        Interpolated y value(s).
    """
    x = np.asarray(x_data, dtype=float)
    y = np.asarray(y_data, dtype=float)
    order = np.argsort(x)
    return np.interp(x_new, x[order], y[order])


# ── High-level convenience wrapper ────────────────────────────────────────────

def compute_circuit_params(**kwargs) -> Dict[str, Any]:
    """Compute superconducting circuit parameters from any input combination.

    Accepts any subset of keyword arguments and derives all quantities it can
    from what is provided.  Unknown or underspecified quantities are omitted
    from the returned dict.

    Accepted inputs
    ---------------
    L_H         : Josephson inductance (Henry)
    f0_Hz       : LC eigenfrequency (Hz) — used with L_H to infer C_F
    EJ_Hz       : Josephson energy (Hz)
    EC_Hz       : Charging energy (Hz)
    C_F         : Shunt capacitance (Farad)
    Ic_A        : Critical current (Ampere)
    g_Hz        : Qubit-resonator coupling (Hz)
    fq_Hz       : Qubit frequency (Hz)
    fr_Hz       : Resonator frequency (Hz)
    anh_Hz      : Qubit anharmonicity (Hz)
    V_junction  : Voltage at lumped-element node (complex, V)
    V_port      : Voltage at output port node (complex, V)
    Z0_Ohm      : Port impedance (Ohm, default 50)
    We_J        : Total electric energy of resonator mode (J)
    Wm_J        : Total magnetic energy of resonator mode (J)

    Returns
    -------
    dict
        Flat dict with all derivable quantities, e.g.:
        {"Ic_A": ..., "EJ_Hz": ..., "EC_Hz": ..., "fq_Hz": ..., ...}
    """
    result: Dict[str, Any] = {}
    Z0 = float(kwargs.get("Z0_Ohm", 50.0))

    # ── Inductance chain ──────────────────────────────────────────────────────
    L_H = kwargs.get("L_H")
    if L_H is not None:
        L_H = float(L_H)
        result["L_H"] = L_H
        Ic = inductance_to_critical_current(L_H)
        result["Ic_A"] = Ic
        EJ = inductance_to_josephson_energy(L_H)
        result["EJ_Hz"] = EJ
    else:
        EJ = kwargs.get("EJ_Hz")
        if EJ is not None:
            EJ = float(EJ)
            result["EJ_Hz"] = EJ
            Ic_derived = EJ * 4.0 * math.pi * e
            result["Ic_A"] = Ic_derived
            result["L_H"] = josephson_energy_to_inductance(EJ)
        Ic = kwargs.get("Ic_A")
        if Ic is not None and "EJ_Hz" not in result:
            Ic = float(Ic)
            result["Ic_A"] = Ic
            result["EJ_Hz"] = critical_current_to_josephson_energy(Ic)
            result["L_H"] = inductance_to_critical_current.__module__ and Phi0 / (2 * math.pi * Ic)
            EJ = result["EJ_Hz"]

    # ── Capacitance chain ─────────────────────────────────────────────────────
    C_F = kwargs.get("C_F")
    EC_Hz = kwargs.get("EC_Hz")
    f0_Hz = kwargs.get("f0_Hz")

    if C_F is not None:
        C_F = float(C_F)
        result["C_F"] = C_F
        result["EC_Hz"] = cap_to_charging_energy(C_F)
    elif EC_Hz is not None:
        EC_Hz = float(EC_Hz)
        result["EC_Hz"] = EC_Hz
        result["C_F"] = charging_energy_to_cap(EC_Hz)
        C_F = result["C_F"]
    elif f0_Hz is not None and "L_H" in result:
        C_F = calc_cap_from_eigenfreq(result["L_H"], float(f0_Hz))
        result["C_F"] = C_F
        result["EC_Hz"] = cap_to_charging_energy(C_F)

    # ── Transmon spectrum ─────────────────────────────────────────────────────
    EJ_val = result.get("EJ_Hz") or kwargs.get("EJ_Hz")
    EC_val = result.get("EC_Hz") or kwargs.get("EC_Hz")
    if EJ_val is not None and EC_val is not None:
        EJ_val = float(EJ_val)
        EC_val = float(EC_val)
        fq = transmon_frequency(EJ_val, EC_val)
        anh = transmon_anharmonicity(EJ_val, EC_val)
        result.setdefault("fq_Hz", fq)
        result.setdefault("anh_Hz", anh)

    # ── Dispersive shift ──────────────────────────────────────────────────────
    fq_val  = result.get("fq_Hz")  or kwargs.get("fq_Hz")
    fr_val  = kwargs.get("fr_Hz")
    g_val   = kwargs.get("g_Hz")
    anh_val = result.get("anh_Hz") or kwargs.get("anh_Hz")
    if all(v is not None for v in [fq_val, anh_val, fr_val, g_val]):
        chi = dispersive_shift(float(fq_val), float(anh_val),
                               float(fr_val), float(g_val))
        result["chi_Hz"] = chi
        result["fr_Hz"]  = float(fr_val)
        result["g_Hz"]   = float(g_val)

    # ── Purcell rate ──────────────────────────────────────────────────────────
    Vs = kwargs.get("V_junction")
    Vz = kwargs.get("V_port")
    C_for_purcell = result.get("C_F") or kwargs.get("C_F")
    if Vs is not None and Vz is not None and C_for_purcell is not None:
        kappa = purcell_rate(Vs, Vz, float(C_for_purcell), Z0)
        result["kappa_purcell_rad_s"] = kappa
        result["T1_purcell_s"] = 1.0 / kappa if kappa > 0 else float("inf")

    # ── Field coupling extraction ─────────────────────────────────────────────
    We_J = kwargs.get("We_J")
    Wm_J = kwargs.get("Wm_J")
    f_mode2 = kwargs.get("f_mode2_Hz") or fq_val
    if We_J is not None and Wm_J is not None and "fr_Hz" in result and f_mode2 is not None:
        try:
            g_extracted = extract_coupling_g(
                float(result["fr_Hz"]), float(We_J), float(Wm_J), float(f_mode2)
            )
            result.setdefault("g_Hz", g_extracted)
        except Exception:
            pass

    return result

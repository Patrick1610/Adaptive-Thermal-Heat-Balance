"""Scalar heat-balance subset required by the frozen ATHB formulation.

The iteration structure is adapted from pythermalcomfort 4.4.2 under the MIT
notice retained in ``LICENSES/pythermalcomfort-4.4.2.txt``. This module exposes
thermal load only; it is not a standalone PMV or PPD implementation.
"""

from __future__ import annotations

import math

from .contracts import (
    HeatBalanceResult,
    HeatBalanceSuccess,
    NumericalFailure,
    NumericalFailureCode,
)

MET_TO_W_M2 = 58.15
LEGACY_ABSOLUTE_OFFSET = 273.0
CLOTHING_CONVERGENCE_EPSILON = 0.00015
MAX_CLOTHING_ITERATION_UPDATES = 150


def _fourth_power(value: float) -> float:
    """Match the pinned Numba binary64 lowering for an integer fourth power."""

    square = value * value
    return square * square


def _input_failure(field: str, value: object) -> NumericalFailure | None:
    if isinstance(value, bool):
        return NumericalFailure(
            NumericalFailureCode.BOOLEAN_INPUT,
            field,
            f"{field} must not be Boolean",
        )
    if not isinstance(value, (int, float)):
        return NumericalFailure(
            NumericalFailureCode.NON_NUMERIC,
            field,
            f"{field} must be an int or float",
        )
    try:
        numeric = float(value)
    except OverflowError:
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            field,
            f"{field} must be finite",
        )
    if not math.isfinite(numeric):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            field,
            f"{field} must be finite",
        )
    return None


def _domain_failure(field: str, value: float, requirement: str) -> NumericalFailure:
    return NumericalFailure(
        NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
        field,
        f"{field}={value!r} is outside {requirement}",
    )


def calculate_heat_balance(
    *,
    tdb_c: float,
    tr_c: float,
    relative_air_speed_m_s: float,
    rh_pct: float,
    adapted_met: float,
    effective_clo: float,
) -> HeatBalanceResult:
    """Return the scalar thermal load or a typed numerical failure.

    Inputs are the already-adapted metabolic rate and selected effective clothing.
    The fixed 150-update limit is intentionally not a public tuning parameter.
    """

    values = (
        ("tdb_c", tdb_c),
        ("tr_c", tr_c),
        ("relative_air_speed_m_s", relative_air_speed_m_s),
        ("rh_pct", rh_pct),
        ("adapted_met", adapted_met),
        ("effective_clo", effective_clo),
    )
    for field, value in values:
        if failure := _input_failure(field, value):
            return failure

    tdb = float(tdb_c)
    tr = float(tr_c)
    speed = float(relative_air_speed_m_s)
    rh = float(rh_pct)
    met = float(adapted_met)
    clo = float(effective_clo)

    domain_checks = (
        ("tdb_c", tdb, 5.0 <= tdb <= 40.0, "[5, 40] degrees Celsius"),
        ("tr_c", tr, 0.0 <= tr <= 50.0, "[0, 50] degrees Celsius"),
        (
            "relative_air_speed_m_s",
            speed,
            0.0 <= speed <= 2.0,
            "[0, 2] metres per second",
        ),
        ("rh_pct", rh, 0.0 <= rh <= 100.0, "[0, 100] percent"),
        ("adapted_met", met, met > 0.0, "the open interval (0, infinity) met"),
        ("effective_clo", clo, 0.0 < clo <= 3.0, "the interval (0, 3] clo"),
    )
    for field, value, accepted, requirement in domain_checks:
        if not accepted:
            return _domain_failure(field, value, requirement)

    try:
        vapor_pressure_pa = rh * 10.0 * math.exp(16.6536 - 4030.183 / (tdb + 235.0))
        clothing_insulation_m2_k_w = 0.155 * clo
        metabolic_rate_w_m2 = met * MET_TO_W_M2
        external_work_w_m2 = 0.0 * MET_TO_W_M2
        internal_heat_w_m2 = metabolic_rate_w_m2 - external_work_w_m2
        if clothing_insulation_m2_k_w <= 0.078:
            clothing_area_factor = 1.0 + 1.29 * clothing_insulation_m2_k_w
        else:
            clothing_area_factor = 1.05 + 0.645 * clothing_insulation_m2_k_w

        forced_convection = 12.1 * math.sqrt(speed)
        convection = forced_convection
        air_kelvin_legacy = tdb + LEGACY_ABSOLUTE_OFFSET
        radiant_kelvin_legacy = tr + LEGACY_ABSOLUTE_OFFSET
        initial_clothing_surface = air_kelvin_legacy + (35.5 - tdb) / (
            3.5 * (6.45 * clothing_insulation_m2_k_w + 0.1)
        )

        p1 = clothing_insulation_m2_k_w * clothing_area_factor
        p2 = p1 * 3.96
        p3 = p1 * 100.0
        p4 = p1 * air_kelvin_legacy
        scaled_radiant_temperature = radiant_kelvin_legacy / 100.0
        p5 = (308.7 - 0.028 * internal_heat_w_m2) + p2 * _fourth_power(scaled_radiant_temperature)
        xn = initial_clothing_surface / 100.0
        xf = initial_clothing_surface / 50.0
        updates = 0
        natural_convection = 0.0

        while abs(xn - xf) > CLOTHING_CONVERGENCE_EPSILON:
            if updates >= MAX_CLOTHING_ITERATION_UPDATES:
                return NumericalFailure(
                    NumericalFailureCode.HEAT_BALANCE_NON_CONVERGENCE,
                    None,
                    "clothing-surface iteration exceeded 150 updates",
                )
            xf = (xf + xn) / 2.0
            natural_convection = 2.38 * abs(100.0 * xf - air_kelvin_legacy) ** 0.25
            convection = max(natural_convection, forced_convection)
            xn = (p5 + p4 * convection - p2 * _fourth_power(xf)) / (100.0 + p3 * convection)
            updates += 1

        clothing_surface_c = 100.0 * xn - LEGACY_ABSOLUTE_OFFSET
        skin_diffusion = 3.05 * 0.001 * (5733.0 - 6.99 * internal_heat_w_m2 - vapor_pressure_pa)
        sweating = (
            0.42 * (internal_heat_w_m2 - MET_TO_W_M2) if internal_heat_w_m2 > MET_TO_W_M2 else 0.0
        )
        latent_respiration = 1.7 * 0.00001 * metabolic_rate_w_m2 * (5867.0 - vapor_pressure_pa)
        dry_respiration = 0.0014 * metabolic_rate_w_m2 * (34.0 - tdb)
        radiation = (
            3.96
            * clothing_area_factor
            * (_fourth_power(xn) - _fourth_power(scaled_radiant_temperature))
        )
        convection_loss = clothing_area_factor * convection * (clothing_surface_c - tdb)
        raw_thermal_load = (
            internal_heat_w_m2
            - skin_diffusion
            - sweating
            - latent_respiration
            - dry_respiration
            - radiation
            - convection_loss
        )
        pmv_transfer_coefficient = 0.303 * math.exp(-0.036 * metabolic_rate_w_m2) + 0.028
        athb_transfer_coefficient = 0.303 * math.exp(-0.036 * met * MET_TO_W_M2) + 0.028
        thermal_load = (pmv_transfer_coefficient * raw_thermal_load) / athb_transfer_coefficient
    except (OverflowError, ValueError, ZeroDivisionError) as error:
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            None,
            f"heat-balance arithmetic failed: {type(error).__name__}",
        )

    outputs = (thermal_load, clothing_surface_c, convection)
    if not all(math.isfinite(value) for value in outputs):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            None,
            "heat-balance arithmetic produced a non-finite value",
        )
    convection_mode = "natural" if natural_convection >= forced_convection else "forced"
    return HeatBalanceSuccess(
        thermal_load_w_m2=thermal_load,
        clothing_surface_temperature_c=clothing_surface_c,
        convective_heat_transfer_w_m2_k=convection,
        clothing_iteration_updates=updates,
        convection_mode=convection_mode,
    )

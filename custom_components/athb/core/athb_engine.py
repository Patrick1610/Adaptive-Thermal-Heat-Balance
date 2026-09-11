"""Frozen scalar ATHB 2022 forward engine."""

from __future__ import annotations

import math

from .contracts import (
    ApplicabilityReason,
    AthbInputs,
    AthbResult,
    AthbSuccess,
    AutomaticClothing,
    FixedClothing,
    HeatBalanceSuccess,
    NumericalFailure,
    NumericalFailureCode,
)
from .pmv_core import calculate_heat_balance

FORMULATION_ID = "athb_2022_ptc_4_4_2"
NUMERICAL_CONTRACT_VERSION = 1
ADAPTATION_MET_CONVERSION = 58.2
_THREE_DECIMAL_SCALE = 1000.0


def _round_three_decimals(value: float) -> float:
    """Match the pinned NumPy binary64 ``around(value, 3)`` scalar path.

    The engine only calls this helper with finite values inside its bounded
    engineering domain. Scaling first and rounding the resulting binary64
    value to the nearest even integer reproduces NumPy's three-decimal path;
    ``copysign`` also retains NumPy's signed-zero result.
    """

    magnitude = float(round(abs(value) * _THREE_DECIMAL_SCALE)) / _THREE_DECIMAL_SCALE
    return math.copysign(magnitude, value)


def _numeric_value(value: object, field: str) -> float | NumericalFailure:
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
        result = float(value)
    except OverflowError:
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            field,
            f"{field} must be finite",
        )
    if not math.isfinite(result):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            field,
            f"{field} must be finite",
        )
    return result


def _outside(field: str, value: float, requirement: str) -> NumericalFailure:
    return NumericalFailure(
        NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
        field,
        f"{field}={value!r} is outside {requirement}",
    )


def relative_air_speed(
    ambient_air_speed_m_s: float,
    original_met: float,
) -> float | NumericalFailure:
    """Convert ambient speed using the original activity value exactly once."""

    speed = _numeric_value(ambient_air_speed_m_s, "ambient_air_speed_m_s")
    if isinstance(speed, NumericalFailure):
        return speed
    met = _numeric_value(original_met, "met")
    if isinstance(met, NumericalFailure):
        return met
    if not 0.0 <= speed <= 2.0:
        return _outside("ambient_air_speed_m_s", speed, "[0, 2] metres per second")
    if not 0.8 <= met <= 2.0:
        return _outside("met", met, "[0.8, 2.0] met")
    relative = speed if met <= 1.0 else _round_three_decimals(speed + 0.3 * (met - 1.0))
    if not 0.0 <= relative <= 2.0:
        return _outside(
            "relative_air_speed_m_s",
            relative,
            "[0, 2] metres per second after movement conversion",
        )
    return relative


def _validated_inputs(inputs: AthbInputs) -> tuple[float, ...] | NumericalFailure:
    fields = (
        ("tdb_c", inputs.tdb_c),
        ("tr_c", inputs.tr_c),
        ("relative_air_speed_m_s", inputs.relative_air_speed_m_s),
        ("rh_pct", inputs.rh_pct),
        ("met", inputs.met),
        ("running_mean_c", inputs.running_mean_c),
    )
    validated: list[float] = []
    for field, value in fields:
        numeric = _numeric_value(value, field)
        if isinstance(numeric, NumericalFailure):
            return numeric
        validated.append(numeric)
    tdb, tr, speed, rh, met, running_mean = validated
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
        ("met", met, 0.8 <= met <= 2.0, "[0.8, 2.0] met"),
        (
            "running_mean_c",
            running_mean,
            -30.0 <= running_mean <= 45.0,
            "[-30, 45] degrees Celsius",
        ),
    )
    for field, value, accepted, requirement in domain_checks:
        if not accepted:
            return _outside(field, value, requirement)
    return tuple(validated)


def _effective_clothing(
    clothing: object,
    adapted_met: float,
    running_mean_c: float,
) -> float | NumericalFailure:
    if isinstance(clothing, AutomaticClothing):
        effective = float(
            10.0
            ** (
                -0.17168
                - 0.000485 * running_mean_c
                + 0.08176 * adapted_met
                - 0.00527 * running_mean_c * adapted_met
            )
        )
        if not math.isfinite(effective):
            return NumericalFailure(
                NumericalFailureCode.NON_FINITE,
                "clothing",
                "automatic clothing calculation produced a non-finite value",
            )
        if not 0.0 < effective <= 3.0:
            return _outside("clothing", effective, "the automatic interval (0, 3] clo")
        return effective
    if isinstance(clothing, FixedClothing):
        fixed = _numeric_value(clothing.clo, "clothing.clo")
        if isinstance(fixed, NumericalFailure):
            return fixed
        if not 0.1 <= fixed <= 2.0:
            return _outside("clothing.clo", fixed, "[0.1, 2.0] clo")
        return fixed
    if isinstance(clothing, bool):
        return NumericalFailure(
            NumericalFailureCode.BOOLEAN_INPUT,
            "clothing",
            "automatic clothing requires the explicit AutomaticClothing tag",
        )
    return NumericalFailure(
        NumericalFailureCode.NON_NUMERIC,
        "clothing",
        "clothing must be an AutomaticClothing or FixedClothing tag",
    )


def _applicability(
    *,
    tdb_c: float,
    tr_c: float,
    relative_air_speed_m_s: float,
    rh_pct: float,
    met: float,
    running_mean_c: float,
    effective_clo: float,
) -> tuple[ApplicabilityReason, ...]:
    reasons: list[ApplicabilityReason] = []
    if not (14.0 <= tdb_c <= 27.0 and 14.0 <= tr_c <= 27.0 and 16.0 <= running_mean_c <= 30.0):
        reasons.append(ApplicabilityReason.LIMITED_EVIDENCE)
    radiant_delta = tr_c - tdb_c
    if not (
        12.6 <= tdb_c <= 38.5
        and 12.6 <= tr_c <= 38.5
        and 16.9 <= rh_pct <= 87.7
        and relative_air_speed_m_s <= 1.9
        and -2.7 <= running_mean_c <= 41.3
        and -7.4 <= radiant_delta <= 9.2
        and 0.1 <= effective_clo <= 2.0
        and met <= 2.6
    ):
        reasons.append(ApplicabilityReason.EXTRAPOLATED)
    return tuple(reasons)


def evaluate_athb(inputs: AthbInputs) -> AthbResult:
    """Evaluate one forward ATHB vector without clipping the sensation vote."""

    validated = _validated_inputs(inputs)
    if isinstance(validated, NumericalFailure):
        return validated
    tdb, tr, speed, rh, met, running_mean = validated
    adapted_met = met - (0.234 * running_mean) / ADAPTATION_MET_CONVERSION
    if not math.isfinite(adapted_met):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            "adapted_met",
            "physiological adaptation produced a non-finite value",
        )
    if adapted_met <= 0.0:
        return _outside("adapted_met", adapted_met, "the open interval (0, infinity) met")

    effective_clo = _effective_clothing(inputs.clothing, adapted_met, running_mean)
    if isinstance(effective_clo, NumericalFailure):
        return effective_clo
    heat_balance = calculate_heat_balance(
        tdb_c=tdb,
        tr_c=tr,
        relative_air_speed_m_s=speed,
        rh_pct=rh,
        adapted_met=adapted_met,
        effective_clo=effective_clo,
    )
    if not isinstance(heat_balance, HeatBalanceSuccess):
        return heat_balance
    load = heat_balance.thermal_load_w_m2
    sensation_vote = (
        1.484
        + 0.0276 * load
        - 0.9602 * adapted_met
        - 0.0342 * running_mean
        + 0.0002264 * load * running_mean
        + 0.018696 * adapted_met * running_mean
        - 0.0002909 * load * adapted_met * running_mean
    )
    if not math.isfinite(sensation_vote):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE,
            None,
            "ATHB transfer function produced a non-finite value",
        )
    return AthbSuccess(
        sensation_vote=sensation_vote,
        public_sensation_vote=_round_three_decimals(sensation_vote),
        adapted_met=adapted_met,
        effective_clo=effective_clo,
        relative_air_speed_m_s=speed,
        thermal_load_w_m2=load,
        applicability_reasons=_applicability(
            tdb_c=tdb,
            tr_c=tr,
            relative_air_speed_m_s=speed,
            rh_pct=rh,
            met=met,
            running_mean_c=running_mean,
            effective_clo=effective_clo,
        ),
    )

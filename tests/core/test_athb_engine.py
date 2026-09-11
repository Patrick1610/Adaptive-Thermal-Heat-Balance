"""Validation and semantic tests for the frozen forward ATHB engine."""

from __future__ import annotations

import math
from typing import Any

import pytest

from custom_components.athb.core import athb_engine
from custom_components.athb.core.contracts import (
    AUTOMATIC_CLOTHING,
    ApplicabilityReason,
    AthbInputs,
    AthbSuccess,
    AutomaticClothing,
    FixedClothing,
    HeatBalanceSuccess,
    NumericalFailure,
    NumericalFailureCode,
)

BASE = {
    "tdb_c": 25.0,
    "tr_c": 25.0,
    "relative_air_speed_m_s": 0.1,
    "rh_pct": 50.0,
    "met": 1.2,
    "running_mean_c": 20.0,
    "clothing": AUTOMATIC_CLOTHING,
}


def _evaluate(**changes: Any) -> AthbSuccess | NumericalFailure:
    return athb_engine.evaluate_athb(AthbInputs(**(BASE | changes)))


def test_frozen_identity_and_published_forward_anchor() -> None:
    result = _evaluate()

    assert athb_engine.FORMULATION_ID == "athb_2022_ptc_4_4_2"
    assert athb_engine.NUMERICAL_CONTRACT_VERSION == 1
    assert athb_engine.ADAPTATION_MET_CONVERSION == 58.2
    assert isinstance(result, AthbSuccess)
    assert result.sensation_vote == pytest.approx(0.20625179757427767, abs=1e-10)
    assert result.public_sensation_vote == 0.206
    assert result.thermal_load_w_m2 == pytest.approx(2.445656581763135, abs=1e-10)


def test_fixed_clothing_is_unchanged_while_metabolism_is_adapted() -> None:
    result = _evaluate(clothing=FixedClothing(0.7))

    assert isinstance(result, AthbSuccess)
    assert result.effective_clo == 0.7
    assert result.adapted_met == pytest.approx(1.2 - (0.234 * 20.0) / 58.2)


def test_relative_air_speed_uses_original_activity_once() -> None:
    assert athb_engine.relative_air_speed(0.4, 1.0) == 0.4
    assert athb_engine.relative_air_speed(0.4, 1.7) == 0.61
    assert athb_engine.relative_air_speed(0.1236, 1.1) == 0.154
    assert athb_engine.relative_air_speed(0.4665, 1.5) == 0.616


def test_three_decimal_rounding_matches_pinned_numpy_ties_and_signed_zero() -> None:
    assert round(0.6165, 3) == 0.617
    assert athb_engine._round_three_decimals(0.6165) == 0.616
    assert round(-1.1865, 3) == -1.187
    assert athb_engine._round_three_decimals(-1.1865) == -1.186
    negative_zero = athb_engine._round_three_decimals(-0.0004)
    assert negative_zero == 0.0
    assert math.copysign(1.0, negative_zero) == -1.0


@pytest.mark.parametrize(
    ("speed", "met", "code", "field"),
    [
        (True, 1.1, NumericalFailureCode.BOOLEAN_INPUT, "ambient_air_speed_m_s"),
        ("0.1", 1.1, NumericalFailureCode.NON_NUMERIC, "ambient_air_speed_m_s"),
        (float("nan"), 1.1, NumericalFailureCode.NON_FINITE, "ambient_air_speed_m_s"),
        (0.1, False, NumericalFailureCode.BOOLEAN_INPUT, "met"),
        (0.1, object(), NumericalFailureCode.NON_NUMERIC, "met"),
        (0.1, float("inf"), NumericalFailureCode.NON_FINITE, "met"),
        (-0.1, 1.1, NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "ambient_air_speed_m_s"),
        (2.1, 1.1, NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "ambient_air_speed_m_s"),
        (0.1, 0.79, NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "met"),
        (0.1, 2.01, NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "met"),
        (2.0, 2.0, NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "relative_air_speed_m_s"),
    ],
)
def test_relative_air_speed_typed_failures(
    speed: object,
    met: object,
    code: NumericalFailureCode,
    field: str,
) -> None:
    result = athb_engine.relative_air_speed(speed, met)  # type: ignore[arg-type]

    assert isinstance(result, NumericalFailure)
    assert result.code is code
    assert result.field == field


@pytest.mark.parametrize(
    ("speed", "met", "field"),
    [
        (10**1000, 1.1, "ambient_air_speed_m_s"),
        (0.1, 10**1000, "met"),
    ],
)
def test_both_relative_speed_inputs_reject_unrepresentable_int(
    speed: int | float,
    met: int | float,
    field: str,
) -> None:
    result = athb_engine.relative_air_speed(speed, met)

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert result.field == field


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("tdb_c", True, NumericalFailureCode.BOOLEAN_INPUT),
        ("tr_c", "20", NumericalFailureCode.NON_NUMERIC),
        ("relative_air_speed_m_s", float("nan"), NumericalFailureCode.NON_FINITE),
        ("rh_pct", float("inf"), NumericalFailureCode.NON_FINITE),
        ("met", object(), NumericalFailureCode.NON_NUMERIC),
        ("running_mean_c", False, NumericalFailureCode.BOOLEAN_INPUT),
    ],
)
def test_forward_rejects_non_scalar_or_nonfinite_inputs(
    field: str,
    value: object,
    code: NumericalFailureCode,
) -> None:
    result = _evaluate(**{field: value})

    assert isinstance(result, NumericalFailure)
    assert result.code is code
    assert result.field == field


@pytest.mark.parametrize(
    "field",
    ["tdb_c", "tr_c", "relative_air_speed_m_s", "rh_pct", "met", "running_mean_c"],
)
def test_all_six_forward_numeric_inputs_reject_unrepresentable_int(field: str) -> None:
    result = _evaluate(**{field: 10**1000})

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert result.field == field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tdb_c", 4.999),
        ("tdb_c", 40.001),
        ("tr_c", -0.001),
        ("tr_c", 50.001),
        ("relative_air_speed_m_s", -0.001),
        ("relative_air_speed_m_s", 2.001),
        ("rh_pct", -0.001),
        ("rh_pct", 100.001),
        ("met", 0.799),
        ("met", 2.001),
        ("running_mean_c", -30.001),
        ("running_mean_c", 45.001),
    ],
)
def test_forward_rejects_each_engineering_domain_violation(field: str, value: float) -> None:
    result = _evaluate(**{field: value})

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN
    assert result.field == field


@pytest.mark.parametrize(
    ("clothing", "code", "field"),
    [
        (False, NumericalFailureCode.BOOLEAN_INPUT, "clothing"),
        (0, NumericalFailureCode.NON_NUMERIC, "clothing"),
        (None, NumericalFailureCode.NON_NUMERIC, "clothing"),
        (FixedClothing(True), NumericalFailureCode.BOOLEAN_INPUT, "clothing.clo"),
        (FixedClothing(float("nan")), NumericalFailureCode.NON_FINITE, "clothing.clo"),
        (FixedClothing(0.099), NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "clothing.clo"),
        (FixedClothing(2.001), NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN, "clothing.clo"),
    ],
)
def test_clothing_requires_explicit_valid_tag(
    clothing: object,
    code: NumericalFailureCode,
    field: str,
) -> None:
    result = _evaluate(clothing=clothing)

    assert isinstance(result, NumericalFailure)
    assert result.code is code
    assert result.field == field


def test_fixed_clothing_rejects_unrepresentable_int() -> None:
    result = _evaluate(clothing=FixedClothing(10**1000))

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert result.field == "clothing.clo"


def test_automatic_clothing_defensive_domain_and_arithmetic_failures() -> None:
    nonfinite = athb_engine._effective_clothing(AutomaticClothing(), float("nan"), 20.0)
    outside = athb_engine._effective_clothing(AutomaticClothing(), -100.0, 45.0)

    assert isinstance(nonfinite, NumericalFailure)
    assert nonfinite.code is NumericalFailureCode.NON_FINITE
    assert isinstance(outside, NumericalFailure)
    assert outside.code is NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN


def test_closed_engineering_boundaries_are_evaluated() -> None:
    lower = _evaluate(
        tdb_c=5.0,
        tr_c=0.0,
        relative_air_speed_m_s=0.0,
        rh_pct=0.0,
        met=0.8,
        running_mean_c=-30.0,
        clothing=FixedClothing(0.1),
    )
    upper = _evaluate(
        tdb_c=40.0,
        tr_c=50.0,
        relative_air_speed_m_s=2.0,
        rh_pct=100.0,
        met=2.0,
        running_mean_c=45.0,
        clothing=FixedClothing(2.0),
    )

    assert isinstance(lower, AthbSuccess)
    assert isinstance(upper, AthbSuccess)
    assert lower.sensation_vote < -3.0
    assert lower.public_sensation_vote == -5.38


def test_applicability_reasons_are_orthogonal_to_success() -> None:
    within = _evaluate(running_mean_c=20.0)
    limited = _evaluate(running_mean_c=5.0)
    extrapolated = _evaluate(rh_pct=0.0, running_mean_c=20.0)
    both = _evaluate(tdb_c=5.0, tr_c=5.0, running_mean_c=-30.0)

    assert isinstance(within, AthbSuccess)
    assert within.applicability_reasons == ()
    assert isinstance(limited, AthbSuccess)
    assert limited.applicability_reasons == (ApplicabilityReason.LIMITED_EVIDENCE,)
    assert isinstance(extrapolated, AthbSuccess)
    assert extrapolated.applicability_reasons == (ApplicabilityReason.EXTRAPOLATED,)
    assert isinstance(both, AthbSuccess)
    assert both.applicability_reasons == (
        ApplicabilityReason.LIMITED_EVIDENCE,
        ApplicabilityReason.EXTRAPOLATED,
    )


def test_adapted_met_checks_remain_typed_under_defensive_constant_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(athb_engine, "ADAPTATION_MET_CONVERSION", float("nan"))
    nonfinite = _evaluate()
    monkeypatch.setattr(athb_engine, "ADAPTATION_MET_CONVERSION", 1.0)
    nonpositive = _evaluate(running_mean_c=20.0, met=1.2)

    assert isinstance(nonfinite, NumericalFailure)
    assert nonfinite.code is NumericalFailureCode.NON_FINITE
    assert nonfinite.field == "adapted_met"
    assert isinstance(nonpositive, NumericalFailure)
    assert nonpositive.code is NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN
    assert nonpositive.field == "adapted_met"


def test_heat_balance_failure_is_propagated_without_numeric_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = NumericalFailure(
        NumericalFailureCode.HEAT_BALANCE_NON_CONVERGENCE,
        None,
        "fixture failure",
    )
    monkeypatch.setattr(athb_engine, "calculate_heat_balance", lambda **_: expected)

    assert _evaluate() is expected


def test_nonfinite_transfer_result_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    heat_balance = HeatBalanceSuccess(float("inf"), 30.0, 4.0, 10, "forced")
    monkeypatch.setattr(athb_engine, "calculate_heat_balance", lambda **_: heat_balance)

    result = _evaluate()

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert result.field is None

"""Unit tests for the scalar standard-library heat-balance subset."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.athb.core import pmv_core
from custom_components.athb.core.contracts import (
    HeatBalanceSuccess,
    NumericalFailure,
    NumericalFailureCode,
)

BASE = {
    "tdb_c": 25.0,
    "tr_c": 25.0,
    "relative_air_speed_m_s": 0.1,
    "rh_pct": 50.0,
    "adapted_met": 1.1195876288659794,
    "effective_clo": 0.6196592662892141,
}


def _calculate(**changes: Any) -> HeatBalanceSuccess | NumericalFailure:
    values = BASE | changes
    return pmv_core.calculate_heat_balance(**values)


def test_published_anchor_heat_load_matches_oracle() -> None:
    result = _calculate()

    assert isinstance(result, HeatBalanceSuccess)
    assert result.thermal_load_w_m2 == pytest.approx(2.445656581763135, abs=1e-10)
    assert result.clothing_surface_temperature_c == pytest.approx(29.752632956141156, abs=1e-10)
    assert result.clothing_iteration_updates == 10
    assert result.convection_mode == "forced"


def test_natural_convection_and_low_clothing_area_branch() -> None:
    result = _calculate(
        tdb_c=18.0,
        tr_c=20.0,
        relative_air_speed_m_s=0.05,
        rh_pct=65.0,
        adapted_met=0.9,
        effective_clo=2.0,
    )
    low_clothing = _calculate(effective_clo=0.1)

    assert isinstance(result, HeatBalanceSuccess)
    assert result.convection_mode == "natural"
    assert isinstance(low_clothing, HeatBalanceSuccess)
    assert low_clothing.thermal_load_w_m2 != result.thermal_load_w_m2


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("tdb_c", True, NumericalFailureCode.BOOLEAN_INPUT),
        ("tr_c", "20", NumericalFailureCode.NON_NUMERIC),
        ("relative_air_speed_m_s", float("nan"), NumericalFailureCode.NON_FINITE),
        ("rh_pct", float("inf"), NumericalFailureCode.NON_FINITE),
        ("adapted_met", object(), NumericalFailureCode.NON_NUMERIC),
        ("effective_clo", False, NumericalFailureCode.BOOLEAN_INPUT),
    ],
)
def test_kernel_rejects_non_scalar_or_nonfinite_inputs(
    field: str,
    value: object,
    code: NumericalFailureCode,
) -> None:
    result = _calculate(**{field: value})

    assert isinstance(result, NumericalFailure)
    assert result.code is code
    assert result.field == field


@pytest.mark.parametrize("field", tuple(BASE))
def test_all_six_kernel_numeric_inputs_reject_unrepresentable_int(field: str) -> None:
    result = _calculate(**{field: 10**1000})

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
        ("adapted_met", 0.0),
        ("effective_clo", 0.0),
        ("effective_clo", 3.001),
    ],
)
def test_kernel_rejects_each_engineering_domain_violation(field: str, value: float) -> None:
    result = _calculate(**{field: value})

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN
    assert result.field == field


def test_kernel_accepts_all_closed_boundaries() -> None:
    lower = _calculate(
        tdb_c=5.0,
        tr_c=0.0,
        relative_air_speed_m_s=0.0,
        rh_pct=0.0,
        adapted_met=0.0001,
        effective_clo=0.0001,
    )
    upper = _calculate(
        tdb_c=40.0,
        tr_c=50.0,
        relative_air_speed_m_s=2.0,
        rh_pct=100.0,
        adapted_met=2.0,
        effective_clo=3.0,
    )

    assert isinstance(lower, HeatBalanceSuccess)
    assert isinstance(upper, HeatBalanceSuccess)


def test_iteration_budget_returns_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pmv_core, "MAX_CLOTHING_ITERATION_UPDATES", 0)

    result = _calculate()

    assert result == NumericalFailure(
        NumericalFailureCode.HEAT_BALANCE_NON_CONVERGENCE,
        None,
        "clothing-surface iteration exceeded 150 updates",
    )


def test_arithmetic_exception_returns_typed_nonfinite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_overflow(_: float) -> float:
        raise OverflowError

    monkeypatch.setattr(pmv_core.math, "exp", raise_overflow)

    result = _calculate()

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert result.field is None
    assert "OverflowError" in result.detail


def test_nonfinite_arithmetic_output_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pmv_core.math, "exp", lambda _: float("inf"))

    result = _calculate()

    assert isinstance(result, NumericalFailure)
    assert result.code is NumericalFailureCode.NON_FINITE
    assert "produced a non-finite value" in result.detail

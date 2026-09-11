"""Psychrometric contract, anchor, and failure tests."""

from __future__ import annotations

import math

import pytest

from custom_components.athb.core.contracts import (
    DeclaredRelativeHumidity,
    DewPointStatus,
    MeasuredRelativeHumidity,
    MoistureFailureCode,
    Provenance,
)
from custom_components.athb.core.psychrometrics import (
    CandidateHumidity,
    DewPointResult,
    HumidityRatio,
    MoistureFailure,
    MoistureState,
    candidate_relative_humidity,
    dew_or_frost_point,
    humidity_ratio,
    moisture_state,
    saturation_vapor_pressure_pa,
)


def _state(temperature: float, rh: float, *, declared: bool = False) -> MoistureState:
    source = DeclaredRelativeHumidity(rh) if declared else MeasuredRelativeHumidity(rh)
    result = moisture_state(temperature, source)
    assert isinstance(result, MoistureState)
    return result


def test_reference_anchors_preserve_vapor_pressure() -> None:
    state = _state(20.0, 50.0)
    assert state.vapor_pressure_pa == pytest.approx(1169.40185, abs=0.001)

    warmed = candidate_relative_humidity(state, 25.0)
    surface = candidate_relative_humidity(state, 12.0)
    assert isinstance(warmed, CandidateHumidity)
    assert isinstance(surface, CandidateHumidity)
    assert warmed.relative_humidity_pct == pytest.approx(36.9, abs=0.02)
    assert surface.relative_humidity_pct == pytest.approx(83.37, abs=0.02)
    assert warmed.vapor_pressure_pa == surface.vapor_pressure_pa == state.vapor_pressure_pa


def test_declared_provenance_survives_all_derivations() -> None:
    state = _state(21.0, 45.0, declared=True)
    candidate = candidate_relative_humidity(state, 25.0)
    ratio = humidity_ratio(state, 101325.0)
    assert state.provenance is Provenance.DECLARED
    assert isinstance(candidate, CandidateHumidity)
    assert candidate.provenance is Provenance.DECLARED
    assert isinstance(ratio, HumidityRatio)
    assert ratio.provenance is Provenance.DECLARED


@pytest.mark.parametrize("temperature", [-100.0, -30.0, 0.0, 0.01, 0.011, 20.0, 200.0])
def test_saturation_pressure_is_positive_and_monotonic(temperature: float) -> None:
    result = saturation_vapor_pressure_pa(temperature)
    assert isinstance(result, float)
    assert result > 0.0


def test_triple_point_boundary_is_continuous() -> None:
    below = saturation_vapor_pressure_pa(math.nextafter(0.01, -math.inf))
    at = saturation_vapor_pressure_pa(0.01)
    above = saturation_vapor_pressure_pa(math.nextafter(0.01, math.inf))
    assert isinstance(below, float)
    assert isinstance(at, float)
    assert isinstance(above, float)
    assert abs(below - at) < 1e-9
    assert abs(above - at) < 0.01


@pytest.mark.parametrize(("temperature", "rh"), [(20.0, 50.0), (-10.0, 80.0), (35.0, 5.0)])
def test_dew_point_round_trip(temperature: float, rh: float) -> None:
    state = _state(temperature, rh)
    result = dew_or_frost_point(state)
    assert isinstance(result, DewPointResult)
    assert result.status is DewPointStatus.SOLVED
    assert result.temperature_c is not None
    saturation = saturation_vapor_pressure_pa(result.temperature_c)
    assert isinstance(saturation, float)
    assert saturation == pytest.approx(state.vapor_pressure_pa, rel=0.0002)


def test_dry_limit_is_explicit_none_not_negative_infinity() -> None:
    result = dew_or_frost_point(_state(20.0, 0.0))
    assert result == DewPointResult(None, DewPointStatus.DRY_LIMIT)


def test_saturation_crossing_is_not_clamped_or_silently_changed() -> None:
    state = _state(28.0, 80.0)
    result = candidate_relative_humidity(state, 20.0)
    assert isinstance(result, MoistureFailure)
    assert result.code is MoistureFailureCode.MOISTURE_LIMITED_NO_SOLUTION


def test_tiny_saturation_overshoot_is_normalized_only_within_tolerance() -> None:
    saturation = saturation_vapor_pressure_pa(20.0)
    assert isinstance(saturation, float)
    state = MoistureState(20.0, 100.0, saturation * (1.0 + 5e-9), Provenance.MEASURED)
    result = candidate_relative_humidity(state, 20.0)
    assert isinstance(result, CandidateHumidity)
    assert result.relative_humidity_pct == 100.0


@pytest.mark.parametrize("rh", [-0.001, 100.001, math.inf, True, "50"])
def test_invalid_starting_relative_humidity_is_typed(rh: object) -> None:
    result = moisture_state(20.0, MeasuredRelativeHumidity(rh))  # type: ignore[arg-type]
    assert isinstance(result, MoistureFailure)


@pytest.mark.parametrize("temperature", [-100.001, 200.001, math.nan, True, "20"])
def test_invalid_saturation_temperature_is_typed(temperature: object) -> None:
    result = saturation_vapor_pressure_pa(temperature)  # type: ignore[arg-type]
    assert isinstance(result, MoistureFailure)


def test_huge_integer_inputs_are_typed_without_overflow() -> None:
    huge = 10**10000
    saturation = saturation_vapor_pressure_pa(huge)
    state = moisture_state(huge, MeasuredRelativeHumidity(50.0))
    rh = moisture_state(20.0, MeasuredRelativeHumidity(huge))
    assert isinstance(saturation, MoistureFailure)
    assert isinstance(state, MoistureFailure)
    assert isinstance(rh, MoistureFailure)


def test_invalid_candidate_temperature_is_propagated() -> None:
    result = candidate_relative_humidity(_state(20.0, 50.0), math.nan)
    assert isinstance(result, MoistureFailure)
    assert result.code is MoistureFailureCode.NON_FINITE


def test_untagged_relative_humidity_is_rejected() -> None:
    result = moisture_state(20.0, 50.0)  # type: ignore[arg-type]
    assert isinstance(result, MoistureFailure)
    assert result.field == "relative_humidity"


@pytest.mark.parametrize(
    "vapor_pressure",
    [-1.0, math.inf, 10**10000],
    ids=("negative", "non-finite", "overflowing-integer"),
)
def test_invalid_constructed_moisture_state_is_rejected(vapor_pressure: float) -> None:
    state = MoistureState(20.0, 50.0, vapor_pressure, Provenance.MEASURED)
    candidate = candidate_relative_humidity(state, 21.0)
    dew_point = dew_or_frost_point(state)
    ratio = humidity_ratio(state, 101325.0)
    assert isinstance(candidate, MoistureFailure)
    assert isinstance(dew_point, MoistureFailure)
    assert isinstance(ratio, MoistureFailure)


def test_humidity_ratio_rejects_impossible_total_pressure() -> None:
    state = _state(20.0, 50.0)
    for pressure in (state.vapor_pressure_pa, 0.0, math.nan, True):
        result = humidity_ratio(state, pressure)  # type: ignore[arg-type]
        assert isinstance(result, MoistureFailure)


def test_dew_point_reports_below_supported_domain() -> None:
    low = saturation_vapor_pressure_pa(-100.0)
    assert isinstance(low, float)
    state = MoistureState(20.0, 0.0, low / 2.0, Provenance.MEASURED)
    result = dew_or_frost_point(state)
    assert isinstance(result, MoistureFailure)
    assert result.code is MoistureFailureCode.BELOW_PSYCHROMETRIC_DOMAIN


def test_manually_supersaturated_state_is_rejected_by_dew_inverse() -> None:
    saturation = saturation_vapor_pressure_pa(20.0)
    assert isinstance(saturation, float)
    state = MoistureState(20.0, 101.0, saturation * 1.01, Provenance.MEASURED)
    result = dew_or_frost_point(state)
    assert isinstance(result, MoistureFailure)
    assert result.code is MoistureFailureCode.MOISTURE_LIMITED_NO_SOLUTION

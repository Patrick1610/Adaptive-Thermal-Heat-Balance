"""Steady-state surface calibration and diagnostic tests."""

from __future__ import annotations

import math

import pytest

from custom_components.athb.core.contracts import MeasuredRelativeHumidity, Provenance
from custom_components.athb.core.psychrometrics import (
    MoistureState,
    moisture_state,
    saturation_vapor_pressure_pa,
)
from custom_components.athb.core.surface import (
    SurfaceCalibration,
    SurfaceEstimate,
    SurfaceFailure,
    SurfaceFailureCode,
    SurfaceHumidityDiagnostic,
    SurfaceThresholdTemperature,
    calibrate_surface_factor,
    estimate_surface_temperature,
    surface_humidity_diagnostic,
    surface_temperature_for_rh_threshold,
)


def _moisture() -> MoistureState:
    result = moisture_state(20.0, MeasuredRelativeHumidity(50.0))
    assert isinstance(result, MoistureState)
    return result


def test_calibration_and_direct_factor_estimate_are_equivalent() -> None:
    calibration = calibrate_surface_factor(
        indoor_temperature_c=20.0,
        outdoor_temperature_c=0.0,
        measured_surface_temperature_c=16.0,
    )
    assert calibration == SurfaceCalibration(0.8)
    estimate = estimate_surface_temperature(
        indoor_temperature_c=20.0,
        current_outdoor_temperature_c=0.0,
        f_rsi=0.8,
    )
    assert isinstance(estimate, SurfaceEstimate)
    assert estimate.temperature.value_c == 16.0
    assert estimate.temperature.provenance is Provenance.ESTIMATED
    assert estimate.reasons == ("steady_state_surface_estimate",)


@pytest.mark.parametrize(
    ("indoor", "outdoor", "surface", "code"),
    [
        (20.0, 16.0, 18.0, SurfaceFailureCode.INSUFFICIENT_CALIBRATION_DELTA),
        (20.0, 0.0, -1.0, SurfaceFailureCode.SURFACE_OUTSIDE_CALIBRATION_RANGE),
        (20.0, 0.0, 0.0, SurfaceFailureCode.INVALID_CALIBRATION_FACTOR),
        (math.nan, 0.0, 10.0, SurfaceFailureCode.INVALID_INPUT),
    ],
)
def test_invalid_surface_calibration_is_typed(
    indoor: float, outdoor: float, surface: float, code: SurfaceFailureCode
) -> None:
    result = calibrate_surface_factor(
        indoor_temperature_c=indoor,
        outdoor_temperature_c=outdoor,
        measured_surface_temperature_c=surface,
    )
    assert isinstance(result, SurfaceFailure)
    assert result.code is code


@pytest.mark.parametrize("factor", [0.0, -0.1, 1.01, math.inf, False])
def test_no_default_or_invalid_surface_factor_is_accepted(factor: float) -> None:
    result = estimate_surface_temperature(
        indoor_temperature_c=20.0,
        current_outdoor_temperature_c=0.0,
        f_rsi=factor,
    )
    assert isinstance(result, SurfaceFailure)


def test_surface_humidity_anchor_threshold_and_supersaturation_diagnostics() -> None:
    at_twelve = surface_humidity_diagnostic(moisture=_moisture(), surface_temperature_c=12.0)
    assert isinstance(at_twelve, SurfaceHumidityDiagnostic)
    assert at_twelve.uncapped_relative_humidity_pct == pytest.approx(83.37, abs=0.01)
    assert at_twelve.displayed_relative_humidity_pct == at_twelve.uncapped_relative_humidity_pct
    assert at_twelve.high_surface_humidity
    assert not at_twelve.predicted_saturation
    saturated = surface_humidity_diagnostic(moisture=_moisture(), surface_temperature_c=5.0)
    assert isinstance(saturated, SurfaceHumidityDiagnostic)
    assert saturated.uncapped_relative_humidity_pct > 100.0
    assert saturated.displayed_relative_humidity_pct == 100.0
    assert saturated.predicted_saturation
    assert "surface_humidity_diagnostic_only" in saturated.reasons
    assert "predicted_surface_saturation" in saturated.reasons
    ordinary = surface_humidity_diagnostic(moisture=_moisture(), surface_temperature_c=20.0)
    assert isinstance(ordinary, SurfaceHumidityDiagnostic)
    assert not ordinary.high_surface_humidity
    assert ordinary.reasons == ("surface_humidity_diagnostic_only",)


def test_surface_threshold_temperature_round_trips() -> None:
    result = surface_temperature_for_rh_threshold(moisture=_moisture(), threshold_pct=80.0)
    assert isinstance(result, SurfaceThresholdTemperature)
    diagnostic = surface_humidity_diagnostic(
        moisture=_moisture(),
        surface_temperature_c=result.temperature_c,
        high_threshold_pct=80.0,
    )
    assert isinstance(diagnostic, SurfaceHumidityDiagnostic)
    assert diagnostic.uncapped_relative_humidity_pct == pytest.approx(80.0, abs=0.01)


@pytest.mark.parametrize(
    ("surface_rh", "high_humidity", "condensation"),
    [
        (79.99, False, False),
        (80.0, True, False),
        (99.99, True, False),
        (100.0, True, True),
    ],
)
def test_fixed_surface_warning_and_condensation_boundaries(
    surface_rh: float,
    high_humidity: bool,
    condensation: bool,
) -> None:
    saturation = saturation_vapor_pressure_pa(20.0)
    assert isinstance(saturation, float)
    moisture = MoistureState(20.0, surface_rh, saturation * surface_rh / 100.0, Provenance.MEASURED)

    diagnostic = surface_humidity_diagnostic(moisture=moisture, surface_temperature_c=20.0)

    assert isinstance(diagnostic, SurfaceHumidityDiagnostic)
    assert diagnostic.high_surface_humidity is high_humidity
    assert diagnostic.predicted_saturation is condensation


@pytest.mark.parametrize("threshold", [0.0, -1.0, 101.0, math.nan, False])
def test_invalid_surface_thresholds_are_typed(threshold: float) -> None:
    diagnostic = surface_humidity_diagnostic(
        moisture=_moisture(), surface_temperature_c=12.0, high_threshold_pct=threshold
    )
    inverse = surface_temperature_for_rh_threshold(moisture=_moisture(), threshold_pct=threshold)
    assert isinstance(diagnostic, SurfaceFailure)
    assert isinstance(inverse, SurfaceFailure)


def test_dry_state_and_invalid_surface_temperature_do_not_invent_diagnostics() -> None:
    dry = moisture_state(20.0, MeasuredRelativeHumidity(0.0))
    assert isinstance(dry, MoistureState)
    threshold = surface_temperature_for_rh_threshold(moisture=dry, threshold_pct=80.0)
    assert isinstance(threshold, SurfaceFailure)
    assert threshold.code is SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN
    invalid = surface_humidity_diagnostic(moisture=_moisture(), surface_temperature_c=250.0)
    assert isinstance(invalid, SurfaceFailure)
    assert invalid.code is SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN


def test_surface_diagnostic_rejects_invalid_moisture_state() -> None:
    invalid = MoistureState(20.0, 50.0, -1.0, Provenance.MEASURED)
    result = surface_humidity_diagnostic(moisture=invalid, surface_temperature_c=12.0)
    assert isinstance(result, SurfaceFailure)
    assert result.code is SurfaceFailureCode.INVALID_INPUT


def test_surface_functions_reject_each_invalid_source_and_arithmetic_overflow() -> None:
    assert isinstance(
        calibrate_surface_factor(
            indoor_temperature_c=20.0,
            outdoor_temperature_c=math.nan,
            measured_surface_temperature_c=10.0,
        ),
        SurfaceFailure,
    )
    assert isinstance(
        calibrate_surface_factor(
            indoor_temperature_c=20.0,
            outdoor_temperature_c=0.0,
            measured_surface_temperature_c=math.nan,
        ),
        SurfaceFailure,
    )
    assert isinstance(
        estimate_surface_temperature(
            indoor_temperature_c=math.nan,
            current_outdoor_temperature_c=0.0,
            f_rsi=0.8,
        ),
        SurfaceFailure,
    )
    assert isinstance(
        estimate_surface_temperature(
            indoor_temperature_c=20.0,
            current_outdoor_temperature_c=math.nan,
            f_rsi=0.8,
        ),
        SurfaceFailure,
    )
    overflow = estimate_surface_temperature(
        indoor_temperature_c=1e308,
        current_outdoor_temperature_c=-1e308,
        f_rsi=0.5,
    )
    assert isinstance(overflow, SurfaceFailure)
    assert overflow.code is SurfaceFailureCode.INVALID_INPUT


def test_surface_threshold_outside_domain_and_nonfinite_vapor_are_typed() -> None:
    huge_pressure = MoistureState(20.0, 50.0, 1e308, Provenance.MEASURED)
    outside = surface_temperature_for_rh_threshold(moisture=huge_pressure, threshold_pct=80.0)
    assert isinstance(outside, SurfaceFailure)
    assert outside.code is SurfaceFailureCode.OUTSIDE_PSYCHROMETRIC_DOMAIN
    nonfinite = MoistureState(20.0, 50.0, math.inf, Provenance.MEASURED)
    diagnostic = surface_humidity_diagnostic(moisture=nonfinite, surface_temperature_c=12.0)
    inverse = surface_temperature_for_rh_threshold(moisture=nonfinite, threshold_pct=80.0)
    assert isinstance(diagnostic, SurfaceFailure)
    assert isinstance(inverse, SurfaceFailure)


def test_huge_integer_surface_input_is_typed() -> None:
    result = estimate_surface_temperature(
        indoor_temperature_c=10**10000,
        current_outdoor_temperature_c=0.0,
        f_rsi=0.8,
    )
    assert isinstance(result, SurfaceFailure)

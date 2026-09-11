"""Pure capability, unit, and inward-grid tests."""

from __future__ import annotations

import itertools

import pytest

from custom_components.athb.core.climate import (
    TARGET_TEMPERATURE,
    TARGET_TEMPERATURE_RANGE,
    AutoMapping,
    CapabilityMapping,
    ClimateCapabilitySnapshot,
    ClimateFailure,
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    StepUnit,
    TemperatureUnit,
    build_grid,
    celsius_delta_to_unit,
    celsius_to_ha,
    ha_to_celsius,
    normalize_range_target,
    normalize_scalar_target,
    resolve_capability,
    unit_delta_to_ha,
)
from custom_components.athb.core.contracts import ActuationDirection, TargetShape


def _snapshot(
    *,
    mode: str | None = "heat",
    features: int = TARGET_TEMPERATURE,
    unit: TemperatureUnit = TemperatureUnit.CELSIUS,
    minimum: float = 5.0,
    maximum: float = 35.0,
    step: float | None = 0.5,
    available: bool = True,
    restored: bool = False,
    feedback: bool = True,
) -> ClimateCapabilitySnapshot:
    return ClimateCapabilitySnapshot(
        mode,
        ("off", "heat", "cool", "heat_cool", "auto"),
        features,
        minimum,
        maximum,
        step,
        unit,
        available=available,
        restored=restored,
        setpoint_feedback_observable=feedback,
    )


@pytest.mark.parametrize(
    ("mode", "features", "mapping", "expected"),
    [
        ("off", 0, AutoMapping.UNMAPPED, ClimateFailure("hvac_off")),
        (
            "heat",
            1,
            AutoMapping.UNMAPPED,
            CapabilityMapping(ActuationDirection.HEATING_ONLY, TargetShape.SCALAR),
        ),
        ("heat", 2, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
        (
            "cool",
            1,
            AutoMapping.UNMAPPED,
            CapabilityMapping(ActuationDirection.COOLING_ONLY, TargetShape.SCALAR),
        ),
        ("cool", 2, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
        (
            "heat_cool",
            2,
            AutoMapping.UNMAPPED,
            CapabilityMapping(ActuationDirection.RANGED, TargetShape.RANGE),
        ),
        ("heat_cool", 1, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
        ("auto", 1, AutoMapping.UNMAPPED, "unsupported_auto_mapping"),
        ("auto", 1, AutoMapping.BIDIRECTIONAL_SCALAR, "unsupported_auto_mapping"),
        (
            "auto",
            1,
            AutoMapping.HEATING,
            CapabilityMapping(ActuationDirection.HEATING_ONLY, TargetShape.SCALAR),
        ),
        (
            "auto",
            1,
            AutoMapping.COOLING,
            CapabilityMapping(ActuationDirection.COOLING_ONLY, TargetShape.SCALAR),
        ),
        (
            "auto",
            2,
            AutoMapping.RANGE,
            CapabilityMapping(ActuationDirection.RANGED, TargetShape.RANGE),
        ),
        ("dry", 3, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
        ("fan_only", 3, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
        ("future", 3, AutoMapping.UNMAPPED, "unsupported_hvac_mode"),
    ],
)
def test_complete_capability_mode_matrix(
    mode: str,
    features: int,
    mapping: AutoMapping,
    expected: CapabilityMapping | ClimateFailure | str,
) -> None:
    result = resolve_capability(_snapshot(mode=mode, features=features), auto_mapping=mapping)
    if isinstance(expected, str):
        assert isinstance(result, ClimateFailure)
        assert result.reason == expected
    else:
        assert result == expected


def test_both_feature_flags_still_use_only_current_mode_shape() -> None:
    both = TARGET_TEMPERATURE | TARGET_TEMPERATURE_RANGE
    assert resolve_capability(_snapshot(mode="heat", features=both)) == CapabilityMapping(
        ActuationDirection.HEATING_ONLY, TargetShape.SCALAR
    )
    assert resolve_capability(_snapshot(mode="heat_cool", features=both)) == CapabilityMapping(
        ActuationDirection.RANGED, TargetShape.RANGE
    )


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(mode=None), "target_unavailable"),
        (_snapshot(mode="unknown"), "target_unavailable"),
        (_snapshot(available=False), "target_unavailable"),
        (_snapshot(restored=True), "restored_target_state"),
        (_snapshot(feedback=False), "setpoint_feedback_unavailable"),
    ],
)
def test_readiness_precedes_mode_mapping(snapshot: ClimateCapabilitySnapshot, reason: str) -> None:
    assert resolve_capability(snapshot) == ClimateFailure(reason)


def test_absolute_and_delta_unit_conversions_are_distinct() -> None:
    assert celsius_to_ha(20.0, TemperatureUnit.FAHRENHEIT) == pytest.approx(68.0)
    assert ha_to_celsius(68.0, TemperatureUnit.FAHRENHEIT) == pytest.approx(20.0)
    assert celsius_delta_to_unit(2.0, TemperatureUnit.FAHRENHEIT) == pytest.approx(3.6)
    assert unit_delta_to_ha(
        0.5, declared_unit=StepUnit.CELSIUS, ha_unit=TemperatureUnit.FAHRENHEIT
    ) == pytest.approx(0.9)
    assert unit_delta_to_ha(
        1.8, declared_unit=StepUnit.FAHRENHEIT, ha_unit=TemperatureUnit.CELSIUS
    ) == pytest.approx(1.0)


def test_coarse_grid_examples_round_strictly_inward() -> None:
    snapshot = _snapshot(minimum=18.0, maximum=30.0, step=2.0)
    options = GridOptions(18.0, 26.0)
    heating = normalize_scalar_target(
        requested_room_c=19.362709,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=snapshot,
        options=options,
    )
    cooling = normalize_scalar_target(
        requested_room_c=23.481038,
        direction=ActuationDirection.COOLING_ONLY,
        snapshot=snapshot,
        options=options,
    )
    assert isinstance(heating, NormalizedScalarTarget)
    assert isinstance(cooling, NormalizedScalarTarget)
    assert heating.normalized_ha == 20.0
    assert cooling.normalized_ha == 22.0
    assert heating.limitations == ("grid_inward_adjustment",)
    assert cooling.limitations == ("grid_inward_adjustment",)
    ranged = normalize_range_target(
        requested_heating_room_c=19.362709,
        requested_cooling_room_c=23.481038,
        snapshot=snapshot,
        options=options,
    )
    assert isinstance(ranged, NormalizedRangeTarget)
    assert (ranged.heating.normalized_ha, ranged.cooling.normalized_ha) == (20.0, 22.0)


def test_no_outward_grid_substitution_and_no_range_widening() -> None:
    assert normalize_range_target(
        requested_heating_room_c=19.362709,
        requested_cooling_room_c=23.481038,
        snapshot=_snapshot(minimum=18.0, maximum=30.0, step=4.0),
        options=GridOptions(18.0, 26.0),
    ) == ClimateFailure("no_legal_range")
    assert normalize_scalar_target(
        requested_room_c=25.9,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=_snapshot(minimum=18.0, maximum=30.0, step=2.0),
        options=GridOptions(18.0, 25.9),
    ) == ClimateFailure("no_legal_inward_target")


def test_exact_grid_values_survive_representational_noise() -> None:
    result = normalize_scalar_target(
        requested_room_c=19.3 + 2e-11,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=_snapshot(minimum=0.0, maximum=30.0, step=0.1),
        options=GridOptions(10.0, 25.0),
    )
    assert isinstance(result, NormalizedScalarTarget)
    assert result.normalized_ha == pytest.approx(19.3)
    assert "grid_inward_adjustment" not in result.limitations


def test_calibration_bounds_and_room_reference_are_preserved() -> None:
    result = normalize_scalar_target(
        requested_room_c=19.8,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=_snapshot(minimum=10.0, maximum=20.0, step=0.5),
        options=GridOptions(18.0, 24.0, calibration_offset_c=0.5),
    )
    assert isinstance(result, NormalizedScalarTarget)
    assert (result.calibrated_c, result.bounded_c, result.normalized_actuator_c) == (
        20.3,
        20.0,
        20.0,
    )
    assert result.normalized_room_c == 19.5
    assert result.limitations == ("device_bound_applied",)


def test_user_bound_is_recorded_before_grid_normalization() -> None:
    result = normalize_scalar_target(
        requested_room_c=16.0,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=_snapshot(minimum=5.0, maximum=35.0, step=1.0),
        options=GridOptions(18.2, 26.0),
    )
    assert isinstance(result, NormalizedScalarTarget)
    assert result.bounded_c == 18.2
    assert result.normalized_ha == 19.0
    assert result.limitations == ("user_bound_applied", "grid_inward_adjustment")


def test_fahrenheit_grid_uses_integer_indices_and_single_absolute_conversion() -> None:
    snapshot = _snapshot(unit=TemperatureUnit.FAHRENHEIT, minimum=50.0, maximum=86.0, step=1.0)
    options = GridOptions(18.0, 26.0, calibration_offset_c=0.5)
    heating = normalize_scalar_target(
        requested_room_c=19.362709,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=snapshot,
        options=options,
    )
    cooling = normalize_scalar_target(
        requested_room_c=23.481038,
        direction=ActuationDirection.COOLING_ONLY,
        snapshot=snapshot,
        options=options,
    )
    assert isinstance(heating, NormalizedScalarTarget)
    assert isinstance(cooling, NormalizedScalarTarget)
    assert heating.normalized_ha == 68.0
    assert cooling.normalized_ha == 75.0
    assert heating.normalized_room_c == pytest.approx(19.5)
    assert cooling.normalized_room_c == pytest.approx(23.3888888889)


def test_step_override_unit_correction_and_origin_override() -> None:
    grid = build_grid(
        _snapshot(unit=TemperatureUnit.FAHRENHEIT, minimum=50.0, maximum=86.0, step=1.0),
        GridOptions(
            10.0, 30.0, step_override=0.5, step_unit=StepUnit.CELSIUS, grid_origin_override_ha=50.0
        ),
    )
    assert not isinstance(grid, ClimateFailure)
    assert grid.step_ha == pytest.approx(0.9)
    assert grid.origin_ha == 50.0


def test_missing_step_uses_labelled_unit_specific_assumption() -> None:
    for unit, minimum, maximum, expected in (
        (TemperatureUnit.CELSIUS, 5.0, 35.0, 0.5),
        (TemperatureUnit.FAHRENHEIT, 40.0, 95.0, 1.0),
    ):
        result = normalize_scalar_target(
            requested_room_c=20.1,
            direction=ActuationDirection.HEATING_ONLY,
            snapshot=_snapshot(unit=unit, minimum=minimum, maximum=maximum, step=None),
            options=GridOptions(10.0, 30.0),
        )
        assert isinstance(result, NormalizedScalarTarget)
        assert result.grid.step_ha == expected
        assert "assumed_target_step" in result.limitations


def test_grid_point_limit_and_empty_intersection_fail_closed() -> None:
    assert build_grid(
        _snapshot(minimum=0.0, maximum=30.0, step=0.001), GridOptions(0.0, 30.0)
    ) == ClimateFailure("grid_too_fine")
    assert build_grid(
        _snapshot(minimum=5.0, maximum=15.0, step=1.0), GridOptions(18.0, 26.0)
    ) == ClimateFailure("no_legal_target_grid")


@pytest.mark.parametrize(
    "options",
    [
        GridOptions(20.0, 20.0),
        GridOptions(18.0, 26.0, calibration_offset_c=3.1),
        GridOptions(18.0, 26.0, step_override=0.0),
        GridOptions(18.0, 26.0, minimum_range_gap_c=0.5),
    ],
)
def test_invalid_grid_options_fail_closed(options: GridOptions) -> None:
    assert build_grid(_snapshot(), options) == ClimateFailure("invalid_grid_configuration")


def test_all_legal_scalar_results_are_directionally_inward() -> None:
    snapshot = _snapshot(minimum=0.0, maximum=30.0, step=0.5)
    options = GridOptions(10.0, 25.0)
    for request, direction in itertools.product(
        (10.0, 10.01, 19.24, 19.25, 19.26, 24.99, 25.0),
        (ActuationDirection.HEATING_ONLY, ActuationDirection.COOLING_ONLY),
    ):
        result = normalize_scalar_target(
            requested_room_c=request, direction=direction, snapshot=snapshot, options=options
        )
        assert isinstance(result, NormalizedScalarTarget)
        if direction is ActuationDirection.HEATING_ONLY:
            assert result.normalized_actuator_c + 1e-12 >= result.bounded_c
        else:
            assert result.normalized_actuator_c - 1e-12 <= result.bounded_c

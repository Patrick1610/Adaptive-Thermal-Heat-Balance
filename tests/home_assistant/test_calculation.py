"""Production HA snapshot-to-core calculation tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.adapters.sources import StateValue, validate_state_value
from custom_components.athb.calculation import (
    CapturedTarget,
    CapturedZoneSnapshot,
    calculate_runtime_snapshot,
    result_values,
)
from custom_components.athb.core.climate import ClimateCapabilitySnapshot, TemperatureUnit
from custom_components.athb.core.contracts import RootFailure, RootSuccess
from custom_components.athb.core.sources import SourceKind, SourceState

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


def _state(entity_id: str, value: str, unit: str) -> StateValue:
    return StateValue(entity_id, value, unit, NOW, True, {}, None, None)


def _target(*, mode: str = "heat", features: int = 1) -> CapturedTarget:
    return CapturedTarget(
        "target-1",
        "registry-1",
        "climate.living_room",
        ClimateCapabilitySnapshot(
            mode,
            ("off", "heat", "cool", "heat_cool"),
            features,
            16.0,
            30.0,
            0.5,
            TemperatureUnit.CELSIUS,
            scalar_target_ha=18.0,
            target_temp_low_ha=18.0 if features == 2 else None,
            target_temp_high_ha=26.0 if features == 2 else None,
        ),
    )


def _named_target(target_id: str, *, mode: str) -> CapturedTarget:
    return CapturedTarget(
        target_id,
        f"registry-{target_id}",
        f"climate.{target_id}",
        ClimateCapabilitySnapshot(
            mode,
            ("off", "heat", "cool"),
            1,
            16.0,
            30.0,
            0.5,
            TemperatureUnit.CELSIUS,
            scalar_target_ha=20.0,
        ),
    )


def test_registry_identity_change_starts_a_new_source_lineage() -> None:
    old = StateValue("sensor.room", "20", "°C", NOW, True, {}, None, None, "old-registry")
    observation, state = validate_state_value(old, kind=SourceKind.PRIMARY_AIR, now=NOW)
    assert observation.source_identity == "registry:old-registry"
    replacement = StateValue(
        "sensor.room",
        "21",
        "°C",
        NOW.replace(second=1),
        True,
        {},
        None,
        None,
        "new-registry",
    )

    changed, changed_state = validate_state_value(
        replacement,
        kind=SourceKind.PRIMARY_AIR,
        now=NOW.replace(second=1),
        prior=state,
    )

    assert changed.source_identity == "registry:new-registry"
    assert changed_state.recovering is False


def test_snapshot_adapter_preserves_declared_rh_and_adaptive_root_path() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"minimum_control_temperature": 18.0, "maximum_control_temperature": 26.0},
            (_target(),),
            explicit_transition=True,
        )
    )

    calculation = result.targets[0].result
    assert calculation is not None
    assert calculation.roots is not None
    assert isinstance(calculation.roots.heating_control, RootSuccess)
    assert result.relative_humidity_provenance == "declared"
    assert dict(result.provenance)["air_speed"] == "declared"
    assert dict(result.provenance)["rh"] == "declared"
    values = result_values(result)
    assert values["effective_targets"] == {"target-1": {"temperature": 19.5}}
    scenarios = values["target_scenarios"]["target-1"]
    assert scenarios["current"]["room"]["temperature"] == pytest.approx(19.362709, abs=0.005)
    assert scenarios["current"]["actuator"] == {"temperature": 19.5}
    assert scenarios["occupied"] == scenarios["current"]
    assert scenarios["unoccupied"]["room"]["temperature"] == pytest.approx(17.362709, abs=0.005)
    assert scenarios["unoccupied"]["actuator"] == {"temperature": 18.0}


def test_primary_freshness_option_supports_slow_reporting_sensor() -> None:
    slow_state = StateValue(
        "sensor.room",
        "20",
        "°C",
        NOW - timedelta(minutes=90),
        True,
        {},
        None,
        None,
    )
    snapshot = CapturedZoneSnapshot(
        NOW,
        slow_state,
        None,
        50.0,
        _state("sensor.outdoor", "5", "°C"),
        None,
        5.0,
        "complete_history",
        "balanced",
        "comfort",
        {
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
            "primary_temperature_freshness_minutes": 120.0,
        },
        (_target(),),
    )

    result = calculate_runtime_snapshot(snapshot)

    assert result.primary_value_c == 20.0
    assert result.suppression_reason is None
    assert result.targets[0].result is not None


def test_other_source_update_reuses_same_still_fresh_primary_observation() -> None:
    snapshot = CapturedZoneSnapshot(
        NOW,
        _state("sensor.room", "20", "°C"),
        _state("sensor.rh", "50", "%"),
        None,
        _state("sensor.outdoor", "5", "°C"),
        None,
        5.0,
        "complete_history",
        "balanced",
        "comfort",
        {"minimum_control_temperature": 18.0, "maximum_control_temperature": 26.0},
        (_target(),),
    )
    first = calculate_runtime_snapshot(snapshot)
    assert snapshot.relative_humidity is not None

    humidity_update = replace(
        snapshot,
        now=NOW + timedelta(minutes=1),
        relative_humidity=replace(
            snapshot.relative_humidity,
            raw_state="55",
            observed_at=NOW + timedelta(minutes=1),
        ),
        source_states=first.source_states,
    )
    second = calculate_runtime_snapshot(humidity_update)

    assert second.primary_value_c == 20.0
    assert second.relative_humidity_pct == 55.0
    assert second.hold_condition is None
    assert second.targets[0].result is not None


def test_humid_cooling_uses_valid_directional_root_despite_unrelated_failures() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "28", "°C"),
            _state("sensor.rh", "80", "%"),
            None,
            _state("sensor.outdoor", "25", "°C"),
            None,
            25.0,
            "complete_history",
            "balanced",
            "comfort",
            {"minimum_control_temperature": 18.0, "maximum_control_temperature": 30.0},
            (_target(mode="cool"),),
            explicit_transition=True,
        )
    )

    calculation = result.targets[0].result
    assert calculation is not None
    assert calculation.roots is not None
    assert isinstance(calculation.roots.heating_control, RootFailure)
    assert isinstance(calculation.roots.cooling_control, RootSuccess)
    assert calculation.suppression_reason is None
    assert calculation.normalized is not None
    assert calculation.normalized.normalized_ha == pytest.approx(25.5)


@pytest.mark.parametrize("raw", ["unavailable", "NaN", "true", "malformed"])
def test_invalid_primary_never_produces_an_executable_target(raw: str) -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", raw, "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(),),
        )
    )

    assert result.targets[0].result is None
    assert result.targets[0].suppression_reason == "primary_temperature_invalid"
    assert result.hold_condition == "primary_temperature_invalid"
    assert result_values(result)["input_status"] == "primary_temperature_invalid"


def test_modelled_surface_needs_no_helper_and_publishes_surface_risk_diagnostics() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            60.0,
            _state("sensor.outdoor", "0", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {
                "radiant_model": "surface",
                "surface_modelled": True,
                "surface_f_rsi": 0.6,
                "surface_view_factor": 0.25,
                "surface_rh_threshold_pct": 80.0,
            },
            (_target(),),
            explicit_transition=True,
        )
    )
    assert result.surface_temperature_c == pytest.approx(12.0)
    assert result.surface_relative_humidity_pct is not None
    assert result.surface_relative_humidity_pct > 90.0
    assert result.surface_saturation is True
    assert "modelled_surface" in result.quality_reasons
    values = result_values(result)
    assert values["surface_temperature"] == pytest.approx(12.0)


def test_modelled_surface_without_calibration_never_invents_a_factor() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            60.0,
            _state("sensor.outdoor", "0", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"radiant_model": "surface", "surface_modelled": True},
            (_target(),),
            explicit_transition=True,
        )
    )

    assert result.surface_temperature_c is None
    assert "radiant_fallback_invalid_surface_model" in result.quality_reasons
    assert "modelled_surface" not in result.quality_reasons


def test_mold_indicator_attribute_drives_surface_risk_without_changing_comfort_roots() -> None:
    shared = (
        NOW,
        _state("sensor.room", "20", "°C"),
        None,
        60.0,
        _state("sensor.outdoor", "0", "°C"),
    )
    mold = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            *shared,
            _state("sensor.mold_indicator", "12", "°C"),
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {
                "radiant_model": "mold_indicator",
                "surface_rh_threshold_pct": 80.0,
            },
            (_target(),),
            explicit_transition=True,
        )
    )
    standard = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            *shared,
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"radiant_model": "uniform"},
            (_target(),),
            explicit_transition=True,
        )
    )

    assert mold.surface_temperature_c == pytest.approx(12.0)
    assert mold.surface_relative_humidity_pct is not None
    assert mold.surface_relative_humidity_pct > 90.0
    assert mold.surface_saturation is True
    assert "mold_indicator_surface_diagnostic" in mold.quality_reasons
    assert dict(mold.provenance)["radiant"] == "estimated"
    assert result_values(mold)["effective_targets"] == result_values(standard)["effective_targets"]
    assert mold.targets[0].result is not None
    assert standard.targets[0].result is not None
    assert mold.targets[0].result.roots == standard.targets[0].result.roots


def test_snapshot_applies_configured_profile_values_and_environmental_slew() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "eco",
            {
                "eco_heating_setback_c": 3.0,
                "minimum_control_temperature": 10.0,
                "maximum_control_temperature": 30.0,
            },
            (_target(),),
            previous_requested=(19.0, None),
            elapsed_since_previous_seconds=600.0,
        )
    )
    policy = result.targets[0].result.policy
    assert policy is not None
    assert policy.pre_slew_heating_c == pytest.approx(16.3611302727)
    assert policy.heating_c == pytest.approx(18.5)


def test_snapshot_applies_max_setback_from_command_bounds_and_reports_fallback_truthfully() -> None:
    adaptive = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "eco",
            {
                "eco_intensity": "deep",
                "inactive_heating_temperature": 16.0,
                "inactive_cooling_temperature": 29.0,
                "minimum_control_temperature": 17.0,
                "maximum_control_temperature": 30.0,
            },
            (_target(),),
            explicit_transition=True,
        )
    )
    assert result_values(adaptive)["effective_targets"]["target-1"]["temperature"] == 17.0

    fallback = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            None,
            "unavailable",
            "balanced",
            "comfort",
            {"fallback_heating_c": 18.1},
            (_target(),),
            explicit_transition=True,
        )
    )
    values = result_values(fallback)
    assert values["input_status"] == "running_mean_unavailable"
    assert values["effective_targets"]["target-1"]["temperature"] == 18.5
    assert values["effective_target_details"]["target-1"] == {
        "mode": "fallback",
        "reason": "running_mean_unavailable",
        "fallback": True,
        "boost_mode": "off",
        "boost_phase": "fallback",
        "boost_target_heating": None,
        "boost_target_cooling": None,
    }


def test_rapid_boost_with_separate_heating_and_cooling_targets_falls_back_to_adaptive() -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"minimum_control_temperature": 16.0, "maximum_control_temperature": 30.0},
            (
                _named_target("heater", mode="heat"),
                _named_target("cooler", mode="cool"),
            ),
            explicit_transition=True,
            boost_mode="rapid",
        )
    )

    assert "rapid_boost_mixed_adaptive" in result.quality_reasons
    for target in result.targets:
        assert target.result is not None
        assert target.result.policy is not None
        assert target.result.policy.boost_mode.value == "adaptive"
        assert target.result.policy.boost_phase == "adaptive"


def test_runtime_source_state_requires_two_reports_after_invalid_primary() -> None:
    def calculate(
        now: datetime,
        primary: StateValue,
        prior: tuple[tuple[str, SourceState], ...] = (),
    ):
        return calculate_runtime_snapshot(
            CapturedZoneSnapshot(
                now,
                primary,
                None,
                50.0,
                StateValue("sensor.outdoor", "5", "°C", NOW, True, {}, None, None),
                None,
                5.0,
                "complete_history",
                "balanced",
                "comfort",
                {},
                (_target(),),
                explicit_transition=True,
                source_states=prior,
            )
        )

    initial = calculate(NOW, _state("sensor.room", "20", "°C"))
    invalid_time = NOW.replace(second=1)
    invalid = calculate(
        invalid_time,
        StateValue("sensor.room", "unknown", "°C", invalid_time, True, {}, None, None),
        initial.source_states,
    )
    assert invalid.targets[0].result is None
    first_time = NOW.replace(second=10)
    first_recovery = calculate(
        first_time,
        StateValue("sensor.room", "20.1", "°C", first_time, True, {}, None, None),
        invalid.source_states,
    )
    assert first_recovery.targets[0].suppression_reason == "primary_temperature_recovering"
    second_time = NOW.replace(second=41)
    recovered = calculate(
        second_time,
        StateValue("sensor.room", "20.2", "°C", second_time, True, {}, None, None),
        first_recovery.source_states,
    )
    assert recovered.targets[0].result is not None


def test_runtime_accepts_first_valid_primary_after_startup_unavailable() -> None:
    unavailable = StateValue(
        "sensor.room",
        "unavailable",
        "°C",
        NOW,
        False,
        {},
        None,
        None,
    )
    initial = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            unavailable,
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(),),
            explicit_transition=True,
        )
    )
    assert initial.targets[0].suppression_reason == "primary_temperature_invalid"

    reported_at = NOW + timedelta(seconds=10)
    recovered = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            reported_at,
            StateValue("sensor.room", "20.1", "°C", reported_at, True, {}, None, None),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(),),
            explicit_transition=False,
            source_states=initial.source_states,
        )
    )

    assert recovered.targets[0].result is not None
    assert recovered.targets[0].suppression_reason is None
    assert not dict(recovered.source_states)["primary"].recovering


def test_measured_air_speed_is_required_when_selected_and_fixed_clothing_is_applied() -> None:
    base = dict(
        now=NOW,
        primary=_state("sensor.room", "20", "°C"),
        relative_humidity=None,
        declared_relative_humidity_pct=50.0,
        outdoor=_state("sensor.outdoor", "5", "°C"),
        optional_radiant=None,
        running_mean_c=5.0,
        history_quality="complete_history",
        strategy="balanced",
        profile="comfort",
        options={
            "air_speed_mode": "measured",
            "clothing_mode": "fixed",
            "fixed_clothing_clo": 0.8,
        },
        targets=(_target(),),
        explicit_transition=True,
    )
    missing = calculate_runtime_snapshot(CapturedZoneSnapshot(**base))
    assert missing.targets[0].suppression_reason == "air_speed_invalid"
    assert missing.hold_condition == "air_speed_invalid"
    fallback = calculate_runtime_snapshot(CapturedZoneSnapshot(**base, failure_hold_elapsed=True))
    assert fallback.targets[0].result is not None
    assert fallback.targets[0].result.policy is not None
    assert fallback.targets[0].result.policy.fallback
    assert fallback.targets[0].result.normalized is not None
    assert fallback.hold_condition == "air_speed_invalid"
    measured = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            **base,
            air_speed=StateValue("sensor.speed", "3.6", "km/h", NOW, True, {}, None, None),
        )
    )
    assert measured.targets[0].result is not None
    assert measured.targets[0].result.current is not None
    assert dict(measured.provenance)["air_speed"] == "measured"


@pytest.mark.parametrize(
    ("mode", "radiant", "options", "expected_reason", "expected_surface"),
    [
        ("direct_mrt", _state("sensor.mrt", "19", "°C"), {}, None, None),
        (
            "globe",
            _state("sensor.globe", "21", "°C"),
            {"globe_diameter_m": 0.15, "globe_emissivity": 0.95},
            None,
            None,
        ),
        (
            "globe",
            _state("sensor.globe", "21", "°C"),
            {"globe_diameter_m": 0.0},
            "radiant_fallback_invalid_globe",
            None,
        ),
        (
            "surface",
            _state("sensor.surface", "16", "°C"),
            {"surface_view_factor": 0.25},
            None,
            16.0,
        ),
        (
            "direct_mrt",
            _state("sensor.mrt", "unknown", "°C"),
            {},
            "radiant_fallback_missing_source",
            None,
        ),
        ("direct_mrt", None, {}, "radiant_fallback_missing_source", None),
    ],
)
def test_all_runtime_radiant_paths_are_explicit(
    mode: str,
    radiant: StateValue | None,
    options: dict[str, float],
    expected_reason: str | None,
    expected_surface: float | None,
) -> None:
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            radiant,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"radiant_model": mode, "met": "invalid", **options},
            (_target(),),
            explicit_transition=True,
        )
    )
    assert result.targets[0].result is not None
    assert result.surface_temperature_c == expected_surface
    if expected_reason is None:
        assert not any(reason.startswith("radiant_fallback") for reason in result.quality_reasons)
    else:
        assert expected_reason in result.quality_reasons


def test_globe_mrt_uses_the_validated_measured_ambient_speed() -> None:
    def calculate(*, measured: bool, speed: float):
        return calculate_runtime_snapshot(
            CapturedZoneSnapshot(
                NOW,
                _state("sensor.room", "20", "°C"),
                None,
                50.0,
                _state("sensor.outdoor", "20", "°C"),
                _state("sensor.globe", "24", "°C"),
                20.0,
                "complete_history",
                "balanced",
                "comfort",
                {
                    "radiant_model": "globe",
                    "air_speed_mode": "measured" if measured else "fixed",
                    "air_speed_m_s": speed,
                },
                (_target(),),
                air_speed=_state("sensor.speed", str(speed), "m/s") if measured else None,
                explicit_transition=True,
            )
        )

    measured = calculate(measured=True, speed=0.8)
    declared_same = calculate(measured=False, speed=0.8)
    declared_default = calculate(measured=False, speed=0.1)
    measured_result = measured.targets[0].result
    declared_same_result = declared_same.targets[0].result
    declared_default_result = declared_default.targets[0].result
    assert measured_result is not None
    assert declared_same_result is not None
    assert declared_default_result is not None
    assert measured_result.current == declared_same_result.current
    assert measured_result.current != declared_default_result.current


def test_result_projection_includes_ranged_target_and_cold_warm_statuses() -> None:
    ranged = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(mode="heat_cool", features=2),),
            explicit_transition=True,
        )
    )
    values = result_values(ranged)
    assert values["effective_targets"] == {"target-1": {"target_low": 19.5, "target_high": 23.0}}
    scenarios = values["target_scenarios"]["target-1"]
    assert scenarios["current"]["actuator"] == {"target_low": 19.5, "target_high": 23.0}
    assert set(scenarios["occupied"]["room"]) == {"target_low", "target_high"}
    assert set(scenarios["unoccupied"]["room"]) == {"target_low", "target_high"}
    cold = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "12", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(),),
        )
    )
    warm = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "30", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (_target(mode="cool"),),
        )
    )
    assert result_values(cold)["comfort_status"] == "cold"
    assert result_values(warm)["comfort_status"] == "warm"


def test_fahrenheit_effective_target_is_projected_in_native_celsius() -> None:
    fahrenheit_target = CapturedTarget(
        "target-1",
        "registry-1",
        "climate.living_room",
        ClimateCapabilitySnapshot(
            "heat",
            ("off", "heat"),
            1,
            60.0,
            86.0,
            1.0,
            TemperatureUnit.FAHRENHEIT,
            scalar_target_ha=64.0,
        ),
    )
    result = calculate_runtime_snapshot(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "20", "°C"),
            None,
            50.0,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {},
            (fahrenheit_target,),
            explicit_transition=True,
        )
    )

    assert result_values(result)["effective_targets"]["target-1"]["temperature"] == pytest.approx(
        (67.0 - 32.0) * 5.0 / 9.0
    )

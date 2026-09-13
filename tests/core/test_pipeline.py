"""Full pure calculation-path tests without replacing numerical or policy layers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from custom_components.athb.core import (
    ActuationDirection,
    ApplicabilityReason,
    AthbSuccess,
    BoostMode,
    ClimateCapabilitySnapshot,
    ComfortStrategy,
    ControlProfile,
    EcoIntensity,
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    RootSuccess,
    TemperatureUnit,
    ZoneCalculationInput,
    calculate_zone,
)
from custom_components.athb.core import pipeline as pipeline_module
from custom_components.athb.core.policy import PolicyTargets


def _input(
    *,
    strategy: ComfortStrategy = ComfortStrategy.BALANCED,
    direction: ActuationDirection = ActuationDirection.HEATING_ONLY,
    running_mean_c: float | None = 5.0,
    hvac_mode: str = "heat",
    features: int = 1,
) -> ZoneCalculationInput:
    return ZoneCalculationInput(
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        relative_humidity_declared=False,
        running_mean_c=running_mean_c,
        strategy=strategy,
        direction=direction,
        profile=ControlProfile.COMFORT,
        climate=ClimateCapabilitySnapshot(
            hvac_mode,
            ("off", "heat", "cool", "heat_cool"),
            features,
            16.0,
            30.0,
            0.5,
            TemperatureUnit.CELSIUS,
            scalar_target_ha=18.0,
        ),
        grid=GridOptions(18.0, 26.0),
        explicit_transition=True,
    )


def test_adaptive_heating_runs_real_numerical_policy_and_grid_path() -> None:
    result = calculate_zone(_input())

    assert isinstance(result.current, AthbSuccess)
    assert result.current.sensation_vote == pytest.approx(-0.1730884570, abs=1e-9)
    assert result.roots is not None
    assert isinstance(result.roots.heating_control, RootSuccess)
    assert result.roots.heating_control.mapped_room_temperature_c == pytest.approx(
        19.362709, abs=0.005
    )
    assert result.policy is not None
    assert not result.policy.fallback
    assert isinstance(result.normalized, NormalizedScalarTarget)
    assert result.normalized.normalized_ha == 19.5
    assert result.suppression_reason is None
    assert result.eligibility is not None
    assert result.eligibility.eligible
    assert 0 < result.evaluation_count <= 3_000


def test_ranged_path_uses_control_roots_and_atomic_normalization() -> None:
    result = calculate_zone(
        _input(
            direction=ActuationDirection.RANGED,
            hvac_mode="heat_cool",
            features=2,
        )
    )

    assert result.policy is not None
    assert isinstance(result.normalized, NormalizedRangeTarget)
    assert result.normalized.heating.normalized_ha == 19.5
    assert result.normalized.cooling.normalized_ha == 23.0
    assert isinstance(result.occupied_normalized, NormalizedRangeTarget)
    assert isinstance(result.unoccupied_normalized, NormalizedRangeTarget)
    assert (
        result.normalized.cooling.normalized_room_c - result.normalized.heating.normalized_room_c
        >= 1
    )


def test_cooling_policy_exposes_normalized_occupancy_references() -> None:
    result = calculate_zone(
        _input(
            direction=ActuationDirection.COOLING_ONLY,
            hvac_mode="cool",
            features=1,
        )
    )

    assert isinstance(result.occupied_normalized, NormalizedScalarTarget)
    assert isinstance(result.unoccupied_normalized, NormalizedScalarTarget)
    assert result.occupied_normalized.normalized_actuator_c == 23.0
    assert result.unoccupied_normalized.normalized_actuator_c == 25.0


def test_missing_history_is_explicit_fixed_fallback_not_adaptive_output() -> None:
    result = calculate_zone(_input(running_mean_c=None))

    assert result.current is None
    assert result.roots is None
    assert result.policy is not None
    assert result.policy.fallback
    assert result.policy.limitations == ("fixed_fallback",)
    assert isinstance(result.normalized, NormalizedScalarTarget)
    assert result.normalized.normalized_ha == 18.0
    assert result.hold_condition == "running_mean_unavailable"


@pytest.mark.parametrize(
    ("intensity", "expected_heating", "expected_cooling"),
    [
        (EcoIntensity.MILD, 17.3611302727, 25.4795271760),
        (EcoIntensity.WORKDAY, 15.3611302727, 27.4795271760),
        (EcoIntensity.DEEP, 16.0, 29.0),
        (EcoIntensity.CUSTOM, 18.3611302727, 24.4795271760),
    ],
)
def test_eco_intensity_uses_post_solve_policy_for_both_directions(
    intensity: EcoIntensity, expected_heating: float, expected_cooling: float
) -> None:
    result = calculate_zone(
        replace(
            _input(direction=ActuationDirection.RANGED, hvac_mode="heat_cool", features=2),
            profile=ControlProfile.ECO,
            eco_intensity=intensity,
            eco_heating_setback_c=1.0,
            eco_cooling_setback_c=1.0,
            inactive_heating_c=16.0,
            inactive_cooling_c=29.0,
            grid=GridOptions(10.0, 30.0),
        )
    )

    assert result.policy is not None
    assert result.policy.heating_c == pytest.approx(expected_heating, abs=0.01)
    assert result.policy.cooling_c == pytest.approx(expected_cooling, abs=0.01)


def test_strategy_order_is_in_sensation_roots_not_temperature_midpoints() -> None:
    roots = {}
    for strategy in ComfortStrategy:
        result = calculate_zone(_input(strategy=strategy))
        assert result.roots is not None
        assert isinstance(result.roots.heating_control, RootSuccess)
        assert isinstance(result.roots.cooling_control, RootSuccess)
        roots[strategy] = (
            result.roots.heating_control.mapped_room_temperature_c,
            result.roots.cooling_control.mapped_room_temperature_c,
        )

    assert roots[ComfortStrategy.EFFICIENT][0] < roots[ComfortStrategy.BALANCED][0]
    assert roots[ComfortStrategy.ECO][0] < roots[ComfortStrategy.EFFICIENT][0]
    assert roots[ComfortStrategy.BALANCED][0] < roots[ComfortStrategy.COMFORT][0]
    assert roots[ComfortStrategy.COMFORT][1] < roots[ComfortStrategy.BALANCED][1]
    assert roots[ComfortStrategy.BALANCED][1] < roots[ComfortStrategy.EFFICIENT][1]
    assert roots[ComfortStrategy.COMFORT][0] < roots[ComfortStrategy.NEAR_NEUTRAL][0]
    assert roots[ComfortStrategy.NEAR_NEUTRAL][1] < roots[ComfortStrategy.COMFORT][1]


def test_rapid_boost_uses_command_limit_then_holds_adaptive_boost_target() -> None:
    active = calculate_zone(
        replace(
            _input(),
            boost_mode=BoostMode.RAPID,
            boost_delta_c=1.0,
            inactive_cooling_c=26.0,
        )
    )
    assert active.policy is not None
    assert active.policy.boost_phase == "rapid"
    assert active.policy.boost_target_heating_c == pytest.approx(20.3627, abs=0.005)
    assert isinstance(active.normalized, NormalizedScalarTarget)
    assert active.normalized.normalized_room_c == 26.0

    reached = calculate_zone(
        replace(
            _input(),
            air_temperature_c=21.0,
            boost_mode=BoostMode.RAPID,
            boost_delta_c=1.0,
            inactive_cooling_c=26.0,
        )
    )
    assert reached.policy is not None
    assert reached.policy.boost_phase == "hold"
    assert reached.policy.rapid_boost_reached
    assert isinstance(reached.normalized, NormalizedScalarTarget)
    assert reached.normalized.normalized_room_c < 26.0


def test_boost_bypasses_occupancy_setback_without_mutating_raw_roots() -> None:
    occupied = calculate_zone(_input())
    unoccupied = calculate_zone(
        replace(_input(), profile=ControlProfile.ECO, eco_intensity=EcoIntensity.MILD)
    )
    boosted = calculate_zone(
        replace(
            _input(),
            profile=ControlProfile.ECO,
            eco_intensity=EcoIntensity.MILD,
            boost_mode=BoostMode.ADAPTIVE,
        )
    )
    assert occupied.roots == unoccupied.roots == boosted.roots
    assert occupied.policy is not None
    assert unoccupied.policy is not None
    assert boosted.policy is not None
    assert unoccupied.policy.heating_c == pytest.approx(occupied.policy.heating_c - 2.0)
    assert boosted.policy.heating_c > occupied.policy.heating_c


def test_policy_exposes_stable_occupied_and_unoccupied_reference_targets() -> None:
    result = calculate_zone(
        replace(
            _input(),
            profile=ControlProfile.ECO,
            eco_intensity=EcoIntensity.MILD,
            boost_mode=BoostMode.ADAPTIVE,
            grid=GridOptions(16.0, 26.0),
        )
    )

    assert result.policy is not None
    assert result.policy.heating_c > result.policy.occupied_heating_c
    assert result.policy.occupied_heating_c == pytest.approx(19.362709, abs=0.005)
    assert result.policy.unoccupied_heating_c == pytest.approx(17.362709, abs=0.005)
    assert isinstance(result.occupied_normalized, NormalizedScalarTarget)
    assert result.occupied_normalized.normalized_actuator_c == 19.5
    assert isinstance(result.unoccupied_normalized, NormalizedScalarTarget)
    assert result.unoccupied_normalized.normalized_actuator_c == 17.5


def test_invalid_moisture_speed_and_strategy_fail_before_actuation() -> None:
    moisture = calculate_zone(replace(_input(), relative_humidity_pct=101.0))
    assert moisture.normalized is None
    assert moisture.suppression_reason == "invalid_relative_humidity"
    speed = calculate_zone(replace(_input(), air_speed_m_s=-1.0))
    assert speed.normalized is None
    assert speed.suppression_reason == "outside_engineering_domain"
    missing_speed = calculate_zone(replace(_input(), air_speed_m_s=None))
    assert missing_speed.normalized is None
    assert missing_speed.suppression_reason == "air_speed_invalid"
    strategy = calculate_zone(replace(_input(), strategy=cast(Any, "not-a-strategy")))
    assert strategy.normalized is None
    assert strategy.suppression_reason == "invalid_strategy"


def test_directional_failure_rejects_range_when_humid_heating_root_is_missing() -> None:
    ranged = replace(
        _input(
            direction=ActuationDirection.RANGED,
            hvac_mode="heat_cool",
            features=2,
        ),
        air_temperature_c=28.0,
        relative_humidity_pct=80.0,
        running_mean_c=25.0,
    )
    result = calculate_zone(ranged)
    assert result.normalized is None
    assert result.eligibility is not None
    assert not result.eligibility.eligible
    assert result.suppression_reason == "missing_required_root:heating_control"


def test_rejected_extrapolation_holds_then_uses_configured_fallback_or_no_write() -> None:
    winter = replace(_input(), running_mean_c=-10.0, reject_extrapolation=True)
    hold = calculate_zone(winter)
    assert hold.policy is None
    assert hold.normalized is None
    assert hold.suppression_reason == "extrapolation_rejected"
    assert hold.hold_condition == "extrapolation_rejected"
    fallback = calculate_zone(replace(winter, failure_hold_elapsed=True))
    assert fallback.policy is not None
    assert fallback.policy.fallback
    assert isinstance(fallback.normalized, NormalizedScalarTarget)
    assert fallback.hold_condition == "extrapolation_rejected"
    no_write = calculate_zone(replace(winter, failure_hold_elapsed=True, fallback_no_write=True))
    assert no_write.normalized is None
    assert no_write.suppression_reason == "fallback_no_write"


def test_reject_extrapolation_includes_the_required_solved_candidate() -> None:
    candidate_only = replace(
        _input(running_mean_c=0.0),
        air_temperature_c=13.0,
        relative_humidity_pct=20.0,
    )
    allowed = calculate_zone(candidate_only)
    assert isinstance(allowed.current, AthbSuccess)
    assert ApplicabilityReason.EXTRAPOLATED not in allowed.current.applicability_reasons
    assert allowed.roots is not None
    assert isinstance(allowed.roots.heating_control, RootSuccess)
    assert ApplicabilityReason.EXTRAPOLATED in allowed.roots.heating_control.applicability_reasons
    assert allowed.normalized is not None

    rejected = calculate_zone(replace(candidate_only, reject_extrapolation=True))
    assert rejected.normalized is None
    assert rejected.suppression_reason == "extrapolation_rejected"
    assert rejected.hold_condition == "extrapolation_rejected"


def test_required_directional_root_failure_holds_then_falls_back() -> None:
    ranged = replace(
        _input(
            direction=ActuationDirection.RANGED,
            hvac_mode="heat_cool",
            features=2,
        ),
        air_temperature_c=28.0,
        relative_humidity_pct=80.0,
        running_mean_c=25.0,
    )
    hold = calculate_zone(ranged)
    assert hold.suppression_reason == "missing_required_root:heating_control"
    assert hold.normalized is None
    fallback = calculate_zone(replace(ranged, failure_hold_elapsed=True))
    assert fallback.policy is not None
    assert fallback.policy.fallback
    assert isinstance(fallback.normalized, NormalizedRangeTarget)


def test_fallback_cooling_is_blocked_below_dewpoint() -> None:
    result = calculate_zone(
        replace(
            _input(
                direction=ActuationDirection.COOLING_ONLY,
                running_mean_c=None,
                hvac_mode="cool",
            ),
            air_temperature_c=28.0,
            relative_humidity_pct=90.0,
            fallback_cooling_c=20.0,
        )
    )
    assert result.normalized is None
    assert result.suppression_reason == "fallback_below_dewpoint"


def _incomplete_policy(heating: float | None, cooling: float | None) -> PolicyTargets:
    return PolicyTargets(
        heating,
        cooling,
        heating,
        cooling,
        heating,
        cooling,
        ControlProfile.COMFORT,
        False,
        "primary",
        "primary",
        None,
        None,
        (),
    )


@pytest.mark.parametrize(
    ("direction", "policy", "reason"),
    [
        (
            ActuationDirection.HEATING_ONLY,
            _incomplete_policy(None, 24.0),
            "missing_heating_target",
        ),
        (
            ActuationDirection.COOLING_ONLY,
            _incomplete_policy(19.0, None),
            "missing_cooling_target",
        ),
        (
            ActuationDirection.RANGED,
            _incomplete_policy(19.0, None),
            "missing_range_target",
        ),
    ],
)
def test_policy_target_shape_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    direction: ActuationDirection,
    policy: PolicyTargets,
    reason: str,
) -> None:
    monkeypatch.setattr(pipeline_module, "build_adaptive_policy", lambda **_kwargs: policy)
    result = calculate_zone(
        _input(
            direction=direction,
            hvac_mode="heat_cool" if direction is ActuationDirection.RANGED else direction.value,
            features=2 if direction is ActuationDirection.RANGED else 1,
        )
    )
    assert result.normalized is None
    assert result.suppression_reason == reason


def test_normalization_failure_is_preserved_as_exact_suppression() -> None:
    result = calculate_zone(
        replace(
            _input(),
            grid=GridOptions(25.0, 20.0),
        )
    )
    assert result.normalized is None
    assert result.suppression_reason == "invalid_grid_configuration"

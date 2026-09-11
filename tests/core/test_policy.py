"""Hand-verifiable strategy, critical, profile, fallback, and bounds policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.core.contracts import (
    ActuationDirection,
    ControlProfile,
    CriticalEligibilityMode,
    EcoIntensity,
    RootFailure,
    RootFailureCode,
    RootName,
    RootSet,
    RootSuccess,
)
from custom_components.athb.core.policy import (
    ActuatorBoundResult,
    CriticalDemand,
    OccupancyState,
    OpposingTarget,
    PolicyFailure,
    PolicyTargets,
    apply_calibration_and_user_bounds,
    apply_critical_demands,
    boost_expiry,
    build_adaptive_policy,
    build_fixed_fallback,
    check_cross_actuator_coordination,
    cooling_dewpoint_eligible,
    resolve_profile,
)

NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


def _root(name: RootName, vote: float, temperature: float) -> RootSuccess:
    return RootSuccess(name, vote, temperature, temperature, 0.0, 0.001, 50)


def _roots(
    *,
    lower: float = 17.0,
    heating: float = 19.0,
    neutral: float = 21.0,
    cooling: float = 23.0,
    upper: float = 25.0,
) -> RootSet:
    return RootSet(
        _root(RootName.LOWER_COMFORT, -0.5, lower),
        _root(RootName.HEATING_CONTROL, -0.25, heating),
        _root(RootName.THERMAL_NEUTRAL, 0.0, neutral),
        _root(RootName.COOLING_CONTROL, 0.25, cooling),
        _root(RootName.UPPER_COMFORT, 0.5, upper),
    )


def _demand(
    location: str,
    *,
    heating: float,
    cooling: float,
    mode: CriticalEligibilityMode = CriticalEligibilityMode.BOTH,
    eligible: bool = True,
) -> CriticalDemand:
    return CriticalDemand(
        location,
        mode,
        _root(RootName.HEATING_CONTROL, -0.25, heating),
        _root(RootName.COOLING_CONTROL, 0.25, cooling),
        eligible,
    )


def test_primary_control_band_is_distinct_from_comfort_band() -> None:
    result = apply_critical_demands(roots=_roots(), demands=(), direction=ActuationDirection.RANGED)
    assert result.heating_c == 19.0
    assert result.cooling_c == 23.0
    assert result.heating_c != 17.0
    assert result.cooling_c != 25.0


def test_worst_heating_and_cooling_demands_are_selected_once_then_capped() -> None:
    result = apply_critical_demands(
        roots=_roots(),
        demands=(
            _demand("zeta", heating=20.5, cooling=21.5),
            _demand("alpha", heating=22.0, cooling=20.0),
            _demand("ignored", heating=30.0, cooling=10.0, eligible=False),
        ),
        direction=ActuationDirection.HEATING_ONLY,
    )
    assert result.heating_c == 21.0
    assert result.heating_contribution is not None
    assert result.heating_contribution.governing_location == "alpha"
    assert result.heating_contribution.requested_c == 3.0
    assert result.heating_contribution.applied_c == 2.0
    assert result.limitations == ("critical_demand_limited",)

    cooling = apply_critical_demands(
        roots=_roots(),
        demands=(_demand("cold", heating=19.0, cooling=20.0),),
        direction=ActuationDirection.COOLING_ONLY,
    )
    assert cooling.cooling_c == 21.0
    assert cooling.cooling_contribution is not None
    assert cooling.cooling_contribution.requested_c == -3.0
    assert cooling.cooling_contribution.applied_c == -2.0


def test_equal_critical_demands_use_stable_location_id() -> None:
    result = apply_critical_demands(
        roots=_roots(),
        demands=(
            _demand("b", heating=20.0, cooling=22.0),
            _demand("a", heating=20.0, cooling=22.0),
        ),
        direction=ActuationDirection.RANGED,
    )
    assert result.heating_contribution is not None
    assert result.cooling_contribution is not None
    assert result.heating_contribution.governing_location == "a"
    assert result.cooling_contribution.governing_location == "a"


def test_directional_modes_never_contribute_opposite_demand() -> None:
    heating_only = _demand("heat", heating=20.0, cooling=20.0, mode=CriticalEligibilityMode.HEATING)
    cooling_only = _demand("cool", heating=22.0, cooling=22.0, mode=CriticalEligibilityMode.COOLING)
    result = apply_critical_demands(
        roots=_roots(),
        demands=(heating_only, cooling_only),
        direction=ActuationDirection.RANGED,
    )
    assert result.heating_contribution is not None
    assert result.heating_contribution.governing_location == "heat"
    assert result.cooling_contribution is not None
    assert result.cooling_contribution.governing_location == "cool"


def test_critical_range_conflict_discards_both_contributions() -> None:
    result = apply_critical_demands(
        roots=_roots(),
        demands=(
            _demand("heat", heating=21.0, cooling=23.0),
            _demand("cool", heating=19.0, cooling=21.0),
        ),
        direction=ActuationDirection.RANGED,
        minimum_range_gap_c=1.0,
    )
    assert result.heating_c == 19.0
    assert result.cooling_c == 23.0
    assert result.heating_contribution is None
    assert result.cooling_contribution is None
    assert "critical_locations_conflict" in result.limitations


def test_missing_outer_roots_exclude_critical_influence_not_directional_baseline() -> None:
    failure = RootFailure(RootName.UPPER_COMFORT, 0.5, RootFailureCode.NO_BRACKET, 33, "diagnostic")
    base = _roots()
    roots = RootSet(
        base.lower_comfort,
        base.heating_control,
        base.thermal_neutral,
        base.cooling_control,
        failure,
    )
    result = apply_critical_demands(
        roots=roots,
        demands=(_demand("cold", heating=20.0, cooling=22.0),),
        direction=ActuationDirection.HEATING_ONLY,
    )
    assert result.heating_c == 19.0
    assert result.limitations == ("critical_constraints_unavailable",)


def test_inconsistent_edge_guard_retains_primary_baseline() -> None:
    result = apply_critical_demands(
        roots=_roots(lower=24.0, heating=24.9, cooling=26.0, upper=25.0),
        demands=(_demand("cold", heating=25.5, cooling=26.0),),
        direction=ActuationDirection.HEATING_ONLY,
    )
    assert result.heating_c == 24.9
    assert "critical_constraints_inconsistent" in result.limitations


def test_missing_required_directional_root_and_narrow_primary_range_fail() -> None:
    base = _roots()
    missing = RootFailure(
        RootName.HEATING_CONTROL, -0.25, RootFailureCode.MOISTURE_LIMITED_NO_SOLUTION, 33, "wet"
    )
    roots = RootSet(
        base.lower_comfort,
        missing,
        base.thermal_neutral,
        base.cooling_control,
        base.upper_comfort,
    )
    assert (
        apply_critical_demands(
            roots=roots, demands=(), direction=ActuationDirection.COOLING_ONLY
        ).cooling_c
        == 23.0
    )
    failed = apply_critical_demands(
        roots=roots, demands=(), direction=ActuationDirection.HEATING_ONLY
    )
    assert failed == PolicyFailure("missing_required_root:heating_control")
    narrow = apply_critical_demands(
        roots=_roots(heating=22.5, cooling=23.0),
        demands=(),
        direction=ActuationDirection.RANGED,
    )
    assert narrow == PolicyFailure("control_band_too_narrow")

    missing_cooling = RootSet(
        base.lower_comfort,
        base.heating_control,
        base.thermal_neutral,
        RootFailure(RootName.COOLING_CONTROL, 0.25, RootFailureCode.NO_BRACKET, 33, "hot"),
        base.upper_comfort,
    )
    assert apply_critical_demands(
        roots=missing_cooling,
        demands=(),
        direction=ActuationDirection.COOLING_ONLY,
    ) == PolicyFailure("missing_required_root:cooling_control")


def test_eco_and_boost_transform_after_critical_policy_without_changing_roots() -> None:
    roots = _roots()
    eco = build_adaptive_policy(
        roots=roots,
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.ECO,
        explicit_transition=True,
    )
    assert isinstance(eco, PolicyTargets)
    assert (eco.heating_c, eco.cooling_c) == (17.0, 25.0)
    assert (eco.pre_profile_heating_c, eco.pre_profile_cooling_c) == (19.0, 23.0)
    assert roots.heating_control.mapped_room_temperature_c == 19.0
    workday = build_adaptive_policy(
        roots=roots,
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.ECO,
        eco_intensity=EcoIntensity.WORKDAY,
        explicit_transition=True,
    )
    assert isinstance(workday, PolicyTargets)
    assert (workday.heating_c, workday.cooling_c) == (15.0, 27.0)
    deep = build_adaptive_policy(
        roots=roots,
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.ECO,
        eco_intensity=EcoIntensity.DEEP,
        inactive_heating_c=16.0,
        inactive_cooling_c=28.0,
        explicit_transition=True,
    )
    assert isinstance(deep, PolicyTargets)
    assert (deep.heating_c, deep.cooling_c) == (16.0, 28.0)
    assert "eco_deep" in deep.limitations
    guarded_deep = build_adaptive_policy(
        roots=roots,
        critical_demands=(
            CriticalDemand(
                "cold_seat",
                CriticalEligibilityMode.HEATING,
                _root(RootName.HEATING_CONTROL, -0.25, 20.5),
                _root(RootName.COOLING_CONTROL, 0.25, 23.0),
                True,
            ),
        ),
        direction=ActuationDirection.HEATING_ONLY,
        profile=ControlProfile.ECO,
        eco_intensity=EcoIntensity.DEEP,
        inactive_heating_c=16.0,
        inactive_cooling_c=28.0,
        explicit_transition=True,
    )
    assert isinstance(guarded_deep, PolicyTargets)
    assert guarded_deep.heating_c == 17.5
    assert guarded_deep.heating_contribution is not None
    assert guarded_deep.heating_contribution.applied_c == 1.5
    boost = build_adaptive_policy(
        roots=roots,
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.BOOST,
        boost_delta_c=2.0,
        explicit_transition=True,
    )
    assert isinstance(boost, PolicyTargets)
    assert (boost.heating_c, boost.cooling_c) == (20.5, 21.5)
    assert "boost_limited" in boost.limitations
    scalar_boost = build_adaptive_policy(
        roots=roots,
        critical_demands=(),
        direction=ActuationDirection.HEATING_ONLY,
        profile=ControlProfile.BOOST,
        explicit_transition=True,
    )
    assert isinstance(scalar_boost, PolicyTargets)
    assert scalar_boost.heating_c == 20.0
    assert scalar_boost.cooling_c is None


def test_environmental_slew_is_half_degree_per_ten_minutes_and_explicit_transition_bypasses() -> (
    None
):
    ordinary = build_adaptive_policy(
        roots=_roots(heating=21.0, cooling=25.0),
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.COMFORT,
        previous_requested=(19.0, 23.0),
        elapsed_since_previous_seconds=600.0,
    )
    assert isinstance(ordinary, PolicyTargets)
    assert (ordinary.heating_c, ordinary.cooling_c) == (19.5, 23.5)
    bypass = build_adaptive_policy(
        roots=_roots(heating=21.0, cooling=25.0),
        critical_demands=(),
        direction=ActuationDirection.RANGED,
        profile=ControlProfile.COMFORT,
        previous_requested=(19.0, 23.0),
        elapsed_since_previous_seconds=0.0,
        explicit_transition=True,
    )
    assert isinstance(bypass, PolicyTargets)
    assert (bypass.heating_c, bypass.cooling_c) == (21.0, 25.0)


def test_auto_profile_resolution_holds_unknown_then_falls_back_to_comfort() -> None:
    assert (
        resolve_profile(
            selected=ControlProfile.AUTO, occupancy=OccupancyState.ABSENT, now=NOW
        ).resolved
        is ControlProfile.COMFORT
    )
    on = resolve_profile(selected=ControlProfile.AUTO, occupancy=OccupancyState.ON, now=NOW)
    assert on.resolved is ControlProfile.COMFORT
    off = resolve_profile(selected=ControlProfile.AUTO, occupancy=OccupancyState.OFF, now=NOW)
    assert off.resolved is ControlProfile.ECO
    held = resolve_profile(
        selected=ControlProfile.AUTO,
        occupancy=OccupancyState.UNKNOWN,
        now=NOW + timedelta(minutes=30),
        previous=off,
    )
    assert held.resolved is ControlProfile.ECO
    expired = resolve_profile(
        selected=ControlProfile.AUTO,
        occupancy=OccupancyState.UNKNOWN,
        now=NOW + timedelta(minutes=31),
        previous=off,
    )
    assert expired.resolved is ControlProfile.COMFORT
    assert expired.reasons == ("occupancy_unknown",)
    explicit = resolve_profile(
        selected=ControlProfile.BOOST, occupancy=OccupancyState.UNKNOWN, now=NOW
    )
    assert explicit.resolved is ControlProfile.BOOST


def test_fixed_fallback_is_separate_and_requires_primary_inputs() -> None:
    result = build_fixed_fallback(
        direction=ActuationDirection.RANGED,
        primary_air_valid=True,
        primary_rh_valid=True,
    )
    assert isinstance(result, PolicyTargets)
    assert result.fallback
    assert (result.heating_c, result.cooling_c) == (18.0, 26.0)
    assert build_fixed_fallback(
        direction=ActuationDirection.HEATING_ONLY,
        primary_air_valid=False,
        primary_rh_valid=True,
    ) == PolicyFailure("primary_air_invalid")
    assert build_fixed_fallback(
        direction=ActuationDirection.HEATING_ONLY,
        primary_air_valid=True,
        primary_rh_valid=False,
    ) == PolicyFailure("primary_rh_invalid")
    assert build_fixed_fallback(
        direction=ActuationDirection.HEATING_ONLY,
        primary_air_valid=True,
        primary_rh_valid=True,
        fallback_policy_no_write=True,
    ) == PolicyFailure("fallback_no_write")
    assert build_fixed_fallback(
        direction=ActuationDirection.RANGED,
        primary_air_valid=True,
        primary_rh_valid=True,
        heating_c=20.0,
        cooling_c=20.5,
    ) == PolicyFailure("control_band_too_narrow")
    assert (
        build_fixed_fallback(
            direction=ActuationDirection.COOLING_ONLY,
            primary_air_valid=True,
            primary_rh_valid=True,
        ).heating_c
        is None
    )


def test_calibration_precedes_user_bounds_and_is_not_a_sensation_adjustment() -> None:
    result = apply_calibration_and_user_bounds(
        requested_room_c=25.0,
        calibration_offset_c=2.0,
        user_min_c=18.0,
        user_max_c=26.0,
    )
    assert result == ActuatorBoundResult(25.0, 27.0, 26.0, ("user_bound_applied",))
    unchanged = apply_calibration_and_user_bounds(
        requested_room_c=20.0,
        calibration_offset_c=-1.0,
        user_min_c=18.0,
        user_max_c=26.0,
    )
    assert unchanged == ActuatorBoundResult(20.0, 19.0, 19.0, ())
    assert isinstance(
        apply_calibration_and_user_bounds(
            requested_room_c=20.0,
            calibration_offset_c=3.1,
            user_min_c=18.0,
            user_max_c=26.0,
        ),
        PolicyFailure,
    )
    assert isinstance(
        apply_calibration_and_user_bounds(
            requested_room_c=float("nan"),
            calibration_offset_c=0.0,
            user_min_c=18.0,
            user_max_c=26.0,
        ),
        PolicyFailure,
    )


def test_invalid_adaptive_policy_configuration_and_root_failure_propagate() -> None:
    invalid = build_adaptive_policy(
        roots=_roots(),
        critical_demands=(),
        direction=ActuationDirection.HEATING_ONLY,
        profile=ControlProfile.COMFORT,
        minimum_range_gap_c=0.5,
    )
    assert invalid == PolicyFailure("invalid_policy_configuration")
    base = _roots()
    missing = RootSet(
        base.lower_comfort,
        RootFailure(RootName.HEATING_CONTROL, -0.25, RootFailureCode.NO_BRACKET, 33, "bad"),
        base.thermal_neutral,
        base.cooling_control,
        base.upper_comfort,
    )
    propagated = build_adaptive_policy(
        roots=missing,
        critical_demands=(),
        direction=ActuationDirection.HEATING_ONLY,
        profile=ControlProfile.COMFORT,
    )
    assert propagated == PolicyFailure("missing_required_root:heating_control")


def test_boost_expiry_is_not_extended_implicitly() -> None:
    expiry = boost_expiry(selected_at=NOW)
    assert expiry == NOW + timedelta(minutes=60)
    persisted_after_restart = expiry
    assert persisted_after_restart == expiry
    with pytest.raises(ValueError, match="valid"):
        boost_expiry(selected_at=datetime(2026, 9, 11), duration=timedelta(minutes=60))


def test_cross_actuator_coordination_uses_room_reference_and_manual_observation() -> None:
    opposing = OpposingTarget(
        "cool-b",
        ActuationDirection.COOLING_ONLY,
        owned=False,
        available=True,
        intended_room_c=None,
        observed_room_c=20.0,
    )
    assert (
        check_cross_actuator_coordination(
            direction=ActuationDirection.HEATING_ONLY,
            proposed_room_c=19.5,
            opposing_targets=(opposing,),
        ).reason
        == "cross_actuator_conflict"
    )
    assert check_cross_actuator_coordination(
        direction=ActuationDirection.HEATING_ONLY,
        proposed_room_c=19.0,
        opposing_targets=(opposing,),
    ).eligible


def test_cross_actuator_unknown_opponent_suspends_affected_direction() -> None:
    unknown = OpposingTarget(
        "heat-a",
        ActuationDirection.HEATING_ONLY,
        owned=False,
        available=False,
        intended_room_c=None,
        observed_room_c=None,
    )
    result = check_cross_actuator_coordination(
        direction=ActuationDirection.COOLING_ONLY,
        proposed_room_c=24.0,
        opposing_targets=(unknown,),
    )
    assert result.reason == "opposing_target_unknown"


def test_owned_opponent_uses_normalized_intent_not_stale_observation() -> None:
    opponent = OpposingTarget(
        "heat-a",
        ActuationDirection.HEATING_ONLY,
        owned=True,
        available=True,
        intended_room_c=20.0,
        observed_room_c=17.0,
    )
    assert (
        check_cross_actuator_coordination(
            direction=ActuationDirection.COOLING_ONLY,
            proposed_room_c=20.5,
            opposing_targets=(opponent,),
        ).reason
        == "cross_actuator_conflict"
    )


def test_fallback_dewpoint_check_uses_final_normalized_room_target() -> None:
    assert (
        cooling_dewpoint_eligible(normalized_room_c=18.9, dewpoint_constraint_c=19.0).reason
        == "fallback_below_dewpoint"
    )
    assert cooling_dewpoint_eligible(normalized_room_c=19.0, dewpoint_constraint_c=19.0).eligible

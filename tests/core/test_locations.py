"""Critical local-air physical assembly and eligibility tests."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.core.contracts import (
    AUTOMATIC_CLOTHING,
    ComfortStrategy,
    CriticalEligibilityMode,
    DeclaredRelativeHumidity,
    MeasuredAirTemperature,
    MeasuredMeanRadiantTemperature,
    MeasuredRelativeHumidity,
    RootSuccess,
)
from custom_components.athb.core.inverse import (
    EvaluationBudget,
    StrategyVotes,
    strategy_votes,
)
from custom_components.athb.core.locations import (
    CriticalDeltaState,
    CriticalLocationFailure,
    PreparedCriticalLocation,
    critical_moisture_state,
    prepare_critical_location,
    solve_critical_location,
    update_critical_delta,
    validate_critical_location_count,
)
from custom_components.athb.core.psychrometrics import MoistureState, moisture_state
from custom_components.athb.core.radiant import DirectRadiantModel, UniformRadiantModel

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _primary_moisture() -> MoistureState:
    result = moisture_state(20.0, MeasuredRelativeHumidity(50.0))
    assert isinstance(result, MoistureState)
    return result


def _eligible_delta(delta_c: float = 2.0):
    state = CriticalDeltaState()
    update = update_critical_delta(
        state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=20.0 - delta_c,
        observed_at=NOW,
    )
    update = update_critical_delta(
        update.state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=20.0 - delta_c,
        observed_at=NOW + timedelta(minutes=5),
    )
    return update_critical_delta(
        update.state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=20.0 - delta_c,
        observed_at=NOW + timedelta(minutes=10),
    )


def test_delta_filter_initializes_then_requires_ten_minutes_and_three_reports() -> None:
    first = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=20.0,
        local_air_temperature_c=18.0,
        observed_at=NOW,
    )
    assert first.state.filtered_delta_c == 2.0
    assert first.state.report_count == 1
    assert not first.eligible
    second = update_critical_delta(
        first.state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=16.0,
        observed_at=NOW + timedelta(minutes=5),
    )
    expected_second = 2.0 + (1.0 - math.exp(-0.5)) * 2.0
    assert second.state.filtered_delta_c == pytest.approx(expected_second)
    third = update_critical_delta(
        second.state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=16.0,
        observed_at=NOW + timedelta(minutes=10),
    )
    assert third.eligible
    assert third.state.report_count == 3
    assert third.effective_delta_c == 3.0
    assert "critical_delta_bounded" in third.reasons


def test_outlier_invalid_stale_and_duplicate_reports_do_not_gain_eligibility() -> None:
    outlier = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=20.0,
        local_air_temperature_c=13.9,
        observed_at=NOW,
    )
    assert not outlier.eligible
    assert outlier.state.raw_delta_c == pytest.approx(6.1)
    assert outlier.reasons == ("critical_delta_outlier",)
    invalid = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=20.0,
        local_air_temperature_c=18.0,
        observed_at=NOW,
        valid=False,
    )
    stale = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=20.0,
        local_air_temperature_c=18.0,
        observed_at=NOW,
        fresh=False,
    )
    assert invalid.state == CriticalDeltaState()
    assert stale.state == CriticalDeltaState()
    first = _eligible_delta()
    duplicate = update_critical_delta(
        first.state,
        primary_air_temperature_c=20.0,
        local_air_temperature_c=18.0,
        observed_at=first.state.last_report_at,
    )
    assert not duplicate.eligible
    assert duplicate.state == first.state
    assert duplicate.reasons == ("critical_report_not_newer",)


@pytest.mark.parametrize(
    ("primary", "local", "observed"),
    [
        (float("nan"), 18.0, NOW),
        (20.0, float("inf"), NOW),
        (20.0, 18.0, datetime(2026, 1, 1)),
    ],
)
def test_invalid_delta_inputs_reset_window(
    primary: float, local: float, observed: datetime
) -> None:
    result = update_critical_delta(
        _eligible_delta().state,
        primary_air_temperature_c=primary,
        local_air_temperature_c=local,
        observed_at=observed,
    )
    assert result.state == CriticalDeltaState()
    assert result.reasons == ("critical_observation_invalid",)


def test_shared_vapor_pressure_recalculates_local_rh_instead_of_reusing_percentage() -> None:
    primary = _primary_moisture()
    local = critical_moisture_state(
        primary_moisture=primary,
        local_air_temperature=MeasuredAirTemperature(18.0),
    )
    assert isinstance(local, MoistureState)
    assert local.vapor_pressure_pa == primary.vapor_pressure_pa
    assert local.starting_relative_humidity_pct == pytest.approx(56.6490619823)
    assert local.starting_relative_humidity_pct != 50.0


def test_explicit_local_rh_overrides_shared_moisture_and_retains_declared_provenance() -> None:
    local = critical_moisture_state(
        primary_moisture=_primary_moisture(),
        local_air_temperature=MeasuredAirTemperature(18.0),
        local_relative_humidity=DeclaredRelativeHumidity(45.0),
    )
    assert isinstance(local, MoistureState)
    assert local.starting_relative_humidity_pct == 45.0
    assert local.provenance.value == "declared"


def test_prepare_location_selects_local_uniform_or_explicit_local_mrt() -> None:
    delta = _eligible_delta()
    common = dict(
        location_id="cold-corner",
        mode=CriticalEligibilityMode.HEATING,
        delta=delta,
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    uniform = prepare_critical_location(**common)
    assert isinstance(uniform, PreparedCriticalLocation)
    assert uniform.control_eligible
    assert "local_uniform_mrt" in uniform.reasons
    assert not uniform.context.radiant_uses_room_coordinate
    direct = prepare_critical_location(
        **common,
        local_radiant=DirectRadiantModel(MeasuredMeanRadiantTemperature(17.0)),
    )
    assert isinstance(direct, PreparedCriticalLocation)
    assert "local_direct_mrt" in direct.reasons


def test_nonuniform_zone_field_is_shared_and_monitoring_never_controls() -> None:
    prepared = prepare_critical_location(
        location_id="monitor",
        mode=CriticalEligibilityMode.MONITORING,
        delta=_eligible_delta(),
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=DirectRadiantModel(MeasuredMeanRadiantTemperature(16.0)),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    assert isinstance(prepared, PreparedCriticalLocation)
    assert prepared.context.radiant_uses_room_coordinate
    assert "shared_zone_radiant_field" in prepared.reasons
    assert not prepared.control_eligible


def test_warming_location_is_observable_but_not_control_eligible() -> None:
    warming = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=20.0,
        local_air_temperature_c=18.0,
        observed_at=NOW,
    )
    prepared = prepare_critical_location(
        location_id="warming",
        mode=CriticalEligibilityMode.BOTH,
        delta=warming,
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    assert isinstance(prepared, PreparedCriticalLocation)
    assert not prepared.control_eligible
    assert "control_ineligible_during_warmup" in prepared.reasons


def test_prepared_cold_location_maps_same_strategy_roots_to_room_coordinate() -> None:
    prepared = prepare_critical_location(
        location_id="cold",
        mode=CriticalEligibilityMode.HEATING,
        delta=_eligible_delta(),
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    votes = strategy_votes(ComfortStrategy.BALANCED)
    assert isinstance(prepared, PreparedCriticalLocation)
    assert isinstance(votes, StrategyVotes)
    solution = solve_critical_location(prepared, votes, budget=EvaluationBudget())
    assert isinstance(solution.roots.heating_control, RootSuccess)
    assert solution.roots.heating_control.requested_vote == votes.heating_control
    assert solution.roots.heating_control.mapped_room_temperature_c == pytest.approx(
        21.362709, abs=0.005
    )
    assert solution.roots.heating_control.local_candidate_temperature_c == pytest.approx(
        19.362709, abs=0.005
    )
    assert solution.control_eligible


def test_incomplete_critical_roots_exclude_location_from_control() -> None:
    humid = moisture_state(28.0, MeasuredRelativeHumidity(80.0))
    assert isinstance(humid, MoistureState)
    delta = _eligible_delta(0.0)
    prepared = prepare_critical_location(
        location_id="humid",
        mode=CriticalEligibilityMode.COOLING,
        delta=delta,
        current_room_air_temperature_c=28.0,
        local_air_temperature=MeasuredAirTemperature(28.0),
        primary_moisture=humid,
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=25.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    votes = strategy_votes(ComfortStrategy.BALANCED)
    assert isinstance(prepared, PreparedCriticalLocation)
    assert isinstance(votes, StrategyVotes)
    result = solve_critical_location(prepared, votes, budget=EvaluationBudget())
    assert not result.control_eligible
    assert "critical_roots_incomplete" in result.reasons


def test_unavailable_location_and_count_limit_are_explicit() -> None:
    unavailable = prepare_critical_location(
        location_id="",
        mode=CriticalEligibilityMode.HEATING,
        delta=_eligible_delta(),
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    assert isinstance(unavailable, CriticalLocationFailure)
    assert validate_critical_location_count(8) is None
    assert validate_critical_location_count(9) == "too_many_critical_locations"
    assert validate_critical_location_count(-1) == "too_many_critical_locations"


def test_missing_delta_and_physically_impossible_local_moisture_are_explicit() -> None:
    missing = prepare_critical_location(
        location_id="missing",
        mode=CriticalEligibilityMode.HEATING,
        delta=update_critical_delta(
            CriticalDeltaState(),
            primary_air_temperature_c=20.0,
            local_air_temperature_c=18.0,
            observed_at=NOW,
            valid=False,
        ),
        current_room_air_temperature_c=20.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=_primary_moisture(),
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=5.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    assert isinstance(missing, CriticalLocationFailure)
    assert missing.reason == "critical_observation_invalid"

    humid = moisture_state(28.0, MeasuredRelativeHumidity(80.0))
    assert isinstance(humid, MoistureState)
    impossible = prepare_critical_location(
        location_id="saturated",
        mode=CriticalEligibilityMode.HEATING,
        delta=_eligible_delta(),
        current_room_air_temperature_c=28.0,
        local_air_temperature=MeasuredAirTemperature(18.0),
        primary_moisture=humid,
        zone_radiant=UniformRadiantModel(),
        relative_air_speed_m_s=0.13,
        met=1.1,
        running_mean_c=25.0,
        clothing=AUTOMATIC_CLOTHING,
    )
    assert isinstance(impossible, CriticalLocationFailure)
    assert impossible.reason == "moisture_limited_no_solution"


def test_huge_integer_delta_input_is_invalid_not_an_exception() -> None:
    result = update_critical_delta(
        CriticalDeltaState(),
        primary_air_temperature_c=10**10000,
        local_air_temperature_c=18.0,
        observed_at=NOW,
    )
    assert not result.eligible
    assert result.reasons == ("critical_observation_invalid",)

"""Bounded heat demand with missing primary temperature feedback."""

from datetime import UTC, datetime, timedelta

from custom_components.athb.core.stale_heating import (
    HeatGuard,
    HeatGuardPhase,
    advance_heat_guard,
    ramp_heating_target,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
ACTIVE = timedelta(minutes=30)
START = timedelta(minutes=60)
RAMP = timedelta(minutes=30)


def advance(
    guard: HeatGuard,
    *,
    now: datetime,
    report_at: datetime | None,
    stale: bool,
    demand: bool = True,
    target: float = 22.0,
) -> HeatGuard:
    return advance_heat_guard(
        guard,
        now=now,
        report_at=report_at,
        source_stale=stale,
        demand=demand,
        active_timeout=ACTIVE,
        stale_start_timeout=START,
        ramp_maximum=RAMP,
        start_target_c=target,
    )


def test_stale_start_is_full_hour_and_does_not_restart() -> None:
    old = NOW - timedelta(hours=8)
    guard = advance(HeatGuard(), now=NOW, report_at=old, stale=True)
    assert guard.phase is HeatGuardPhase.STALE_START
    assert (
        advance(guard, now=NOW + timedelta(minutes=40), report_at=old, stale=True).phase
        is HeatGuardPhase.STALE_START
    )
    guard = advance(guard, now=NOW + START, report_at=old, stale=True)
    assert guard.phase is HeatGuardPhase.RAMP
    assert guard.ramp_at == NOW + START
    guard = advance(guard, now=NOW + START + RAMP, report_at=old, stale=True)
    assert guard.phase is HeatGuardPhase.EXHAUSTED
    guard = advance(guard, now=NOW + timedelta(hours=2), report_at=old, stale=True, demand=False)
    assert (
        advance(guard, now=NOW + timedelta(hours=3), report_at=old, stale=True).phase
        is HeatGuardPhase.EXHAUSTED
    )


def test_new_report_continuously_resets_active_feedback_deadline() -> None:
    guard = advance(HeatGuard(), now=NOW, report_at=NOW, stale=False)
    guard = advance(guard, now=NOW + timedelta(minutes=29), report_at=NOW, stale=False)
    assert guard.phase is HeatGuardPhase.MONITORING
    guard = advance(
        guard, now=NOW + timedelta(minutes=29), report_at=NOW + timedelta(minutes=29), stale=False
    )
    assert (
        advance(
            guard, now=NOW + timedelta(minutes=58), report_at=guard.report_at, stale=False
        ).phase
        is HeatGuardPhase.MONITORING
    )
    assert (
        advance(guard, now=NOW + timedelta(minutes=59), report_at=guard.report_at, stale=True).phase
        is HeatGuardPhase.RAMP
    )


def test_stale_trial_switches_to_active_monitoring_after_same_value_report() -> None:
    old = NOW - timedelta(hours=8)
    guard = advance(HeatGuard(), now=NOW, report_at=old, stale=True)
    guard = advance(
        guard, now=NOW + timedelta(minutes=59), report_at=NOW + timedelta(minutes=59), stale=False
    )
    assert guard.phase is HeatGuardPhase.MONITORING
    assert (
        advance(
            guard, now=NOW + timedelta(minutes=88), report_at=guard.report_at, stale=False
        ).phase
        is HeatGuardPhase.MONITORING
    )


def test_ramp_rate_and_hard_deadline() -> None:
    guard = HeatGuard(HeatGuardPhase.RAMP, ramp_at=NOW, ramp_start_c=21.0)
    assert (
        ramp_heating_target(
            guard,
            now=NOW + timedelta(minutes=10),
            fallback_c=18.0,
            minutes_per_degree=10,
            maximum_minutes=30,
        )
        == 20.0
    )
    assert (
        ramp_heating_target(
            guard, now=NOW + RAMP, fallback_c=18.0, minutes_per_degree=10, maximum_minutes=30
        )
        == 18.0
    )
    high = HeatGuard(HeatGuardPhase.RAMP, ramp_at=NOW, ramp_start_c=24.0)
    assert (
        ramp_heating_target(
            high,
            now=NOW + timedelta(minutes=10),
            fallback_c=18.0,
            minutes_per_degree=10,
            maximum_minutes=30,
        )
        == 22.0
    )
    assert (
        ramp_heating_target(
            HeatGuard(), now=NOW, fallback_c=18.0, minutes_per_degree=10, maximum_minutes=30
        )
        is None
    )
    assert (
        ramp_heating_target(
            HeatGuard(HeatGuardPhase.EXHAUSTED),
            now=NOW,
            fallback_c=18.0,
            minutes_per_degree=10,
            maximum_minutes=30,
        )
        == 18.0
    )
    assert (
        ramp_heating_target(
            HeatGuard(HeatGuardPhase.RAMP),
            now=NOW,
            fallback_c=18.0,
            minutes_per_degree=10,
            maximum_minutes=30,
        )
        == 18.0
    )

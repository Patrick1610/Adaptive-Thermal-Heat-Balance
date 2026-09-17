"""Deterministic heat-demand feedback guard for a measured room temperature."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class HeatGuardPhase(StrEnum):
    IDLE = "idle"
    MONITORING = "monitoring"
    STALE_START = "stale_start"
    RAMP = "ramp"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class HeatGuard:
    phase: HeatGuardPhase = HeatGuardPhase.IDLE
    started_at: datetime | None = None
    report_at: datetime | None = None
    ramp_at: datetime | None = None
    ramp_start_c: float | None = None
    evidence_at: datetime | None = None


def advance_heat_guard(
    guard: HeatGuard,
    *,
    now: datetime,
    report_at: datetime | None,
    source_stale: bool,
    demand: bool,
    active_timeout: timedelta,
    stale_start_timeout: timedelta,
    ramp_maximum: timedelta,
    start_target_c: float,
) -> HeatGuard:
    """Advance without ever granting a second stale trial for the same report."""

    if report_at is not None and guard.evidence_at is not None and report_at > guard.evidence_at:
        guard = HeatGuard(
            HeatGuardPhase.MONITORING if demand else HeatGuardPhase.IDLE,
            now if demand else None,
            report_at,
            evidence_at=report_at,
        )
    if not demand:
        return replace(
            guard,
            phase=(
                HeatGuardPhase.EXHAUSTED
                if guard.phase in {HeatGuardPhase.RAMP, HeatGuardPhase.EXHAUSTED}
                else HeatGuardPhase.IDLE
            ),
            started_at=None,
            evidence_at=report_at if guard.evidence_at is None else guard.evidence_at,
        )
    if guard.phase is HeatGuardPhase.IDLE:
        guard = replace(
            guard,
            phase=HeatGuardPhase.STALE_START if source_stale else HeatGuardPhase.MONITORING,
            started_at=now,
            report_at=report_at,
            evidence_at=report_at,
        )
    if guard.phase is HeatGuardPhase.EXHAUSTED:
        return guard
    if guard.phase is HeatGuardPhase.RAMP:
        return (
            replace(guard, phase=HeatGuardPhase.EXHAUSTED)
            if guard.ramp_at is None or now >= guard.ramp_at + ramp_maximum
            else guard
        )
    deadline = (
        guard.started_at + stale_start_timeout
        if guard.phase is HeatGuardPhase.STALE_START and guard.started_at is not None
        else max(
            (value for value in (guard.started_at, guard.report_at) if value is not None),
            default=now,
        )
        + active_timeout
    )
    if now >= deadline:
        return replace(
            guard,
            phase=HeatGuardPhase.RAMP,
            ramp_at=deadline,
            ramp_start_c=start_target_c,
        )
    return guard


def ramp_heating_target(
    guard: HeatGuard,
    *,
    now: datetime,
    fallback_c: float,
    minutes_per_degree: float,
    maximum_minutes: float,
) -> float | None:
    """Monotone linear withdrawal, accelerated if the total deadline requires it."""

    if guard.phase not in {HeatGuardPhase.RAMP, HeatGuardPhase.EXHAUSTED}:
        return None
    if guard.phase is HeatGuardPhase.EXHAUSTED:
        return fallback_c
    if guard.ramp_at is None or guard.ramp_start_c is None:
        return fallback_c
    start = max(fallback_c, guard.ramp_start_c)
    elapsed = max(0.0, (now - guard.ramp_at).total_seconds() / 60.0)
    rate = max(1.0 / minutes_per_degree, (start - fallback_c) / maximum_minutes)
    return max(fallback_c, start - rate * elapsed)

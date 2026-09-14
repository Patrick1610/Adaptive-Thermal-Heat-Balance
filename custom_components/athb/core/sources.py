"""Measurement validation, freshness, jump quarantine, and shared identity."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .contracts import Observation, ObservationValidity, Provenance


class SourceKind(StrEnum):
    PRIMARY_AIR = "primary_air"
    LOCAL_AIR = "local_air"
    RELATIVE_HUMIDITY = "relative_humidity"
    DIRECT_MRT = "direct_mrt"
    SURFACE = "surface"
    GLOBE = "globe"
    OUTDOOR = "outdoor"
    AIR_SPEED = "air_speed"


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    minimum: float
    maximum: float
    freshness: timedelta
    jump_limit: float | None
    jump_window: timedelta | None
    quarantine_consistency: float | None


SOURCE_POLICIES = {
    SourceKind.PRIMARY_AIR: SourcePolicy(
        -20.0, 60.0, timedelta(minutes=30), 3.0, timedelta(minutes=5), 1.0
    ),
    SourceKind.LOCAL_AIR: SourcePolicy(
        -20.0, 60.0, timedelta(minutes=30), 3.0, timedelta(minutes=5), 1.0
    ),
    SourceKind.RELATIVE_HUMIDITY: SourcePolicy(
        0.0, 100.0, timedelta(minutes=30), 25.0, timedelta(minutes=5), 5.0
    ),
    SourceKind.DIRECT_MRT: SourcePolicy(
        -20.0, 80.0, timedelta(minutes=30), 10.0, timedelta(minutes=1), 1.0
    ),
    SourceKind.SURFACE: SourcePolicy(
        -30.0, 100.0, timedelta(minutes=30), 10.0, timedelta(minutes=1), 1.0
    ),
    SourceKind.GLOBE: SourcePolicy(
        -30.0, 100.0, timedelta(minutes=30), 10.0, timedelta(minutes=1), 1.0
    ),
    SourceKind.OUTDOOR: SourcePolicy(
        -60.0, 60.0, timedelta(hours=2), 8.0, timedelta(minutes=5), 1.0
    ),
    SourceKind.AIR_SPEED: SourcePolicy(0.0, 2.0, timedelta(minutes=30), None, None, None),
}


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Stable registry identity, or generation-bound identity when registry ID is absent."""

    entity_id: str
    attribute: str | None
    registry_id: str | None
    source_generation: int

    @property
    def lineage_identity(self) -> str:
        if self.registry_id:
            return f"registry:{self.registry_id}"
        return f"unregistered:{self.entity_id}:{self.source_generation}"


@dataclass(frozen=True, slots=True)
class QuarantineState:
    values: tuple[tuple[datetime, float], ...] = ()


@dataclass(frozen=True, slots=True)
class SourceState:
    last_accepted: Observation | None = None
    quarantine: QuarantineState | None = None
    recovery_reports: tuple[datetime, ...] = ()
    recovering: bool = False


@dataclass(frozen=True, slots=True)
class SourceUpdate:
    observation: Observation
    state: SourceState


def _temperature_to_c(value: float, unit: str) -> float | None:
    normalized = unit.strip().lower().replace("°", "")
    if normalized in {"c", "degc", "celsius"}:
        return value
    if normalized in {"f", "degf", "fahrenheit"}:
        return (value - 32.0) * 5.0 / 9.0
    if normalized in {"k", "kelvin"}:
        return value - 273.15
    return None


def convert_source_value(
    kind: SourceKind, raw_value: object, unit: str
) -> tuple[float, str] | None:
    """Parse a finite non-Boolean value and normalize its physical unit."""

    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
        return None
    try:
        value = float(raw_value)
    except ValueError, OverflowError:
        return None
    if not math.isfinite(value):
        return None
    if kind in {
        SourceKind.PRIMARY_AIR,
        SourceKind.LOCAL_AIR,
        SourceKind.DIRECT_MRT,
        SourceKind.SURFACE,
        SourceKind.GLOBE,
        SourceKind.OUTDOOR,
    }:
        converted = _temperature_to_c(value, unit)
        return None if converted is None else (converted, "°C")
    if kind is SourceKind.RELATIVE_HUMIDITY:
        return (value, "%") if unit.strip() in {"%", "percent"} else None
    if kind is SourceKind.AIR_SPEED:
        normalized = unit.strip().lower()
        if normalized in {"m/s", "mps"}:
            return value, "m/s"
        if normalized in {"km/h", "kmh"}:
            return value / 3.6, "m/s"
    return None


def _invalid(
    identity: SourceIdentity,
    *,
    now: datetime,
    reason: str,
    unit: str,
    value: float | None = None,
) -> Observation:
    return Observation(
        identity.lineage_identity,
        value,
        unit,
        None,
        now,
        Provenance.MEASURED,
        ObservationValidity.INVALID,
        (reason,),
    )


def _invalid_transition_state(state: SourceState) -> SourceState:
    """Require recovery reports only after a previously accepted observation."""

    return SourceState(state.last_accepted, recovering=state.last_accepted is not None)


def validate_measured_source(
    *,
    state: SourceState,
    identity: SourceIdentity,
    kind: SourceKind,
    raw_value: object,
    unit: str,
    observed_at: datetime | None,
    received_at: datetime,
    available: bool = True,
    freshness: timedelta | None = None,
) -> SourceUpdate:
    """Validate one entity observation and advance its quarantine/recovery state."""

    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    now = received_at.astimezone(UTC)
    if not available:
        observation = _invalid(identity, now=now, reason="source_unavailable", unit=unit)
        return SourceUpdate(observation, _invalid_transition_state(state))
    if observed_at is None or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        observation = _invalid(identity, now=now, reason="missing_freshness", unit=unit)
        return SourceUpdate(observation, _invalid_transition_state(state))
    observed = observed_at.astimezone(UTC)
    converted = convert_source_value(kind, raw_value, unit)
    if converted is None:
        observation = _invalid(identity, now=now, reason="invalid_numeric_or_unit", unit=unit)
        return SourceUpdate(observation, _invalid_transition_state(state))
    value, canonical_unit = converted
    policy = SOURCE_POLICIES[kind]
    if not policy.minimum <= value <= policy.maximum:
        observation = _invalid(
            identity,
            now=now,
            reason="outside_source_limits",
            unit=canonical_unit,
            value=value,
        )
        return SourceUpdate(observation, _invalid_transition_state(state))
    maximum_age = freshness if freshness is not None else policy.freshness
    if maximum_age <= timedelta(0):
        raise ValueError("freshness must be positive")
    age = now - observed
    if age < timedelta(0) or age > maximum_age:
        observation = Observation(
            identity.lineage_identity,
            value,
            canonical_unit,
            observed,
            now,
            Provenance.MEASURED,
            ObservationValidity.STALE,
            ("source_stale",),
        )
        # A fresh timestamp is sufficient to recover from age alone. Availability,
        # invalid-value and jump failures retain the stricter multi-report gate.
        return SourceUpdate(observation, SourceState(state.last_accepted, recovering=False))

    last = state.last_accepted
    if last is not None and last.observed_at is not None:
        if observed == last.observed_at and value == last.value and canonical_unit == last.unit:
            return SourceUpdate(last, state)
        if observed <= last.observed_at:
            observation = _invalid(
                identity,
                now=now,
                reason=(
                    "timestamp_value_conflict"
                    if observed == last.observed_at
                    else "non_increasing_timestamp"
                ),
                unit=canonical_unit,
                value=value,
            )
            return SourceUpdate(observation, SourceState(last, recovering=True))

    jump = False
    if (
        last is not None
        and last.value is not None
        and last.observed_at is not None
        and policy.jump_limit is not None
        and policy.jump_window is not None
        and timedelta(0) <= observed - last.observed_at <= policy.jump_window
        and abs(value - last.value) > policy.jump_limit
    ):
        jump = True

    quarantine = state.quarantine
    if jump and quarantine is None:
        quarantine = QuarantineState(((observed, value),))
    elif quarantine is not None:
        values = (*quarantine.values, (observed, value))
        consistency = policy.quarantine_consistency
        mutually_consistent = (
            consistency is not None
            and max(item[1] for item in values) - min(item[1] for item in values) <= consistency
        )
        spans_minute = (values[-1][0] - values[0][0]).total_seconds() >= 60.0
        if len(values) >= 3 and spans_minute and mutually_consistent:
            quarantine = None
        else:
            quarantine = QuarantineState(values[-3:])

    if quarantine is not None:
        observation = Observation(
            identity.lineage_identity,
            value,
            canonical_unit,
            observed,
            now,
            Provenance.MEASURED,
            ObservationValidity.INVALID,
            ("jump_quarantine",),
        )
        return SourceUpdate(observation, SourceState(last, quarantine, recovering=True))

    observation = Observation(
        identity.lineage_identity,
        value,
        canonical_unit,
        observed,
        now,
        Provenance.MEASURED,
        ObservationValidity.VALID,
    )
    # A transient unavailable state during Home Assistant startup must not make
    # the first valid baseline wait for a second physical sensor report. The
    # strict recovery gate remains active after any previously accepted value.
    recovering = state.recovering and last is not None
    recovery = (*state.recovery_reports, observed)[-2:] if recovering else ()
    ready = len(recovery) >= 2 and (recovery[-1] - recovery[-2]).total_seconds() >= 30.0
    return SourceUpdate(
        observation, SourceState(observation, None, recovery, recovering and not ready)
    )


def declared_observation(
    *,
    source_identity: str,
    value: object,
    unit: str,
    minimum: float,
    maximum: float,
) -> Observation:
    """Validate a fixed declaration without fabricating sensor timestamps."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        numeric = None
    else:
        try:
            numeric = float(value)
        except OverflowError:
            numeric = None
    valid = numeric is not None and math.isfinite(numeric) and minimum <= numeric <= maximum
    return Observation(
        source_identity,
        numeric if valid else None,
        unit,
        None,
        None,
        Provenance.DECLARED,
        ObservationValidity.VALID if valid else ObservationValidity.INVALID,
        () if valid else ("invalid_declaration",),
    )


def primary_recovery_ready(state: SourceState) -> bool:
    """Require two valid recovery reports spanning at least 30 seconds."""

    return (
        len(state.recovery_reports) >= 2
        and (state.recovery_reports[-1] - state.recovery_reports[-2]).total_seconds() >= 30.0
    )

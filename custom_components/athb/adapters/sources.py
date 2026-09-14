"""Public Home Assistant state adapters for validated ATHB snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.climate.const import ATTR_CURRENT_TEMPERATURE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from ..core.contracts import Observation, ObservationValidity
from ..core.sources import (
    SOURCE_POLICIES,
    SourceIdentity,
    SourceKind,
    SourceState,
    convert_source_value,
    validate_measured_source,
)


@dataclass(frozen=True, slots=True)
class StateValue:
    """Small immutable public-state snapshot with no arbitrary attributes."""

    entity_id: str
    raw_state: object
    unit: str
    observed_at: datetime | None
    available: bool
    attributes: dict[str, object]
    context_id: str | None
    parent_context_id: str | None
    registry_identity: str | None = None
    source_generation: int = 1
    source_attribute: str | None = None


FRESHNESS_OPTION_KEYS = {
    SourceKind.PRIMARY_AIR: "primary_temperature_freshness_minutes",
    SourceKind.LOCAL_AIR: "local_temperature_freshness_minutes",
    SourceKind.RELATIVE_HUMIDITY: "relative_humidity_freshness_minutes",
    SourceKind.DIRECT_MRT: "radiant_freshness_minutes",
    SourceKind.SURFACE: "radiant_freshness_minutes",
    SourceKind.GLOBE: "radiant_freshness_minutes",
    SourceKind.AIR_SPEED: "air_speed_freshness_minutes",
}


def configured_freshness(options: Mapping[str, object], kind: SourceKind) -> timedelta:
    """Resolve a validated per-source freshness window, retaining safe defaults."""

    default = SOURCE_POLICIES[kind].freshness
    key = FRESHNESS_OPTION_KEYS.get(kind)
    if key is None:
        return default
    raw = options.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return default
    minutes = float(raw)
    if not math.isfinite(minutes) or not 5.0 <= minutes <= 360.0:
        return default
    return timedelta(minutes=minutes)


def snapshot_state(
    state: State | None,
    *,
    attributes: tuple[str, ...] = (),
    registry_identity: str | None = None,
    source_generation: int = 1,
) -> StateValue | None:
    """Capture only explicitly requested public attributes."""

    if state is None:
        return None
    reported = getattr(state, "last_reported", None) or state.last_updated
    return StateValue(
        state.entity_id,
        state.state,
        str(state.attributes.get("unit_of_measurement", "")),
        reported.astimezone(UTC) if reported is not None else None,
        state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE},
        {name: state.attributes.get(name) for name in attributes},
        state.context.id,
        state.context.parent_id,
        registry_identity,
        source_generation,
    )


def snapshot_primary_temperature(
    state: State | None,
    *,
    climate_unit: str | None = None,
    registry_identity: str | None = None,
    source_generation: int = 1,
) -> StateValue | None:
    """Capture a sensor state or a climate's public current-temperature attribute."""

    captured = snapshot_state(
        state,
        attributes=(ATTR_CURRENT_TEMPERATURE,),
        registry_identity=registry_identity,
        source_generation=source_generation,
    )
    if captured is None or not captured.entity_id.startswith("climate."):
        return captured
    return replace(
        captured,
        raw_state=captured.attributes.get(ATTR_CURRENT_TEMPERATURE),
        unit=captured.unit or str(climate_unit or ""),
        source_attribute=ATTR_CURRENT_TEMPERATURE,
    )


def validate_state_value(
    value: StateValue | None,
    *,
    kind: SourceKind,
    now: datetime,
    prior: SourceState | None = None,
    generation: int = 1,
    freshness: timedelta | None = None,
) -> tuple[Observation, SourceState]:
    """Validate a captured HA value through the production source validator."""

    if value is None:
        identity = SourceIdentity("missing", None, None, generation)
        update = validate_measured_source(
            state=prior or SourceState(),
            identity=identity,
            kind=kind,
            raw_value=None,
            unit="",
            observed_at=None,
            received_at=now,
            available=False,
        )
        return update.observation, update.state
    identity = SourceIdentity(
        value.entity_id,
        value.source_attribute,
        value.registry_identity,
        value.source_generation if value.source_generation > 0 else generation,
    )
    if (
        prior is not None
        and prior.last_accepted is not None
        and prior.last_accepted.source_identity != identity.lineage_identity
    ):
        prior = None
    if (
        prior is not None
        and prior.last_accepted is not None
        and prior.last_accepted.observed_at == value.observed_at
        and value.observed_at is not None
        and now - value.observed_at
        <= (freshness if freshness is not None else SOURCE_POLICIES[kind].freshness)
        and (converted := convert_source_value(kind, value.raw_state, value.unit)) is not None
        and prior.last_accepted.value == converted[0]
    ):
        return prior.last_accepted, prior
    update = validate_measured_source(
        state=prior or SourceState(),
        identity=identity,
        kind=kind,
        raw_value=value.raw_state,
        unit=value.unit,
        observed_at=value.observed_at,
        received_at=now,
        available=value.available,
        freshness=freshness,
    )
    return update.observation, update.state


def valid_value(observation: Observation) -> float | None:
    """Return only a finite current validated value."""

    if observation.validity is not ObservationValidity.VALID:
        return None
    value = observation.value
    if value is None or isinstance(value, bool) or not math.isfinite(value):
        return None
    return value


def primitive(value: Any) -> str | int | float | bool | None:
    """Restrict diagnostics/config snapshots to JSON primitives."""

    return value if isinstance(value, str | int | float | bool) or value is None else str(value)

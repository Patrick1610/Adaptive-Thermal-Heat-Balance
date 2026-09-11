"""Public Home Assistant state adapters for validated ATHB snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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


def validate_state_value(
    value: StateValue | None,
    *,
    kind: SourceKind,
    now: datetime,
    prior: SourceState | None = None,
    generation: int = 1,
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
        None,
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
        and now - value.observed_at <= SOURCE_POLICIES[kind].freshness
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

"""Source validation, provenance, quarantine, and recovery tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.core.contracts import ObservationValidity, Provenance
from custom_components.athb.core.sources import (
    SourceIdentity,
    SourceKind,
    SourceState,
    convert_source_value,
    declared_observation,
    primary_recovery_ready,
    validate_measured_source,
)

NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
IDENTITY = SourceIdentity("sensor.room", None, "registry-1", 1)


def _update(
    state: SourceState | None = None,
    *,
    value: object = 20.0,
    unit: str = "°C",
    observed_at: datetime | None = NOW,
    received_at: datetime = NOW,
    available: bool = True,
    kind: SourceKind = SourceKind.PRIMARY_AIR,
    freshness: timedelta | None = None,
):
    return validate_measured_source(
        state=state if state is not None else SourceState(),
        identity=IDENTITY,
        kind=kind,
        raw_value=value,
        unit=unit,
        observed_at=observed_at,
        received_at=received_at,
        available=available,
        freshness=freshness,
    )


@pytest.mark.parametrize(
    ("kind", "value", "unit", "expected", "canonical"),
    [
        (SourceKind.OUTDOOR, 68.0, "°F", 20.0, "°C"),
        (SourceKind.OUTDOOR, 293.15, "K", 20.0, "°C"),
        (SourceKind.RELATIVE_HUMIDITY, "50", "%", 50.0, "%"),
        (SourceKind.AIR_SPEED, 3.6, "km/h", 1.0, "m/s"),
    ],
)
def test_supported_units_are_normalized(
    kind: SourceKind, value: object, unit: str, expected: float, canonical: str
) -> None:
    converted = convert_source_value(kind, value, unit)
    assert converted is not None
    assert converted[0] == pytest.approx(expected)
    assert converted[1] == canonical


@pytest.mark.parametrize(
    ("value", "unit"),
    [(True, "°C"), ("unknown", "°C"), ("NaN", "°C"), (20.0, "watts")],
)
def test_invalid_raw_states_or_units_are_never_replaced(value: object, unit: str) -> None:
    result = _update(value=value, unit=unit)
    assert result.observation.validity is ObservationValidity.INVALID
    assert result.observation.value is None
    assert result.observation.reasons == ("invalid_numeric_or_unit",)


def test_measured_source_retains_identity_timestamp_and_provenance() -> None:
    result = _update()
    assert result.observation.source_identity == "registry:registry-1"
    assert result.observation.observed_at == NOW
    assert result.observation.received_at == NOW
    assert result.observation.provenance is Provenance.MEASURED
    assert result.observation.validity is ObservationValidity.VALID


def test_source_limits_freshness_availability_and_timestamp_order_are_explicit() -> None:
    outside = _update(value=61.0)
    assert outside.observation.reasons == ("outside_source_limits",)
    stale = _update(observed_at=NOW - timedelta(minutes=31))
    assert stale.observation.validity is ObservationValidity.STALE
    future = _update(observed_at=NOW + timedelta(seconds=1))
    assert future.observation.validity is ObservationValidity.STALE
    unavailable = _update(available=False)
    assert unavailable.observation.reasons == ("source_unavailable",)
    missing = _update(observed_at=None)
    assert missing.observation.reasons == ("missing_freshness",)
    first = _update()
    duplicate = _update(first.state, received_at=NOW + timedelta(seconds=1))
    assert duplicate.observation is first.observation
    assert duplicate.state is first.state
    older = _update(
        first.state,
        observed_at=NOW - timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    )
    assert older.observation.reasons == ("non_increasing_timestamp",)
    conflict = _update(first.state, value=21.0, received_at=NOW + timedelta(seconds=1))
    assert conflict.observation.reasons == ("timestamp_value_conflict",)


def test_reusing_same_observation_does_not_advance_recovery_reports() -> None:
    invalid = _update(available=False)
    first_recovery = _update(
        invalid.state,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    )

    replay = _update(
        first_recovery.state,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=10),
    )

    assert replay.observation is first_recovery.observation
    assert replay.state.recovering is True
    assert replay.state.recovery_reports == first_recovery.state.recovery_reports


def test_configured_freshness_can_extend_but_not_disable_stale_screening() -> None:
    accepted = _update(
        observed_at=NOW - timedelta(minutes=90),
        freshness=timedelta(hours=2),
    )
    assert accepted.observation.validity is ObservationValidity.VALID
    stale = _update(
        observed_at=NOW - timedelta(minutes=121),
        freshness=timedelta(hours=2),
    )
    assert stale.observation.validity is ObservationValidity.STALE
    with pytest.raises(ValueError, match="freshness must be positive"):
        _update(freshness=timedelta(0))


def test_age_only_staleness_recovers_on_one_genuinely_new_report() -> None:
    stale = _update(observed_at=NOW - timedelta(minutes=31))
    assert stale.observation.validity is ObservationValidity.STALE
    assert not stale.state.recovering

    recovered = _update(
        stale.state,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    )

    assert recovered.observation.validity is ObservationValidity.VALID
    assert not recovered.state.recovering


def test_unavailable_source_keeps_strict_multi_report_recovery() -> None:
    unavailable = _update(available=False)
    assert unavailable.state.recovering

    first = _update(
        unavailable.state,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    )

    assert first.observation.validity is ObservationValidity.VALID
    assert first.state.recovering
    assert not primary_recovery_ready(first.state)


def test_jump_quarantine_requires_three_consistent_reports_spanning_a_minute() -> None:
    accepted = _update()
    first = _update(
        accepted.state,
        value=25.0,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    )
    assert first.observation.reasons == ("jump_quarantine",)
    second = _update(
        first.state,
        value=25.5,
        observed_at=NOW + timedelta(seconds=31),
        received_at=NOW + timedelta(seconds=31),
    )
    assert second.observation.validity is ObservationValidity.INVALID
    third = _update(
        second.state,
        value=25.2,
        observed_at=NOW + timedelta(seconds=61),
        received_at=NOW + timedelta(seconds=61),
    )
    assert third.observation.validity is ObservationValidity.VALID
    assert third.state.recovering
    recovered = _update(
        third.state,
        value=25.1,
        observed_at=NOW + timedelta(seconds=92),
        received_at=NOW + timedelta(seconds=92),
    )
    assert recovered.observation.validity is ObservationValidity.VALID
    assert primary_recovery_ready(recovered.state)
    assert not recovered.state.recovering


def test_inconsistent_quarantine_reports_do_not_release() -> None:
    accepted = _update()
    state = _update(
        accepted.state,
        value=25.0,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
    ).state
    state = _update(
        state,
        value=27.0,
        observed_at=NOW + timedelta(seconds=31),
        received_at=NOW + timedelta(seconds=31),
    ).state
    result = _update(
        state,
        value=25.0,
        observed_at=NOW + timedelta(seconds=62),
        received_at=NOW + timedelta(seconds=62),
    )
    assert result.observation.validity is ObservationValidity.INVALID
    assert result.state.quarantine is not None


def test_declared_values_have_no_fabricated_timestamps_and_strict_type() -> None:
    declared = declared_observation(
        source_identity="config:rh", value=50.0, unit="%", minimum=0.0, maximum=100.0
    )
    assert declared.provenance is Provenance.DECLARED
    assert declared.observed_at is None
    assert declared.received_at is None
    assert declared.validity is ObservationValidity.VALID
    for invalid in (True, "50", float("nan"), 101.0):
        result = declared_observation(
            source_identity="config:rh",
            value=invalid,
            unit="%",
            minimum=0.0,
            maximum=100.0,
        )
        assert result.validity is ObservationValidity.INVALID
        assert result.value is None


def test_registry_identity_survives_rename_but_unknown_identity_is_generation_bound() -> None:
    renamed = SourceIdentity("sensor.renamed", None, "registry-1", 99)
    assert renamed.lineage_identity == IDENTITY.lineage_identity
    first = SourceIdentity("sensor.outdoor", None, None, 1)
    replacement = SourceIdentity("sensor.outdoor", None, None, 2)
    assert first.lineage_identity != replacement.lineage_identity


def test_received_timestamp_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _update(received_at=datetime(2026, 9, 11, 10, 0))

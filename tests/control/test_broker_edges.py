"""Safety-critical broker edge and branch coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.adapters.broker import (
    AcknowledgedTarget,
    BrokerPreflight,
    CommandBroker,
    ContextToken,
    NormalizedIntent,
    PendingCommand,
    _maximum_change_ha,
    _preflight_reason,
    _release_hysteresis_blocks,
    anti_chatter_reason,
    service_payload,
)
from custom_components.athb.core.contracts import (
    AcknowledgementStatus,
    ActuationDirection,
    DispatchStatus,
    TargetShape,
)
from custom_components.athb.core.ownership import FeedbackObservation, Ownership, TargetFingerprint

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _intent(*, value: float = 20.0, shape: TargetShape = TargetShape.SCALAR) -> NormalizedIntent:
    return NormalizedIntent(
        "climate.test",
        "registry-1",
        shape,
        ActuationDirection.HEATING_ONLY
        if shape is TargetShape.SCALAR
        else ActuationDirection.RANGED,
        value if shape is TargetShape.SCALAR else None,
        19.0 if shape is TargetShape.RANGE else None,
        23.0 if shape is TargetShape.RANGE else None,
        value if shape is TargetShape.SCALAR else None,
        19.0 if shape is TargetShape.RANGE else None,
        23.0 if shape is TargetShape.RANGE else None,
        value - 0.2 if shape is TargetShape.SCALAR else None,
        18.8 if shape is TargetShape.RANGE else None,
        23.2 if shape is TargetShape.RANGE else None,
        0.5,
        0.5,
        0.2,
        1,
        1,
        1,
        1,
        0,
        "zone-1",
        NOW,
        NOW + timedelta(minutes=2),
    )


def _preflight() -> BrokerPreflight:
    return BrokerPreflight("registry-1", 1, 1, 1, 1, Ownership.OWNED, True, True, "zone-1")


class Service:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def async_set_temperature(
        self, payload: dict[str, object], context: ContextToken
    ) -> None:
        del context
        self.calls.append(payload)


class Persistence:
    def __init__(self) -> None:
        self.persist_ok = True
        self.dispatch_ok = True
        self.after_persist = None
        self.resolved: list[str] = []

    async def async_persist_pending(self, command: PendingCommand) -> bool:
        del command
        if self.after_persist is not None:
            self.after_persist()
        return self.persist_ok

    async def async_mark_dispatched(self, command: PendingCommand) -> bool:
        del command
        return self.dispatch_ok

    async def async_resolve(self, command: PendingCommand, reason: str) -> bool:
        del command
        self.resolved.append(reason)
        return True


def _broker(
    current: list[BrokerPreflight], service: Service, persistence: Persistence
) -> CommandBroker:
    return CommandBroker(
        service=service,
        persistence=persistence,
        preflight=lambda _identity: current[0],
        command_id_factory=lambda: "cmd-edge",
        context_factory=lambda: ContextToken("ctx-edge", object()),
    )


def _feedback(value: float, *, context: str = "ctx-edge") -> FeedbackObservation:
    return FeedbackObservation(
        "registry-1",
        TargetFingerprint(TargetShape.SCALAR, temperature_ha=value),
        context,
        None,
        1,
        1,
        1,
        1,
        0,
    )


def test_invalid_payload_shapes_are_rejected() -> None:
    with pytest.raises(ValueError, match="scalar"):
        service_payload(replace(_intent(), temperature_ha=None))
    with pytest.raises(ValueError, match="range"):
        service_payload(replace(_intent(shape=TargetShape.RANGE), target_temp_high_ha=None))


@pytest.mark.parametrize(
    ("current", "intent", "reason"),
    [
        (_preflight(), replace(_intent(), expires_at=NOW), "intent_expired"),
        (
            replace(_preflight(), target_registry_identity="other"),
            _intent(),
            "target_identity_changed",
        ),
        (replace(_preflight(), entry_generation=2), _intent(), "entry_generation_changed"),
        (replace(_preflight(), ownership_revision=2), _intent(), "ownership_revision_changed"),
        (replace(_preflight(), input_generation=2), _intent(), "input_generation_changed"),
    ],
)
def test_remaining_generation_preflight_failures(
    current: BrokerPreflight, intent: NormalizedIntent, reason: str
) -> None:
    assert _preflight_reason(intent, current, NOW) == reason


def test_shape_missing_values_and_range_change_measurement_fail_safe() -> None:
    scalar = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0)
    ranged = TargetFingerprint(TargetShape.RANGE, low_ha=19.0, high_ha=23.0)
    assert _maximum_change_ha(scalar, ranged) == float("inf")
    assert _maximum_change_ha(TargetFingerprint(TargetShape.SCALAR), scalar) == float("inf")
    assert _maximum_change_ha(TargetFingerprint(TargetShape.RANGE), ranged) == float("inf")
    assert (
        _maximum_change_ha(ranged, TargetFingerprint(TargetShape.RANGE, low_ha=18.5, high_ha=24.0))
        == 1.0
    )


def test_cooling_and_atomic_range_release_hysteresis() -> None:
    scalar_ack = AcknowledgedTarget(
        TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0), 20.0, None, None, NOW
    )
    cooling = replace(
        _intent(value=20.5),
        direction=ActuationDirection.COOLING_ONLY,
        continuous_bounded_room_c=20.55,
    )
    assert _release_hysteresis_blocks(cooling, scalar_ack)
    assert not _release_hysteresis_blocks(
        replace(cooling, continuous_bounded_room_c=20.6), scalar_ack
    )

    range_ack = AcknowledgedTarget(
        TargetFingerprint(TargetShape.RANGE, low_ha=19.5, high_ha=22.5),
        None,
        19.5,
        22.5,
        NOW,
    )
    ranged = replace(
        _intent(shape=TargetShape.RANGE),
        continuous_bounded_low_room_c=18.95,
        continuous_bounded_high_room_c=23.05,
    )
    assert _release_hysteresis_blocks(ranged, range_ack)
    released = replace(
        ranged,
        continuous_bounded_low_room_c=18.9,
        continuous_bounded_high_room_c=23.1,
    )
    assert not _release_hysteresis_blocks(released, range_ack)
    incomplete = replace(ranged, normalized_low_room_c=None)
    assert not _release_hysteresis_blocks(incomplete, range_ack)


def test_storage_dispatch_verification_and_post_persist_gate_fail_closed() -> None:
    service, persistence, current = Service(), Persistence(), [_preflight()]
    persistence.dispatch_ok = False
    outcome = asyncio.run(_broker(current, service, persistence).async_submit(_intent(), now=NOW))
    assert outcome.reason == "storage_verification_failed"
    assert service.calls == []

    service, persistence, current = Service(), Persistence(), [_preflight()]
    broker = _broker(current, service, persistence)
    persistence.after_persist = broker.close_gate
    outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert outcome.reason == "dispatch_gate_closed"
    assert service.calls == []


def test_feedback_without_pending_and_rejected_feedback_paths() -> None:
    service, persistence, current = Service(), Persistence(), [_preflight()]
    broker = _broker(current, service, persistence)

    async def scenario() -> tuple[object, object]:
        external = await broker.async_feedback(_feedback(20.0), now=NOW)
        await broker.async_submit(_intent(), now=NOW)
        rejected = await broker.async_feedback(_feedback(19.0), now=NOW)
        return external, rejected

    external, rejected = asyncio.run(scenario())
    assert external.reason == "external_temperature_target"
    assert rejected.acknowledgement_status is AcknowledgementStatus.REJECTED
    assert persistence.resolved == ["coerced_or_rejected"]


def test_queue_drain_and_early_timeout_paths() -> None:
    service, persistence, current = Service(), Persistence(), [_preflight()]
    broker = _broker(current, service, persistence)

    async def scenario() -> tuple[object, object, object]:
        missing_timeout = await broker.async_acknowledgement_timeout("missing", now=NOW)
        empty_drain = await broker.async_drain_queued("missing", now=NOW)
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_submit(_intent(value=21.0), now=NOW)
        early = await broker.async_acknowledgement_timeout(
            "registry-1", now=NOW + timedelta(seconds=29)
        )
        await broker.async_feedback(_feedback(20.0), now=NOW + timedelta(seconds=30))
        drained = await broker.async_drain_queued("registry-1", now=NOW + timedelta(seconds=61))
        return missing_timeout, empty_drain, (early, drained)

    missing, empty, pair = asyncio.run(scenario())
    assert missing is None
    assert empty is None
    early, drained = pair
    assert early is None
    assert drained.dispatch_status is DispatchStatus.DISPATCHED
    assert service.calls[-1]["temperature"] == 21.0


def test_naive_clock_and_unused_invalidation_are_safe() -> None:
    service, persistence, current = Service(), Persistence(), [_preflight()]
    broker = _broker(current, service, persistence)
    assert broker.state_counts("missing") == (0, 0)
    broker.invalidate("registry-1")
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(broker.async_submit(_intent(), now=NOW.replace(tzinfo=None)))


def test_range_anti_chatter_change_and_missing_scalar_coordinates() -> None:
    ranged_ack = AcknowledgedTarget(
        TargetFingerprint(TargetShape.RANGE, low_ha=18.0, high_ha=24.0),
        None,
        18.0,
        24.0,
        NOW,
    )
    assert (
        anti_chatter_reason(
            _intent(shape=TargetShape.RANGE),
            acknowledged=ranged_ack,
            last_dispatch_at=None,
            now=NOW,
        )
        is None
    )
    scalar_ack = AcknowledgedTarget(
        TargetFingerprint(TargetShape.SCALAR, temperature_ha=19.0), None, None, None, NOW
    )
    assert not _release_hysteresis_blocks(_intent(), scalar_ack)

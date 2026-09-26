"""Command-broker payload, queue, anti-chatter, and race tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.athb.adapters.broker import (
    AcknowledgedTarget,
    BrokerPreflight,
    CommandBroker,
    CommandOutcome,
    ContextToken,
    NormalizedIntent,
    PendingCommand,
    TargetLeaseRegistry,
    anti_chatter_reason,
    service_payload,
)
from custom_components.athb.core.contracts import (
    AcknowledgementStatus,
    ActuationDirection,
    DispatchStatus,
    TargetShape,
)
from custom_components.athb.core.ownership import (
    FeedbackObservation,
    Ownership,
    TargetFingerprint,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ContextToken]] = []
        self.error = False
        self.on_call = None

    async def async_set_temperature(
        self, payload: dict[str, object], context: ContextToken
    ) -> None:
        self.calls.append((payload, context))
        if self.on_call is not None:
            await self.on_call()
        if self.error:
            raise TimeoutError("uncertain")


class FakePersistence:
    def __init__(self) -> None:
        self.persisted: list[PendingCommand] = []
        self.dispatched: list[PendingCommand] = []
        self.resolved: list[tuple[str, str]] = []
        self.persist_ok = True
        self.dispatch_ok = True
        self.after_persist: object = None

    async def async_persist_pending(self, command: PendingCommand) -> bool:
        self.persisted.append(command)
        if callable(self.after_persist):
            self.after_persist()
        return self.persist_ok

    async def async_mark_dispatched(self, command: PendingCommand) -> bool:
        self.dispatched.append(command)
        return self.dispatch_ok

    async def async_resolve(self, command: PendingCommand, reason: str) -> bool:
        self.resolved.append((command.command_id, reason))
        return True


def _intent(
    *,
    value: float = 20.0,
    direction: ActuationDirection = ActuationDirection.HEATING_ONLY,
    shape: TargetShape = TargetShape.SCALAR,
    explicit: bool = False,
    safety_deescalation: bool = False,
    input_generation: int = 1,
) -> NormalizedIntent:
    return NormalizedIntent(
        "climate.test",
        "registry-1",
        shape,
        direction,
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
        input_generation,
        1,
        1,
        0,
        "zone-1",
        NOW,
        NOW + timedelta(minutes=2),
        explicit,
        safety_deescalation,
    )


def _preflight() -> BrokerPreflight:
    return BrokerPreflight("registry-1", 1, 1, 1, 1, Ownership.OWNED, True, True, "zone-1")


def _broker(
    service: FakeService, persistence: FakePersistence, current: list[BrokerPreflight]
) -> CommandBroker:
    counter = iter(range(1, 100))
    return CommandBroker(
        service=service,
        persistence=persistence,
        preflight=lambda _target: current[0],
        command_id_factory=lambda: f"cmd-{next(counter)}",
        context_factory=lambda: ContextToken("ctx-1", object()),
    )


def _feedback(value: float, *, context: str | None = "ctx-1") -> FeedbackObservation:
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


def test_payloads_are_exact_and_never_include_hvac_mode() -> None:
    assert service_payload(_intent()) == {"entity_id": "climate.test", "temperature": 20.0}
    assert service_payload(
        _intent(shape=TargetShape.RANGE, direction=ActuationDirection.RANGED)
    ) == {
        "entity_id": "climate.test",
        "target_temp_low": 19.0,
        "target_temp_high": 23.0,
    }


def test_broker_persists_rechecks_then_dispatches_sole_temperature_call() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)
    outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert outcome.dispatch_status is DispatchStatus.DISPATCHED
    assert outcome.acknowledgement_status is AcknowledgementStatus.PENDING
    assert len(persistence.persisted) == len(persistence.dispatched) == 1
    assert service.calls[0][0] == {"entity_id": "climate.test", "temperature": 20.0}
    assert service.calls[0][1].context_id == "ctx-1"
    assert broker.state_counts("registry-1") == (1, 0)


def test_generation_change_during_persistence_cancels_before_service_dispatch() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    persistence.after_persist = lambda: current.__setitem__(
        0, replace(current[0], input_generation=2)
    )
    outcome = asyncio.run(_broker(service, persistence, current).async_submit(_intent(), now=NOW))
    assert outcome.reason == "input_generation_changed"
    assert outcome.dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert service.calls == []


def test_storage_verification_failure_inhibits_dispatch() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    persistence.persist_ok = False
    outcome = asyncio.run(_broker(service, persistence, current).async_submit(_intent(), now=NOW))
    assert outcome.reason == "storage_verification_failed"
    assert service.calls == []


def test_all_preflight_failures_suppress_without_queue_or_call() -> None:
    variants = (
        (replace(_preflight(), ownership=Ownership.DISABLED), "disabled"),
        (replace(_preflight(), target_ready=False), "target_not_ready"),
        (replace(_preflight(), data_ready=False), "data_not_ready"),
        (replace(_preflight(), lease_owner="zone-2"), "target_lease_lost"),
        (replace(_preflight(), capability_generation=2), "capability_generation_changed"),
        (replace(_preflight(), dispatch_gate_open=False), "dispatch_gate_closed"),
    )
    for preflight, reason in variants:
        service, persistence, current = FakeService(), FakePersistence(), [preflight]
        broker = _broker(service, persistence, current)
        outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
        assert outcome.reason == reason
        assert service.calls == []
        assert broker.state_counts("registry-1") == (0, 0)


def test_stale_safety_bypasses_only_data_readiness_and_keeps_exact_payload() -> None:
    stale = replace(_preflight(), data_ready=False)
    service, persistence, current = FakeService(), FakePersistence(), [stale]
    broker = _broker(service, persistence, current)

    outcome = asyncio.run(
        broker.async_submit(
            _intent(value=18.0, explicit=True, safety_deescalation=True),
            now=NOW,
        )
    )

    assert outcome.dispatch_status is DispatchStatus.DISPATCHED
    assert service.calls[0][0] == {"entity_id": "climate.test", "temperature": 18.0}
    assert "hvac_mode" not in service.calls[0][0]

    for unsafe in (
        replace(stale, ownership=Ownership.MANUAL_OVERRIDE),
        replace(stale, target_ready=False),
        replace(stale, lease_owner="zone-2"),
        replace(stale, dispatch_gate_open=False),
    ):
        blocked_service = FakeService()
        blocked = _broker(blocked_service, FakePersistence(), [unsafe])
        blocked_outcome = asyncio.run(
            blocked.async_submit(
                _intent(value=18.0, explicit=True, safety_deescalation=True),
                now=NOW,
            )
        )
        assert blocked_outcome.dispatch_status is DispatchStatus.NOT_DISPATCHED
        assert blocked_service.calls == []


def test_acknowledgement_updates_last_owned_target_and_suppresses_duplicate() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> tuple[object, object]:
        await broker.async_submit(_intent(), now=NOW)
        acknowledgement = await broker.async_feedback(
            _feedback(20.0), now=NOW + timedelta(seconds=1)
        )
        duplicate = await broker.async_submit(_intent(), now=NOW + timedelta(seconds=61))
        return acknowledgement, duplicate

    acknowledgement, duplicate = asyncio.run(scenario())
    assert acknowledgement.acknowledgement_status is AcknowledgementStatus.ACKNOWLEDGED
    assert duplicate.reason == "target_unchanged"
    assert len(service.calls) == 1


def test_feedback_before_service_return_is_not_overwritten_by_dispatch_persistence() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def feedback_during_call() -> None:
        outcome = await broker.async_feedback(_feedback(20.0), now=NOW)
        assert outcome.acknowledgement_status is AcknowledgementStatus.ACKNOWLEDGED

    service.on_call = feedback_during_call
    outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert outcome.acknowledgement_status is AcknowledgementStatus.ACKNOWLEDGED
    assert persistence.resolved == [("cmd-1", "own_context_match")]
    assert len(persistence.dispatched) == 1
    assert broker.state_counts("registry-1") == (0, 0)


def test_one_pending_and_one_latest_queued_intent() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        assert (await broker.async_submit(_intent(value=20.5), now=NOW)).reason == "command_pending"
        assert (await broker.async_submit(_intent(value=21.0), now=NOW)).reason == "command_pending"

    asyncio.run(scenario())
    assert broker.state_counts("registry-1") == (1, 1)
    assert len(service.calls) == 1


def test_command_failure_is_uncertain_and_never_blindly_retried() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    service.error = True
    broker = _broker(service, persistence, current)

    async def scenario() -> tuple[object, object]:
        failed = await broker.async_submit(_intent(), now=NOW)
        second = await broker.async_submit(_intent(value=20.5), now=NOW + timedelta(seconds=1))
        return failed, second

    failed, second = asyncio.run(scenario())
    assert failed.reason == "command_outcome_unknown"
    assert failed.acknowledgement_status is AcknowledgementStatus.UNKNOWN
    assert second.reason == "command_pending"
    assert len(service.calls) == 1


def test_explicit_resume_resolves_uncertain_pending_before_a_fresh_command() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    service.error = True
    broker = _broker(service, persistence, current)

    async def scenario() -> object:
        failed = await broker.async_submit(_intent(), now=NOW)
        assert failed.reason == "command_outcome_unknown"
        await broker.async_resume_target("registry-1")
        service.error = False
        current[0] = replace(current[0], input_generation=2)
        return await broker.async_submit(
            _intent(value=20.5, input_generation=2), now=NOW + timedelta(seconds=1)
        )

    resumed = asyncio.run(scenario())
    assert persistence.resolved == [("cmd-1", "explicit_resume")]
    assert resumed.reason == "awaiting_acknowledgement"
    assert len(service.calls) == 2


def test_acknowledgement_timeout_clears_queue_and_returns_unknown() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> object:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_submit(_intent(value=20.5), now=NOW)
        return await broker.async_acknowledgement_timeout(
            "registry-1", now=NOW + timedelta(seconds=30)
        )

    timeout = asyncio.run(scenario())
    assert timeout is not None
    assert timeout.reason == "command_outcome_unknown"
    assert broker.state_counts("registry-1") == (0, 0)


def test_close_gate_immediately_discards_future_intent() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)
    broker.close_gate()
    outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert outcome.reason == "dispatch_gate_closed"
    assert service.calls == []


def _ack(
    value: float,
    *,
    direction: ActuationDirection = ActuationDirection.HEATING_ONLY,
) -> AcknowledgedTarget:
    del direction
    return AcknowledgedTarget(
        TargetFingerprint(TargetShape.SCALAR, temperature_ha=value), value, None, None, NOW
    )


def test_anti_chatter_reason_order_and_directional_release_hysteresis() -> None:
    acknowledged = _ack(20.0)
    assert (
        anti_chatter_reason(
            _intent(value=20.0), acknowledged=acknowledged, last_dispatch_at=NOW, now=NOW
        )
        == "target_unchanged"
    )
    assert (
        anti_chatter_reason(
            replace(_intent(value=20.25), meaningful_delta_ha=0.5),
            acknowledged=acknowledged,
            last_dispatch_at=None,
            now=NOW,
        )
        == "below_minimum_change"
    )
    heating_release = replace(
        _intent(value=19.5), continuous_bounded_room_c=19.45, meaningful_delta_ha=0.5
    )
    assert (
        anti_chatter_reason(
            heating_release, acknowledged=acknowledged, last_dispatch_at=None, now=NOW
        )
        == "quantization_hysteresis"
    )
    assert (
        anti_chatter_reason(
            replace(heating_release, continuous_bounded_room_c=19.4),
            acknowledged=acknowledged,
            last_dispatch_at=NOW,
            now=NOW + timedelta(seconds=59),
        )
        == "command_interval"
    )


def test_floor_rounding_release_hysteresis_uses_crossed_grid_boundary() -> None:
    acknowledged = _ack(18.5)
    floor_release = replace(
        _intent(value=18.0),
        continuous_bounded_room_c=18.41,
        rounding_mode="floor",
        step_room_c=0.5,
    )

    assert (
        anti_chatter_reason(
            floor_release,
            acknowledged=acknowledged,
            last_dispatch_at=None,
            now=NOW,
        )
        == "quantization_hysteresis"
    )
    assert (
        anti_chatter_reason(
            replace(floor_release, continuous_bounded_room_c=18.4),
            acknowledged=acknowledged,
            last_dispatch_at=None,
            now=NOW,
        )
        is None
    )


def test_explicit_rounding_release_hysteresis_respects_policy_boundaries() -> None:
    heating_ack = _ack(18.5)
    cooling_ack = _ack(18.0)

    mathematical_heating = replace(
        _intent(value=18.0),
        continuous_bounded_room_c=18.16,
        rounding_mode="mathematical",
        step_room_c=0.5,
    )
    assert (
        anti_chatter_reason(
            mathematical_heating,
            acknowledged=heating_ack,
            last_dispatch_at=None,
            now=NOW,
        )
        == "quantization_hysteresis"
    )
    assert (
        anti_chatter_reason(
            replace(mathematical_heating, continuous_bounded_room_c=18.15),
            acknowledged=heating_ack,
            last_dispatch_at=None,
            now=NOW,
        )
        is None
    )

    ceiling_cooling = replace(
        _intent(value=18.5, direction=ActuationDirection.COOLING_ONLY),
        continuous_bounded_room_c=18.09,
        rounding_mode="ceiling",
        step_room_c=0.5,
    )
    assert (
        anti_chatter_reason(
            ceiling_cooling,
            acknowledged=cooling_ack,
            last_dispatch_at=None,
            now=NOW,
        )
        == "quantization_hysteresis"
    )
    assert (
        anti_chatter_reason(
            replace(ceiling_cooling, continuous_bounded_room_c=18.1),
            acknowledged=cooling_ack,
            last_dispatch_at=None,
            now=NOW,
        )
        is None
    )


def test_explicit_transition_bypasses_hysteresis_and_ordinary_but_not_hard_interval() -> None:
    intent = replace(_intent(value=19.5, explicit=True), continuous_bounded_room_c=19.49)
    acknowledged = _ack(20.0)
    assert (
        anti_chatter_reason(
            intent,
            acknowledged=acknowledged,
            last_dispatch_at=NOW,
            now=NOW + timedelta(seconds=9),
        )
        == "command_interval"
    )
    assert (
        anti_chatter_reason(
            intent,
            acknowledged=acknowledged,
            last_dispatch_at=NOW,
            now=NOW + timedelta(seconds=10),
        )
        is None
    )


def test_recovery_reassertion_accepts_matching_live_target_without_write() -> None:
    service = FakeService()
    persistence = FakePersistence()
    current = [
        replace(
            _preflight(),
            observed_target=TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0),
        )
    ]
    broker = _broker(service, persistence, current)

    outcome = asyncio.run(
        broker.async_submit(
            replace(
                _intent(value=20.0, explicit=True),
                recovery_reassertion=True,
            ),
            now=NOW,
        )
    )

    assert outcome.reason == "target_current"
    assert outcome.dispatch_status is DispatchStatus.NOT_DISPATCHED
    assert service.calls == []
    duplicate = asyncio.run(broker.async_submit(_intent(value=20.0), now=NOW))
    assert duplicate.reason == "target_unchanged"


def test_range_recovery_accepts_both_matching_live_endpoints_without_write() -> None:
    service = FakeService()
    persistence = FakePersistence()
    current = [
        replace(
            _preflight(),
            observed_target=TargetFingerprint(TargetShape.RANGE, low_ha=19.0, high_ha=23.0),
        )
    ]
    broker = _broker(service, persistence, current)

    outcome = asyncio.run(
        broker.async_submit(
            replace(
                _intent(
                    shape=TargetShape.RANGE,
                    direction=ActuationDirection.RANGED,
                    explicit=True,
                ),
                recovery_reassertion=True,
            ),
            now=NOW,
        )
    )

    assert outcome.reason == "target_current"
    assert service.calls == []


def test_recovery_reassertion_uses_live_target_and_bypasses_prior_acknowledgement() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> tuple[CommandOutcome, CommandOutcome]:
        initial = await broker.async_submit(_intent(value=20.0), now=NOW)
        assert initial.command_id is not None
        await broker.async_feedback(_feedback(20.0), now=NOW + timedelta(seconds=1))
        current[0] = replace(
            current[0],
            observed_target=TargetFingerprint(TargetShape.SCALAR, temperature_ha=19.5),
        )
        reasserted = await broker.async_submit(
            replace(
                _intent(value=20.0, explicit=True),
                meaningful_delta_ha=5.0,
                recovery_reassertion=True,
            ),
            now=NOW + timedelta(seconds=10),
        )
        return initial, reasserted

    initial, reasserted = asyncio.run(scenario())

    assert initial.dispatch_status is DispatchStatus.DISPATCHED
    assert reasserted.dispatch_status is DispatchStatus.DISPATCHED
    assert len(service.calls) == 2
    assert service.calls[-1][0] == {"entity_id": "climate.test", "temperature": 20.0}


def test_target_leases_prevent_duplicate_enabled_control() -> None:
    leases = TargetLeaseRegistry()
    assert leases.acquire("registry-1", "zone-a")
    assert leases.acquire("registry-1", "zone-a")
    assert not leases.acquire("registry-1", "zone-b")
    leases.release("registry-1", "zone-b")
    assert leases.owner("registry-1") == "zone-a"
    leases.release("registry-1", "zone-a")
    assert leases.acquire("registry-1", "zone-b")

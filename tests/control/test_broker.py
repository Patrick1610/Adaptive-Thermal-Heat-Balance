"""Command-broker payload, queue, anti-chatter, and race tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

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


@pytest.mark.parametrize("service_error", [False, True])
def test_five_attempts_are_spaced_and_same_grid_target_does_not_reset(service_error: bool) -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    service.error = service_error
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        for attempt in range(5):
            now = NOW + timedelta(seconds=60 * attempt)
            intent = replace(_intent(), created_at=now, expires_at=now + timedelta(minutes=2))
            await broker.async_submit(intent, now=now)
            assert broker.delivery_state("registry-1")["attempts"] == attempt + 1
            timeout = await broker.async_acknowledgement_timeout(
                "registry-1", now=now + timedelta(seconds=30)
            )
            assert timeout is not None
            blocked = await broker.async_submit(
                replace(intent, continuous_bounded_room_c=19.9, explicit_transition=True),
                now=now + timedelta(seconds=59),
            )
            assert blocked.reason == ("write_failed" if attempt == 4 else "command_interval")
        assert broker.delivery_state("registry-1")["status"] == "write_failed"
        assert broker.delivery_state("registry-1")["next_retry_at"] is None
        now = NOW + timedelta(minutes=6)
        failed = await broker.async_submit(
            replace(_intent(), created_at=now, expires_at=now + timedelta(minutes=2)), now=now
        )
        assert failed.reason == "write_failed"
        assert len(service.calls) == 5
        # A lowering is a new goal, not blocked by the exhausted earlier raise.
        lowered = await broker.async_submit(
            replace(_intent(value=18), created_at=now, expires_at=now + timedelta(minutes=2)),
            now=now,
        )
        assert lowered.dispatch_status in {DispatchStatus.DISPATCHED, DispatchStatus.FAILED}
        assert broker.delivery_state("registry-1")["attempts"] == 1
        assert len(service.calls) == 6

    asyncio.run(scenario())


def test_new_goal_during_retry_cannot_bypass_minute_interval() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
        assert (
            await broker.async_submit(
                _intent(value=18, explicit=True), now=NOW + timedelta(seconds=31)
            )
        ).reason == "command_interval"
        assert broker.delivery_state("registry-1")["attempts"] == 0
        await broker.async_submit(_intent(value=18), now=NOW + timedelta(seconds=60))
        assert [call[0]["temperature"] for call in service.calls] == [20, 18]

    asyncio.run(scenario())


def test_retry_live_preflight_does_not_overwrite_an_unprocessed_external_target() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    current[0] = replace(current[0], observed_target=_feedback(19).observed)
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
        current[0] = replace(current[0], observed_target=_feedback(18).observed)
        refused = await broker.async_submit(_intent(), now=NOW + timedelta(seconds=60))
        assert refused.reason == "external_temperature_target"
        assert len(service.calls) == 1
        # Live unavailable is a readiness problem, never a manual intervention.
        current[0] = replace(current[0], target_ready=False)
        assert (await broker.async_submit(_intent(), now=NOW + timedelta(seconds=61))).reason == (
            "target_not_ready"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "guard", ["data_ready", "lease_owner", "ownership", "capability_generation"]
)
def test_retry_rechecks_all_existing_guards(guard: str) -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
        changed = {
            "data_ready": False,
            "lease_owner": "other",
            "ownership": Ownership.MANUAL_OVERRIDE,
            "capability_generation": 2,
        }[guard]
        current[0] = replace(current[0], **{guard: changed})
        outcome = await broker.async_submit(_intent(), now=NOW + timedelta(seconds=60))
        assert outcome.dispatch_status is DispatchStatus.NOT_DISPATCHED
        assert len(service.calls) == 1

    asyncio.run(scenario())


def test_late_old_echo_does_not_acknowledge_new_goal_or_hide_user_intervention() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
        await broker.async_submit(_intent(value=18), now=NOW + timedelta(seconds=60))
        echo = await broker.async_feedback(
            _feedback(20, context="delegated"), now=NOW + timedelta(seconds=61)
        )
        assert echo.reason == "duplicate_command_echo"
        assert broker.state_counts("registry-1")[0] == 1
        user = await broker.async_feedback(
            replace(_feedback(18), user_initiated=True), now=NOW + timedelta(seconds=62)
        )
        assert user.reason == "external_temperature_target"
        assert broker.state_counts("registry-1")[0] == 0

    asyncio.run(scenario())


def test_readback_acknowledgement_wins_even_when_service_returns_error() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    service.error = True
    broker = _broker(service, persistence, current)

    async def acknowledge() -> None:
        await broker.async_feedback(_feedback(20), now=NOW)

    service.on_call = acknowledge
    outcome = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert outcome.acknowledgement_status is AcknowledgementStatus.ACKNOWLEDGED
    assert broker.delivery_state("registry-1")["status"] == "acknowledged"
    assert broker.delivery_state("registry-1")["last_error"] is None


def test_target_return_resets_budget_and_reconciles_live_target() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        await broker.async_target_unavailable("registry-1")
        assert broker.delivery_state("registry-1")["status"] == "waiting_target"
        assert broker.delivery_state("registry-1")["attempts"] == 0
        current[0] = replace(current[0], observed_target=_feedback(18).observed)
        result = await broker.async_submit(
            replace(_intent(), recovery_reassertion=True), now=NOW + timedelta(seconds=60)
        )
        assert result.dispatch_status is DispatchStatus.DISPATCHED
        assert len(service.calls) == 2

    asyncio.run(scenario())


def test_atomic_range_has_its_own_normalized_retry_budget() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        intent = _intent(shape=TargetShape.RANGE, direction=ActuationDirection.RANGED)
        await broker.async_submit(intent, now=NOW)
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
        await broker.async_submit(intent, now=NOW + timedelta(seconds=60))
        assert broker.delivery_state("registry-1")["attempts"] == 2
        await broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=90))
        await broker.async_submit(
            replace(
                intent,
                target_temp_high_ha=24,
                normalized_high_room_c=24,
                expires_at=NOW + timedelta(minutes=4),
            ),
            now=NOW + timedelta(seconds=120),
        )
        assert broker.delivery_state("registry-1")["attempts"] == 1
        assert all(
            "target_temp_low" in call[0] and "target_temp_high" in call[0] for call in service.calls
        )

    asyncio.run(scenario())


def test_rejected_own_write_retries_without_misclassifying_coercion_as_manual() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    current[0] = replace(current[0], observed_target=_feedback(19).observed)
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        rejected = await broker.async_feedback(_feedback(19.5), now=NOW + timedelta(seconds=1))
        assert rejected.acknowledgement_status is AcknowledgementStatus.REJECTED
        assert broker.delivery_state("registry-1")["status"] == "retry_pending"
        current[0] = replace(current[0], observed_target=_feedback(19.5).observed)
        assert (await broker.async_submit(_intent(), now=NOW + timedelta(seconds=60))).reason == (
            "awaiting_acknowledgement"
        )
        assert len(service.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["persist", "dispatch"])
def test_final_live_target_recheck_after_storage_never_overwrites_external_change(
    phase: str,
) -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    current[0] = replace(current[0], observed_target=_feedback(19).observed)
    broker = _broker(service, persistence, current)

    def change() -> None:
        current[0] = replace(current[0], observed_target=_feedback(18).observed)

    async def mark(command: PendingCommand) -> bool:
        persistence.dispatched.append(command)
        change()
        return True

    if phase == "persist":
        persistence.after_persist = change
    else:
        persistence.async_mark_dispatched = mark
    result = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert result.reason == "external_temperature_target"
    assert service.calls == []
    assert broker.delivery_state("registry-1")["attempts"] == 0


def test_dispatch_deadline_uses_last_attempt_not_latest_calculation() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)
    assert broker.next_dispatch_at("registry-1", explicit=True) is None
    asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert broker.next_dispatch_at("registry-1", explicit=True) == NOW + timedelta(seconds=10)
    assert broker.next_dispatch_at("registry-1", explicit=False) == NOW + timedelta(seconds=60)
    asyncio.run(broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30)))
    assert broker.next_dispatch_at("registry-1", explicit=True) == NOW + timedelta(seconds=60)


@pytest.mark.parametrize("error", [False, True])
def test_unavailable_during_service_does_not_resurrect_retry_state(error: bool) -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    service.error = error
    broker = _broker(service, persistence, current)

    async def unavailable() -> None:
        current[0] = replace(current[0], target_ready=False)
        await broker.async_target_unavailable("registry-1")

    service.on_call = unavailable
    result = asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert result.reason == "command_cancelled"
    assert broker.delivery_state("registry-1")["status"] == "waiting_target"
    assert broker.delivery_state("registry-1")["next_retry_at"] is None
    assert broker.state_counts("registry-1") == (0, 0)


def test_automatic_echo_is_bounded_to_dispatched_command_and_cannot_ack_service() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)
    echo = _feedback(20.0, context="delegated-controller")
    assert not broker.expected_automatic_echo(echo, now=NOW)

    def check_not_dispatched() -> None:
        assert not broker.expected_automatic_echo(echo, now=NOW)

    persistence.after_persist = check_not_dispatched

    asyncio.run(broker.async_submit(_intent(), now=NOW))
    assert broker.expected_automatic_echo(echo, now=NOW + timedelta(seconds=10))
    assert broker.state_counts("registry-1") == (1, 0)
    assert not persistence.resolved
    assert not broker.expected_automatic_echo(
        replace(echo, user_initiated=True), now=NOW + timedelta(seconds=10)
    )
    assert not broker.expected_automatic_echo(_feedback(21.0), now=NOW)
    assert not broker.expected_automatic_echo(replace(echo, external_revision=1), now=NOW)
    assert not broker.expected_automatic_echo(echo, now=NOW + timedelta(seconds=30))
    current[0] = replace(current[0], ownership=Ownership.MANUAL_OVERRIDE)
    assert not broker.expected_automatic_echo(echo, now=NOW)
    current[0] = _preflight()

    outcome = asyncio.run(broker.async_feedback(echo, now=NOW + timedelta(seconds=10)))
    assert outcome.reason == "delegated_exact_pending_match"
    assert broker.state_counts("registry-1") == (0, 0)
    duplicate = asyncio.run(broker.async_feedback(echo, now=NOW + timedelta(seconds=11)))
    assert duplicate.reason == "duplicate_command_echo"
    assert len(persistence.resolved) == 1
    assert not broker.expected_automatic_echo(echo, now=NOW + timedelta(seconds=30))
    asyncio.run(broker.async_resume_target("registry-1"))
    assert not broker.expected_automatic_echo(echo, now=NOW + timedelta(seconds=12))


def test_undispatched_feedback_is_ignored_but_late_dispatched_feedback_recovers() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def feedback_before_dispatch(command: PendingCommand) -> bool:
        outcome = await broker.async_feedback(_feedback(20.0), now=NOW)
        assert outcome.reason == "outside_pending_acknowledgement_window"
        assert broker.state_counts("registry-1") == (1, 0)
        return True

    persistence.async_persist_pending = feedback_before_dispatch
    persistence.async_mark_dispatched = feedback_before_dispatch
    asyncio.run(broker.async_submit(_intent(), now=NOW))
    expired = asyncio.run(broker.async_feedback(_feedback(20.0), now=NOW + timedelta(seconds=30)))
    assert expired.reason == "late_target_acknowledged"
    assert broker.state_counts("registry-1") == (0, 0)
    timeout = asyncio.run(
        broker.async_acknowledgement_timeout("registry-1", now=NOW + timedelta(seconds=30))
    )
    assert timeout is None


def test_automatic_range_echo_requires_both_exact_endpoints_and_open_gate() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)
    asyncio.run(broker.async_submit(_intent(shape=TargetShape.RANGE), now=NOW))
    echo = replace(
        _feedback(20.0, context="delegated"),
        observed=TargetFingerprint(TargetShape.RANGE, low_ha=19.0, high_ha=23.0),
    )
    assert broker.expected_automatic_echo(echo, now=NOW)
    assert not broker.expected_automatic_echo(
        replace(echo, observed=TargetFingerprint(TargetShape.RANGE, low_ha=19.0)), now=NOW
    )
    assert not broker.expected_automatic_echo(
        replace(echo, observed=TargetFingerprint(TargetShape.RANGE, low_ha=19.0, high_ha=24.0)),
        now=NOW,
    )
    broker.close_gate()
    assert not broker.expected_automatic_echo(echo, now=NOW)


def test_delegated_ack_of_old_command_drains_only_the_newest_valid_policy() -> None:
    service, persistence, current = FakeService(), FakePersistence(), [_preflight()]
    broker = _broker(service, persistence, current)

    async def scenario() -> None:
        await broker.async_submit(_intent(), now=NOW)
        current[0] = replace(current[0], entry_generation=2, input_generation=2)
        latest = replace(_intent(value=21.0, explicit=True, input_generation=2), entry_generation=2)
        queued = await broker.async_submit(latest, now=NOW + timedelta(seconds=1))
        assert queued.reason == "command_pending"
        assert broker.state_counts("registry-1") == (1, 1)
        echo = replace(_feedback(20.0, context="roommind"), entry_generation=2, input_generation=2)
        acknowledged = await broker.async_feedback(echo, now=NOW + timedelta(seconds=10))
        assert acknowledged.acknowledgement_status is AcknowledgementStatus.INFERRED_ACKNOWLEDGED
        drained = await broker.async_drain_queued("registry-1", now=NOW + timedelta(seconds=11))
        assert drained is not None
        assert drained.dispatch_status is DispatchStatus.DISPATCHED

    asyncio.run(scenario())
    assert [call[0]["temperature"] for call in service.calls] == [20.0, 21.0]


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
            _intent(value=20.5, input_generation=2), now=NOW + timedelta(seconds=60)
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

"""Sole climate-writing command broker with bounded per-target queues."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..core.contracts import AcknowledgementStatus, ActuationDirection, DispatchStatus, TargetShape
from ..core.ownership import (
    FeedbackObservation,
    Ownership,
    PendingAcknowledgement,
    TargetFingerprint,
    classify_acknowledgement,
)

ORDINARY_COMMAND_INTERVAL = timedelta(seconds=60)
HARD_COMMAND_INTERVAL = timedelta(seconds=10)
ACKNOWLEDGEMENT_DEADLINE = timedelta(seconds=30)
SERVICE_CALL_DEADLINE_SECONDS = 15.0
RELEASE_HYSTERESIS_C = 0.1


@dataclass(frozen=True, slots=True)
class ContextToken:
    context_id: str
    native_context: object


@dataclass(frozen=True, slots=True)
class NormalizedIntent:
    target_entity_id: str
    target_registry_identity: str
    shape: TargetShape
    direction: ActuationDirection
    temperature_ha: float | None
    target_temp_low_ha: float | None
    target_temp_high_ha: float | None
    normalized_room_c: float | None
    normalized_low_room_c: float | None
    normalized_high_room_c: float | None
    continuous_bounded_room_c: float | None
    continuous_bounded_low_room_c: float | None
    continuous_bounded_high_room_c: float | None
    step_ha: float
    meaningful_delta_ha: float
    feedback_resolution_ha: float
    entry_generation: int
    input_generation: int
    capability_generation: int
    ownership_revision: int
    external_revision: int
    lease_owner: str
    created_at: datetime
    expires_at: datetime
    explicit_transition: bool = False
    safety_deescalation: bool = False


@dataclass(frozen=True, slots=True)
class BrokerPreflight:
    target_registry_identity: str
    entry_generation: int
    input_generation: int
    capability_generation: int
    ownership_revision: int
    ownership: Ownership
    target_ready: bool
    data_ready: bool
    lease_owner: str | None
    dispatch_gate_open: bool = True


@dataclass(frozen=True, slots=True)
class PendingCommand:
    command_id: str
    intent: NormalizedIntent
    context: ContextToken
    expected_pre_command_target: TargetFingerprint | None
    requested: TargetFingerprint
    registered_at: datetime
    acknowledgement_deadline: datetime
    dispatched: bool = False

    def acknowledgement_contract(self) -> PendingAcknowledgement:
        return PendingAcknowledgement(
            self.command_id,
            self.intent.target_registry_identity,
            self.intent.entry_generation,
            self.intent.input_generation,
            self.intent.capability_generation,
            self.intent.ownership_revision,
            self.intent.external_revision,
            self.context.context_id,
            self.requested,
            self.intent.step_ha,
            self.intent.feedback_resolution_ha,
        )


@dataclass(frozen=True, slots=True)
class AcknowledgedTarget:
    fingerprint: TargetFingerprint
    scalar_room_c: float | None
    low_room_c: float | None
    high_room_c: float | None
    acknowledged_at: datetime


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    command_id: str | None
    dispatch_status: DispatchStatus
    acknowledgement_status: AcknowledgementStatus
    reason: str


@dataclass(slots=True)
class _TargetBrokerState:
    pending: PendingCommand | None = None
    queued: NormalizedIntent | None = None
    acknowledged: AcknowledgedTarget | None = None
    last_dispatch_at: datetime | None = None
    last_feedback_status: AcknowledgementStatus | None = None
    last_feedback_reason: str | None = None


class ClimateTemperatureService(Protocol):
    async def async_set_temperature(
        self, payload: dict[str, object], context: ContextToken
    ) -> None: ...


class CommandPersistence(Protocol):
    async def async_persist_pending(self, command: PendingCommand) -> bool: ...

    async def async_mark_dispatched(self, command: PendingCommand) -> bool: ...

    async def async_resolve(self, command: PendingCommand, reason: str) -> bool: ...


class TargetLeaseRegistry:
    """Domain-wide enabled-target lease with an exact final broker check."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def acquire(self, target_identity: str, owner: str) -> bool:
        existing = self._owners.get(target_identity)
        if existing not in {None, owner}:
            return False
        self._owners[target_identity] = owner
        return True

    def release(self, target_identity: str, owner: str) -> None:
        if self._owners.get(target_identity) == owner:
            del self._owners[target_identity]

    def owner(self, target_identity: str) -> str | None:
        return self._owners.get(target_identity)


def service_payload(intent: NormalizedIntent) -> dict[str, object]:
    """Build the only permitted climate.set_temperature data envelope."""

    if intent.shape is TargetShape.SCALAR:
        if intent.temperature_ha is None:
            raise ValueError("scalar intent requires temperature")
        return {"entity_id": intent.target_entity_id, "temperature": intent.temperature_ha}
    if intent.target_temp_low_ha is None or intent.target_temp_high_ha is None:
        raise ValueError("range intent requires both endpoints")
    return {
        "entity_id": intent.target_entity_id,
        "target_temp_low": intent.target_temp_low_ha,
        "target_temp_high": intent.target_temp_high_ha,
    }


def intent_fingerprint(intent: NormalizedIntent) -> TargetFingerprint:
    return TargetFingerprint(
        intent.shape,
        temperature_ha=intent.temperature_ha,
        low_ha=intent.target_temp_low_ha,
        high_ha=intent.target_temp_high_ha,
    )


def _preflight_reason(
    intent: NormalizedIntent, current: BrokerPreflight, now: datetime
) -> str | None:
    if not current.dispatch_gate_open:
        return "dispatch_gate_closed"
    if now >= intent.expires_at:
        return "intent_expired"
    if current.target_registry_identity != intent.target_registry_identity:
        return "target_identity_changed"
    if current.entry_generation != intent.entry_generation:
        return "entry_generation_changed"
    if current.input_generation != intent.input_generation:
        return "input_generation_changed"
    if current.capability_generation != intent.capability_generation:
        return "capability_generation_changed"
    if current.ownership_revision != intent.ownership_revision:
        return "ownership_revision_changed"
    if current.ownership is not Ownership.OWNED:
        return current.ownership.value
    if not current.target_ready:
        return "target_not_ready"
    if not current.data_ready and not intent.safety_deescalation:
        return "data_not_ready"
    if current.lease_owner != intent.lease_owner:
        return "target_lease_lost"
    return None


def _same_target(a: TargetFingerprint, b: TargetFingerprint) -> bool:
    return a == b


def _maximum_change_ha(a: TargetFingerprint, b: TargetFingerprint) -> float:
    if a.shape is not b.shape:
        return math.inf
    if a.shape is TargetShape.SCALAR:
        if a.temperature_ha is None or b.temperature_ha is None:
            return math.inf
        return abs(a.temperature_ha - b.temperature_ha)
    if None in {a.low_ha, a.high_ha, b.low_ha, b.high_ha}:
        return math.inf
    assert a.low_ha is not None
    assert a.high_ha is not None
    assert b.low_ha is not None
    assert b.high_ha is not None
    return max(abs(a.low_ha - b.low_ha), abs(a.high_ha - b.high_ha))


def _release_hysteresis_blocks(intent: NormalizedIntent, acknowledged: AcknowledgedTarget) -> bool:
    if intent.explicit_transition:
        return False
    if intent.shape is TargetShape.SCALAR:
        if intent.normalized_room_c is None or acknowledged.scalar_room_c is None:
            return False
        if (
            intent.direction is ActuationDirection.HEATING_ONLY
            and intent.normalized_room_c < acknowledged.scalar_room_c
        ):
            return (
                intent.continuous_bounded_room_c is None
                or intent.continuous_bounded_room_c
                > intent.normalized_room_c - RELEASE_HYSTERESIS_C
            )
        if (
            intent.direction is ActuationDirection.COOLING_ONLY
            and intent.normalized_room_c > acknowledged.scalar_room_c
        ):
            return (
                intent.continuous_bounded_room_c is None
                or intent.continuous_bounded_room_c
                < intent.normalized_room_c + RELEASE_HYSTERESIS_C
            )
        return False
    if None in {
        intent.normalized_low_room_c,
        intent.normalized_high_room_c,
        acknowledged.low_room_c,
        acknowledged.high_room_c,
    }:
        return False
    assert intent.normalized_low_room_c is not None
    assert intent.normalized_high_room_c is not None
    assert acknowledged.low_room_c is not None
    assert acknowledged.high_room_c is not None
    lower_releases = intent.normalized_low_room_c < acknowledged.low_room_c
    upper_releases = intent.normalized_high_room_c > acknowledged.high_room_c
    lower_blocked = lower_releases and (
        intent.continuous_bounded_low_room_c is None
        or intent.continuous_bounded_low_room_c
        > intent.normalized_low_room_c - RELEASE_HYSTERESIS_C
    )
    upper_blocked = upper_releases and (
        intent.continuous_bounded_high_room_c is None
        or intent.continuous_bounded_high_room_c
        < intent.normalized_high_room_c + RELEASE_HYSTERESIS_C
    )
    return lower_blocked or upper_blocked


def anti_chatter_reason(
    intent: NormalizedIntent,
    *,
    acknowledged: AcknowledgedTarget | None,
    last_dispatch_at: datetime | None,
    now: datetime,
) -> str | None:
    """Apply unchanged, meaningful, hysteresis, then interval suppression order."""

    requested = intent_fingerprint(intent)
    if acknowledged is not None and _same_target(requested, acknowledged.fingerprint):
        return "target_unchanged"
    if (
        acknowledged is not None
        and _maximum_change_ha(requested, acknowledged.fingerprint) + 1e-12
        < intent.meaningful_delta_ha
    ):
        return "below_minimum_change"
    if acknowledged is not None and _release_hysteresis_blocks(intent, acknowledged):
        return "quantization_hysteresis"
    if last_dispatch_at is not None:
        interval = (
            HARD_COMMAND_INTERVAL if intent.explicit_transition else ORDINARY_COMMAND_INTERVAL
        )
        if now - last_dispatch_at < interval:
            return "command_interval"
    return None


class CommandBroker:
    """The sole production path permitted to invoke climate.set_temperature."""

    def __init__(
        self,
        *,
        service: ClimateTemperatureService,
        persistence: CommandPersistence,
        preflight: Callable[[str], BrokerPreflight],
        command_id_factory: Callable[[], str],
        context_factory: Callable[[], ContextToken],
        service_deadline_seconds: float = SERVICE_CALL_DEADLINE_SECONDS,
    ) -> None:
        self._service = service
        self._persistence = persistence
        self._preflight = preflight
        self._command_id_factory = command_id_factory
        self._context_factory = context_factory
        self._service_deadline_seconds = service_deadline_seconds
        self._states: dict[str, _TargetBrokerState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._gate_open = True

    def state_counts(self, target_identity: str) -> tuple[int, int]:
        state = self._states.get(target_identity)
        if state is None:
            return (0, 0)
        return (int(state.pending is not None), int(state.queued is not None))

    def close_gate(self) -> None:
        self._gate_open = False
        for state in self._states.values():
            state.queued = None

    def invalidate(self, target_identity: str) -> None:
        self._states.setdefault(target_identity, _TargetBrokerState()).queued = None

    async def async_resume_target(self, target_identity: str) -> None:
        """Resolve an uncertain command only on explicit user resume."""

        state = self._states.setdefault(target_identity, _TargetBrokerState())
        pending = state.pending
        state.pending = None
        state.queued = None
        state.last_feedback_status = None
        state.last_feedback_reason = None
        if pending is not None:
            await self._persistence.async_resolve(pending, "explicit_resume")

    @staticmethod
    def _feedback_during_dispatch(
        state: _TargetBrokerState,
    ) -> tuple[AcknowledgementStatus, str] | None:
        if state.pending is not None or state.last_feedback_status is None:
            return None
        return (
            state.last_feedback_status,
            state.last_feedback_reason or "feedback_before_service_return",
        )

    async def async_submit(self, intent: NormalizedIntent, *, now: datetime) -> CommandOutcome:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self._gate_open:
            return CommandOutcome(
                None,
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatch_gate_closed",
            )
        state = self._states.setdefault(intent.target_registry_identity, _TargetBrokerState())
        current = self._preflight(intent.target_registry_identity)
        reason = _preflight_reason(intent, current, now)
        if reason is not None:
            state.queued = None
            return CommandOutcome(
                None, DispatchStatus.NOT_DISPATCHED, AcknowledgementStatus.NOT_APPLICABLE, reason
            )
        if state.pending is not None:
            state.queued = intent
            return CommandOutcome(
                None,
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.PENDING,
                "command_pending",
            )
        chatter = anti_chatter_reason(
            intent,
            acknowledged=state.acknowledged,
            last_dispatch_at=state.last_dispatch_at,
            now=now,
        )
        if chatter is not None:
            if chatter == "command_interval":
                state.queued = intent
            return CommandOutcome(
                None, DispatchStatus.NOT_DISPATCHED, AcknowledgementStatus.NOT_APPLICABLE, chatter
            )
        lock = self._locks.setdefault(intent.target_registry_identity, asyncio.Lock())
        async with lock:
            return await self._async_dispatch(intent, state, now)

    async def _async_dispatch(
        self, intent: NormalizedIntent, state: _TargetBrokerState, now: datetime
    ) -> CommandOutcome:
        context = self._context_factory()
        command = PendingCommand(
            self._command_id_factory(),
            intent,
            context,
            state.acknowledged.fingerprint if state.acknowledged is not None else None,
            intent_fingerprint(intent),
            now,
            now + ACKNOWLEDGEMENT_DEADLINE,
        )
        state.pending = command
        if not await self._persistence.async_persist_pending(command):
            state.pending = None
            return CommandOutcome(
                command.command_id,
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "storage_verification_failed",
            )
        current = self._preflight(intent.target_registry_identity)
        reason = _preflight_reason(intent, current, now)
        if reason is not None or not self._gate_open:
            state.pending = None
            await self._persistence.async_resolve(command, reason or "dispatch_gate_closed")
            return CommandOutcome(
                command.command_id,
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                reason or "dispatch_gate_closed",
            )
        dispatched = PendingCommand(
            command.command_id,
            command.intent,
            command.context,
            command.expected_pre_command_target,
            command.requested,
            command.registered_at,
            command.acknowledgement_deadline,
            True,
        )
        state.pending = dispatched
        if not await self._persistence.async_mark_dispatched(dispatched):
            state.pending = None
            return CommandOutcome(
                dispatched.command_id,
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "storage_verification_failed",
            )
        try:
            await asyncio.wait_for(
                self._service.async_set_temperature(service_payload(intent), context),
                timeout=self._service_deadline_seconds,
            )
        except Exception:
            return CommandOutcome(
                dispatched.command_id,
                DispatchStatus.FAILED,
                AcknowledgementStatus.UNKNOWN,
                "command_outcome_unknown",
            )
        state.last_dispatch_at = now
        completed_feedback = self._feedback_during_dispatch(state)
        if completed_feedback is not None:
            return CommandOutcome(
                dispatched.command_id,
                DispatchStatus.DISPATCHED,
                completed_feedback[0],
                completed_feedback[1],
            )
        return CommandOutcome(
            dispatched.command_id,
            DispatchStatus.DISPATCHED,
            AcknowledgementStatus.PENDING,
            "awaiting_acknowledgement",
        )

    async def async_feedback(
        self, feedback: FeedbackObservation, *, now: datetime
    ) -> CommandOutcome:
        state = self._states.setdefault(feedback.target_identity, _TargetBrokerState())
        pending = state.pending
        decision = classify_acknowledgement(
            pending.acknowledgement_contract() if pending is not None else None, feedback
        )
        if pending is None:
            return CommandOutcome(
                None, DispatchStatus.NOT_DISPATCHED, decision.status, decision.reason
            )
        state.last_feedback_status = decision.status
        state.last_feedback_reason = decision.reason
        if decision.status in {
            AcknowledgementStatus.ACKNOWLEDGED,
            AcknowledgementStatus.INFERRED_ACKNOWLEDGED,
        }:
            state.acknowledged = AcknowledgedTarget(
                feedback.observed,
                pending.intent.normalized_room_c,
                pending.intent.normalized_low_room_c,
                pending.intent.normalized_high_room_c,
                now,
            )
            state.pending = None
            await self._persistence.async_resolve(pending, decision.reason)
        elif decision.status is AcknowledgementStatus.REJECTED or decision.manual_intervention:
            state.pending = None
            state.queued = None
            await self._persistence.async_resolve(pending, decision.reason)
        return CommandOutcome(
            pending.command_id, DispatchStatus.DISPATCHED, decision.status, decision.reason
        )

    async def async_acknowledgement_timeout(
        self, target_identity: str, *, now: datetime
    ) -> CommandOutcome | None:
        state = self._states.get(target_identity)
        if state is None or state.pending is None or now < state.pending.acknowledgement_deadline:
            return None
        pending = state.pending
        state.pending = None
        state.queued = None
        await self._persistence.async_resolve(pending, "command_outcome_unknown")
        return CommandOutcome(
            pending.command_id,
            DispatchStatus.DISPATCHED,
            AcknowledgementStatus.UNKNOWN,
            "command_outcome_unknown",
        )

    async def async_drain_queued(
        self, target_identity: str, *, now: datetime
    ) -> CommandOutcome | None:
        state = self._states.get(target_identity)
        if state is None or state.queued is None or state.pending is not None:
            return None
        queued = state.queued
        state.queued = None
        return await self.async_submit(queued, now=now)

"""Pure per-target ownership transitions and acknowledgement classification."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .contracts import AcknowledgementStatus, TargetShape

DEFAULT_OVERRIDE_DURATION = timedelta(hours=2)
MIN_OVERRIDE_DURATION = timedelta(minutes=15)
MAX_OVERRIDE_DURATION = timedelta(hours=24)


class Ownership(StrEnum):
    DISABLED = "disabled"
    OWNED = "owned"
    MANUAL_OVERRIDE = "manual_override"
    RECONCILING = "reconciling"
    COMMAND_FAULT = "command_fault"


class DataReadiness(StrEnum):
    READY = "ready"
    DEGRADED_READY = "degraded_ready"
    HOLD_LAST_GOOD = "hold_last_good"
    FALLBACK_READY = "fallback_ready"
    INVALID = "invalid"


class TargetReadiness(StrEnum):
    AVAILABLE_SUPPORTED = "available_supported"
    SUSPENDED_MODE = "suspended_mode"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


class OwnershipEvent(StrEnum):
    ENABLE = "enable"
    RECONCILED = "reconciled"
    DISABLE = "disable"
    EXTERNAL_TARGET = "external_target"
    EXTERNAL_HVAC_MODE = "external_hvac_mode"
    EXTERNAL_PRESET = "external_preset"
    OVERRIDE_EXPIRED = "override_expired"
    RESUME = "resume"
    TARGET_UNAVAILABLE = "target_unavailable"
    TARGET_RETURNED = "target_returned"
    TARGET_REPLACED = "target_replaced"
    COMMAND_REJECTED = "command_rejected"
    COMMAND_UNKNOWN = "command_unknown"
    LATE_ACKNOWLEDGED = "late_acknowledged"
    CLEAN_RESTART = "clean_restart"
    UNCLEAN_RESTART = "unclean_restart"


@dataclass(frozen=True, slots=True)
class OwnershipState:
    target_identity: str
    ownership: Ownership = Ownership.DISABLED
    data_readiness: DataReadiness = DataReadiness.INVALID
    target_readiness: TargetReadiness = TargetReadiness.UNAVAILABLE
    revision: int = 0
    external_revision: int = 0
    override_reason: str | None = None
    override_expiry: datetime | None = None
    resume_required: bool = False


@dataclass(frozen=True, slots=True)
class OwnershipTransition:
    state: OwnershipState
    invalidate_intents: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TargetFingerprint:
    shape: TargetShape
    temperature_ha: float | None = None
    low_ha: float | None = None
    high_ha: float | None = None


@dataclass(frozen=True, slots=True)
class PendingAcknowledgement:
    command_id: str
    target_identity: str
    entry_generation: int
    input_generation: int
    capability_generation: int
    ownership_revision: int
    external_revision: int
    context_id: str
    requested: TargetFingerprint
    step_ha: float
    feedback_resolution_ha: float


@dataclass(frozen=True, slots=True)
class FeedbackObservation:
    target_identity: str
    observed: TargetFingerprint
    context_id: str | None
    parent_context_id: str | None
    entry_generation: int
    input_generation: int
    capability_generation: int
    ownership_revision: int
    external_revision: int


@dataclass(frozen=True, slots=True)
class AcknowledgementDecision:
    status: AcknowledgementStatus
    reason: str
    manual_intervention: bool = False


def initial_ownership(target_identity: str) -> OwnershipState:
    if not target_identity:
        raise ValueError("target identity is required")
    return OwnershipState(target_identity)


def _aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def reduce_ownership(
    state: OwnershipState,
    event: OwnershipEvent,
    *,
    now: datetime,
    target_readiness: TargetReadiness | None = None,
    data_readiness: DataReadiness | None = None,
    preset_affects_temperature_target: bool = False,
    override_duration: timedelta | None = DEFAULT_OVERRIDE_DURATION,
    acknowledged_external_revision: int | None = None,
) -> OwnershipTransition:
    """Apply one authoritative event without conflating readiness and ownership."""

    _aware(now)
    readiness = target_readiness or state.target_readiness
    data = data_readiness or state.data_readiness
    revision = state.revision + 1
    external_revision = state.external_revision
    if event is OwnershipEvent.ENABLE:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.RECONCILING,
                target_readiness=readiness,
                data_readiness=data,
                revision=revision,
                override_reason=None,
                override_expiry=None,
                resume_required=False,
            ),
            True,
            "control_enabled",
        )
    if event is OwnershipEvent.RECONCILED:
        if state.ownership is not Ownership.RECONCILING or state.resume_required:
            return OwnershipTransition(state, False, "reconciliation_not_permitted")
        if readiness is not TargetReadiness.AVAILABLE_SUPPORTED:
            return OwnershipTransition(
                replace(state, target_readiness=readiness, data_readiness=data, revision=revision),
                True,
                "target_not_ready",
            )
        if data not in {
            DataReadiness.READY,
            DataReadiness.DEGRADED_READY,
            DataReadiness.FALLBACK_READY,
        }:
            return OwnershipTransition(
                replace(state, target_readiness=readiness, data_readiness=data, revision=revision),
                True,
                "data_not_ready",
            )
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.OWNED,
                target_readiness=readiness,
                data_readiness=data,
                revision=revision,
            ),
            True,
            "reconciled",
        )
    if event is OwnershipEvent.DISABLE:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.DISABLED,
                revision=revision,
                override_reason=None,
                override_expiry=None,
                resume_required=False,
            ),
            True,
            "control_disabled",
        )
    if event is OwnershipEvent.EXTERNAL_TARGET or (
        event is OwnershipEvent.EXTERNAL_PRESET and preset_affects_temperature_target
    ):
        if override_duration is not None and not (
            MIN_OVERRIDE_DURATION <= override_duration <= MAX_OVERRIDE_DURATION
        ):
            raise ValueError("override duration must be 15 minutes through 24 hours")
        external_revision += 1
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.MANUAL_OVERRIDE,
                revision=revision,
                external_revision=external_revision,
                override_reason=(
                    "external_temperature_target"
                    if event is OwnershipEvent.EXTERNAL_TARGET
                    else "external_preset_target"
                ),
                override_expiry=(
                    now + override_duration if override_duration is not None else None
                ),
                resume_required=False,
            ),
            True,
            "manual_override",
        )
    if event is OwnershipEvent.EXTERNAL_PRESET:
        return OwnershipTransition(state, False, "preset_state_only")
    if event is OwnershipEvent.EXTERNAL_HVAC_MODE:
        next_ownership = state.ownership
        if state.ownership in {Ownership.OWNED, Ownership.RECONCILING} and (
            readiness is TargetReadiness.AVAILABLE_SUPPORTED
        ):
            next_ownership = Ownership.RECONCILING
        return OwnershipTransition(
            replace(
                state,
                ownership=next_ownership,
                target_readiness=readiness,
                revision=revision,
            ),
            True,
            "hvac_mode_reassessed",
        )
    if event is OwnershipEvent.OVERRIDE_EXPIRED:
        if state.ownership is not Ownership.MANUAL_OVERRIDE:
            return OwnershipTransition(state, False, "no_override")
        if state.override_expiry is None or now < state.override_expiry:
            return OwnershipTransition(state, False, "override_active")
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.RECONCILING,
                revision=revision,
                override_reason=None,
                override_expiry=None,
            ),
            True,
            "override_expired",
        )
    if event is OwnershipEvent.RESUME:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.RECONCILING,
                revision=revision,
                override_reason=None,
                override_expiry=None,
                resume_required=False,
            ),
            True,
            "resume_requested",
        )
    if event is OwnershipEvent.TARGET_UNAVAILABLE:
        return OwnershipTransition(
            replace(state, target_readiness=TargetReadiness.UNAVAILABLE, revision=revision),
            True,
            "target_unavailable",
        )
    if event is OwnershipEvent.TARGET_RETURNED:
        next_ownership = state.ownership
        if state.ownership is Ownership.OWNED:
            next_ownership = Ownership.RECONCILING
        return OwnershipTransition(
            replace(
                state,
                ownership=next_ownership,
                target_readiness=readiness,
                revision=revision,
            ),
            True,
            "target_returned",
        )
    if event is OwnershipEvent.TARGET_REPLACED:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.DISABLED,
                target_readiness=TargetReadiness.UNAVAILABLE,
                revision=revision,
                override_reason="target_replaced",
                override_expiry=None,
                resume_required=True,
            ),
            True,
            "target_replaced",
        )
    if event in {OwnershipEvent.COMMAND_REJECTED, OwnershipEvent.COMMAND_UNKNOWN}:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.COMMAND_FAULT,
                revision=revision,
                override_reason=(
                    "coerced_or_rejected"
                    if event is OwnershipEvent.COMMAND_REJECTED
                    else "command_outcome_unknown"
                ),
                resume_required=True,
            ),
            True,
            "command_fault",
        )
    if event is OwnershipEvent.LATE_ACKNOWLEDGED:
        if (
            state.ownership is not Ownership.COMMAND_FAULT
            or acknowledged_external_revision is None
            or state.external_revision != acknowledged_external_revision
        ):
            return OwnershipTransition(state, False, "late_acknowledgement_obsolete")
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.RECONCILING,
                revision=revision,
                override_reason=None,
                resume_required=False,
            ),
            True,
            "late_acknowledgement_accepted",
        )
    if event in {OwnershipEvent.CLEAN_RESTART, OwnershipEvent.UNCLEAN_RESTART}:
        return OwnershipTransition(
            replace(
                state,
                ownership=Ownership.RECONCILING,
                revision=revision,
                resume_required=event is OwnershipEvent.UNCLEAN_RESTART,
                override_reason=(
                    "unclean_restart" if event is OwnershipEvent.UNCLEAN_RESTART else None
                ),
            ),
            True,
            "restart_reconciliation",
        )
    raise AssertionError(f"unhandled ownership event: {event}")


def _matches(a: TargetFingerprint, b: TargetFingerprint, tolerance: float) -> bool:
    if a.shape is not b.shape:
        return False
    if a.shape is TargetShape.SCALAR:
        return (
            a.temperature_ha is not None
            and b.temperature_ha is not None
            and math.isclose(a.temperature_ha, b.temperature_ha, rel_tol=0.0, abs_tol=tolerance)
        )
    return (
        a.low_ha is not None
        and b.low_ha is not None
        and a.high_ha is not None
        and b.high_ha is not None
        and math.isclose(a.low_ha, b.low_ha, rel_tol=0.0, abs_tol=tolerance)
        and math.isclose(a.high_ha, b.high_ha, rel_tol=0.0, abs_tol=tolerance)
    )


def classify_acknowledgement(
    pending: PendingAcknowledgement | None,
    feedback: FeedbackObservation,
) -> AcknowledgementDecision:
    """Classify attributable, inferred, contradictory, and obsolete feedback."""

    if pending is None:
        return AcknowledgementDecision(
            AcknowledgementStatus.NOT_APPLICABLE, "external_temperature_target", True
        )
    identity_matches = pending.target_identity == feedback.target_identity
    generation_matches = (
        pending.entry_generation == feedback.entry_generation
        and pending.input_generation == feedback.input_generation
        and pending.capability_generation == feedback.capability_generation
        and pending.ownership_revision == feedback.ownership_revision
    )
    if not identity_matches or not generation_matches:
        return AcknowledgementDecision(AcknowledgementStatus.NOT_APPLICABLE, "obsolete_feedback")
    tolerance = min(pending.step_ha / 4.0, pending.feedback_resolution_ha)
    if not math.isfinite(tolerance) or tolerance < 0.0:
        return AcknowledgementDecision(AcknowledgementStatus.REJECTED, "invalid_feedback_tolerance")
    matches = _matches(pending.requested, feedback.observed, tolerance)
    own_context = pending.context_id in {feedback.context_id, feedback.parent_context_id}
    if own_context:
        return (
            AcknowledgementDecision(AcknowledgementStatus.ACKNOWLEDGED, "own_context_match")
            if matches
            else AcknowledgementDecision(AcknowledgementStatus.REJECTED, "coerced_or_rejected")
        )
    contextless = feedback.context_id is None and feedback.parent_context_id is None
    no_external_change = pending.external_revision == feedback.external_revision
    if contextless and matches and no_external_change:
        return AcknowledgementDecision(
            AcknowledgementStatus.INFERRED_ACKNOWLEDGED, "contextless_exact_pending_match"
        )
    return AcknowledgementDecision(
        AcknowledgementStatus.NOT_APPLICABLE, "external_temperature_target", True
    )

"""Pure ownership transition and acknowledgement tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.athb.core.contracts import AcknowledgementStatus, TargetShape
from custom_components.athb.core.ownership import (
    AcknowledgementDecision,
    DataReadiness,
    FeedbackObservation,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    PendingAcknowledgement,
    TargetFingerprint,
    TargetReadiness,
    classify_acknowledgement,
    initial_ownership,
    reduce_ownership,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _owned() -> OwnershipState:
    state = initial_ownership("registry-1")
    enabled = reduce_ownership(
        state,
        OwnershipEvent.ENABLE,
        now=NOW,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        data_readiness=DataReadiness.READY,
    ).state
    return reduce_ownership(
        enabled,
        OwnershipEvent.RECONCILED,
        now=NOW,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        data_readiness=DataReadiness.READY,
    ).state


def test_enable_requires_reconciliation_and_ready_inputs() -> None:
    state = initial_ownership("registry-1")
    assert state.ownership is Ownership.DISABLED
    enabled = reduce_ownership(state, OwnershipEvent.ENABLE, now=NOW).state
    assert enabled.ownership is Ownership.RECONCILING
    blocked = reduce_ownership(enabled, OwnershipEvent.RECONCILED, now=NOW)
    assert blocked.state.ownership is Ownership.RECONCILING
    assert blocked.reason == "target_not_ready"


def test_external_temperature_target_always_creates_manual_override() -> None:
    owned = _owned()
    result = reduce_ownership(owned, OwnershipEvent.EXTERNAL_TARGET, now=NOW)
    assert result.state.ownership is Ownership.MANUAL_OVERRIDE
    assert result.state.override_expiry == NOW + timedelta(hours=2)
    assert result.state.external_revision == owned.external_revision + 1
    assert result.invalidate_intents


def test_external_hvac_mode_never_creates_manual_override() -> None:
    owned = _owned()
    off = reduce_ownership(
        owned,
        OwnershipEvent.EXTERNAL_HVAC_MODE,
        now=NOW,
        target_readiness=TargetReadiness.SUSPENDED_MODE,
    )
    assert off.state.ownership is Ownership.OWNED
    assert off.state.target_readiness is TargetReadiness.SUSPENDED_MODE
    assert off.state.external_revision == owned.external_revision
    assert off.invalidate_intents
    heat = reduce_ownership(
        off.state,
        OwnershipEvent.EXTERNAL_HVAC_MODE,
        now=NOW,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
    )
    assert heat.state.ownership is Ownership.RECONCILING


def test_mode_change_does_not_clear_existing_manual_override() -> None:
    manual = reduce_ownership(_owned(), OwnershipEvent.EXTERNAL_TARGET, now=NOW).state
    changed = reduce_ownership(
        manual,
        OwnershipEvent.EXTERNAL_HVAC_MODE,
        now=NOW,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
    ).state
    assert changed.ownership is Ownership.MANUAL_OVERRIDE
    assert changed.override_expiry == manual.override_expiry


def test_preset_is_classified_by_actual_temperature_effect() -> None:
    owned = _owned()
    harmless = reduce_ownership(owned, OwnershipEvent.EXTERNAL_PRESET, now=NOW)
    assert harmless.state.ownership is Ownership.OWNED
    assert harmless.state.revision == owned.revision
    assert not harmless.invalidate_intents
    affecting = reduce_ownership(
        owned,
        OwnershipEvent.EXTERNAL_PRESET,
        now=NOW,
        preset_affects_temperature_target=True,
    )
    assert affecting.state.ownership is Ownership.MANUAL_OVERRIDE


def test_disable_target_replacement_and_unavailability_are_fail_closed() -> None:
    owned = _owned()
    unavailable = reduce_ownership(owned, OwnershipEvent.TARGET_UNAVAILABLE, now=NOW)
    assert unavailable.state.ownership is Ownership.OWNED
    assert unavailable.state.target_readiness is TargetReadiness.UNAVAILABLE
    returned = reduce_ownership(
        unavailable.state,
        OwnershipEvent.TARGET_RETURNED,
        now=NOW,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
    )
    assert returned.state.ownership is Ownership.RECONCILING
    replaced = reduce_ownership(returned.state, OwnershipEvent.TARGET_REPLACED, now=NOW)
    assert replaced.state.ownership is Ownership.DISABLED
    assert replaced.state.resume_required


def test_override_expiry_and_resume_reconcile_without_bypassing_validation() -> None:
    manual = reduce_ownership(_owned(), OwnershipEvent.EXTERNAL_TARGET, now=NOW).state
    assert (
        reduce_ownership(
            manual, OwnershipEvent.OVERRIDE_EXPIRED, now=NOW + timedelta(minutes=119)
        ).reason
        == "override_active"
    )
    expired = reduce_ownership(
        manual, OwnershipEvent.OVERRIDE_EXPIRED, now=NOW + timedelta(hours=2)
    ).state
    assert expired.ownership is Ownership.RECONCILING
    assert (
        reduce_ownership(
            expired,
            OwnershipEvent.RECONCILED,
            now=NOW + timedelta(hours=2),
            data_readiness=DataReadiness.INVALID,
        ).state.ownership
        is Ownership.RECONCILING
    )
    resumed = reduce_ownership(manual, OwnershipEvent.RESUME, now=NOW).state
    assert resumed.ownership is Ownership.RECONCILING


def test_until_resumed_and_override_duration_validation() -> None:
    result = reduce_ownership(
        _owned(), OwnershipEvent.EXTERNAL_TARGET, now=NOW, override_duration=None
    )
    assert result.state.override_expiry is None
    with pytest.raises(ValueError, match="override duration"):
        reduce_ownership(
            _owned(),
            OwnershipEvent.EXTERNAL_TARGET,
            now=NOW,
            override_duration=timedelta(minutes=14),
        )


def test_command_fault_and_restart_require_explicit_reconciliation() -> None:
    fault = reduce_ownership(_owned(), OwnershipEvent.COMMAND_UNKNOWN, now=NOW).state
    assert fault.ownership is Ownership.COMMAND_FAULT
    assert fault.resume_required
    unclean = reduce_ownership(fault, OwnershipEvent.UNCLEAN_RESTART, now=NOW).state
    assert unclean.ownership is Ownership.RECONCILING
    assert unclean.resume_required
    clean = reduce_ownership(_owned(), OwnershipEvent.CLEAN_RESTART, now=NOW).state
    assert clean.ownership is Ownership.RECONCILING
    assert not clean.resume_required


def test_late_acknowledgement_requires_matching_external_revision() -> None:
    fault = reduce_ownership(_owned(), OwnershipEvent.COMMAND_UNKNOWN, now=NOW).state
    obsolete = reduce_ownership(
        fault,
        OwnershipEvent.LATE_ACKNOWLEDGED,
        now=NOW,
        acknowledged_external_revision=fault.external_revision + 1,
    )
    assert obsolete.reason == "late_acknowledgement_obsolete"
    accepted = reduce_ownership(
        fault,
        OwnershipEvent.LATE_ACKNOWLEDGED,
        now=NOW,
        acknowledged_external_revision=fault.external_revision,
    )
    assert accepted.state.ownership is Ownership.RECONCILING


def _pending(*, shape: TargetShape = TargetShape.SCALAR) -> PendingAcknowledgement:
    fingerprint = (
        TargetFingerprint(shape, temperature_ha=20.0)
        if shape is TargetShape.SCALAR
        else TargetFingerprint(shape, low_ha=19.0, high_ha=23.0)
    )
    return PendingAcknowledgement(
        "cmd-1", "registry-1", 1, 2, 3, 4, 5, "ctx-1", fingerprint, 0.5, 0.2
    )


def _feedback(
    observed: TargetFingerprint,
    *,
    context: str | None = "ctx-1",
    parent: str | None = None,
    identity: str = "registry-1",
    input_generation: int = 2,
    external_revision: int = 5,
) -> FeedbackObservation:
    return FeedbackObservation(
        identity, observed, context, parent, 1, input_generation, 3, 4, external_revision
    )


def test_own_context_and_parent_context_acknowledge_exact_pending() -> None:
    pending = _pending()
    observed = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.1)
    assert (
        classify_acknowledgement(pending, _feedback(observed)).status
        is AcknowledgementStatus.ACKNOWLEDGED
    )
    parent = _feedback(observed, context="child", parent="ctx-1")
    assert classify_acknowledgement(pending, parent).status is AcknowledgementStatus.ACKNOWLEDGED


def test_stricter_quarter_step_tolerance_and_coercion_rejection() -> None:
    pending = _pending()
    accepted = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.125)
    rejected = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.126)
    assert (
        classify_acknowledgement(pending, _feedback(accepted)).status
        is AcknowledgementStatus.ACKNOWLEDGED
    )
    assert classify_acknowledgement(pending, _feedback(rejected)) == AcknowledgementDecision(
        AcknowledgementStatus.REJECTED, "coerced_or_rejected"
    )


def test_contextless_exact_feedback_is_inferred_only_without_external_revision() -> None:
    pending = _pending(shape=TargetShape.RANGE)
    observed = TargetFingerprint(TargetShape.RANGE, low_ha=19.0, high_ha=23.0)
    inferred = classify_acknowledgement(pending, _feedback(observed, context=None))
    assert inferred.status is AcknowledgementStatus.INFERRED_ACKNOWLEDGED
    changed = classify_acknowledgement(
        pending, _feedback(observed, context=None, external_revision=6)
    )
    assert changed.manual_intervention


def test_external_same_value_still_expresses_external_ownership() -> None:
    pending = _pending()
    same = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0)
    decision = classify_acknowledgement(pending, _feedback(same, context="external"))
    assert decision.reason == "external_temperature_target"
    assert decision.manual_intervention


def test_obsolete_identity_or_generation_cannot_restore_ownership() -> None:
    pending = _pending()
    value = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0)
    assert (
        classify_acknowledgement(pending, _feedback(value, identity="replacement")).reason
        == "obsolete_feedback"
    )
    assert (
        classify_acknowledgement(pending, _feedback(value, input_generation=99)).reason
        == "obsolete_feedback"
    )


def test_feedback_without_pending_is_external() -> None:
    value = TargetFingerprint(TargetShape.SCALAR, temperature_ha=20.0)
    decision = classify_acknowledgement(None, _feedback(value, context=None))
    assert decision.manual_intervention
    assert decision.status is AcknowledgementStatus.NOT_APPLICABLE

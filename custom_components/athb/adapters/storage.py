"""Versioned and verified control-state persistence boundary."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol

CONTROL_STORAGE_VERSION = 1


@dataclass(frozen=True, slots=True)
class StoredCommand:
    command_id: str
    target_identity: str
    payload_fingerprint: str
    context_id: str
    entry_generation: int
    input_generation: int
    capability_generation: int
    ownership_revision: int
    created_at: str
    expires_at: str
    dispatched: bool


@dataclass(frozen=True, slots=True)
class StoredActuator:
    target_identity: str
    ownership: str
    ownership_revision: int
    external_revision: int
    override_reason: str | None
    override_expiry: str | None
    resume_required: bool
    last_observed_target_fingerprint: str | None
    last_command_id: str | None
    last_command_payload_fingerprint: str | None
    last_command_context_id: str | None
    pending_command: StoredCommand | None


@dataclass(frozen=True, slots=True)
class ControlStoreState:
    storage_generation: int
    run_id: str
    clean_shutdown: bool
    configuration_fingerprint: str
    strategy: str
    actuators: tuple[StoredActuator, ...]
    schema_version: int = CONTROL_STORAGE_VERSION


@dataclass(frozen=True, slots=True)
class ControlLoadResult:
    state: ControlStoreState | None
    reason: str | None
    corrupt_raw: str | None = None


@dataclass(frozen=True, slots=True)
class ControlWriteResult:
    verified: bool
    reason: str


@dataclass(frozen=True, slots=True)
class StartupRecovery:
    state: ControlStoreState
    requires_resume: bool
    reason: str


class ControlStorageBackend(Protocol):
    async def async_save(self, serialized: str) -> None: ...

    async def async_readback(self) -> str | None: ...


def _command_from_dict(value: object) -> StoredCommand | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending_command must be an object")
    command = StoredCommand(**value)
    integers = (
        command.entry_generation,
        command.input_generation,
        command.capability_generation,
        command.ownership_revision,
    )
    if (
        any(
            not item
            for item in (
                command.command_id,
                command.target_identity,
                command.payload_fingerprint,
                command.context_id,
            )
        )
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in integers)
        or not isinstance(command.dispatched, bool)
    ):
        raise ValueError("invalid pending command")
    created = datetime.fromisoformat(command.created_at)
    expires = datetime.fromisoformat(command.expires_at)
    if (
        created.tzinfo is None
        or created.utcoffset() is None
        or expires.tzinfo is None
        or expires.utcoffset() is None
        or expires <= created
    ):
        raise ValueError("invalid pending command timestamps")
    return command


def _actuator_from_dict(value: object) -> StoredActuator:
    if not isinstance(value, dict):
        raise ValueError("actuator must be an object")
    item = dict(value)
    item["pending_command"] = _command_from_dict(item.get("pending_command"))
    actuator = StoredActuator(**item)
    if (
        not actuator.target_identity
        or not actuator.ownership
        or isinstance(actuator.ownership_revision, bool)
        or not isinstance(actuator.ownership_revision, int)
        or actuator.ownership_revision < 0
        or isinstance(actuator.external_revision, bool)
        or not isinstance(actuator.external_revision, int)
        or actuator.external_revision < 0
        or not isinstance(actuator.resume_required, bool)
        or (
            actuator.pending_command is not None
            and actuator.pending_command.target_identity != actuator.target_identity
        )
    ):
        raise ValueError("invalid stored actuator")
    if actuator.override_expiry is not None:
        expiry = datetime.fromisoformat(actuator.override_expiry)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("invalid override expiry")
    return actuator


def serialize_control_state(state: ControlStoreState) -> str:
    """Serialize canonical state without executable listener/task objects."""

    if (
        state.schema_version != CONTROL_STORAGE_VERSION
        or isinstance(state.storage_generation, bool)
        or not isinstance(state.storage_generation, int)
        or state.storage_generation < 0
        or not isinstance(state.clean_shutdown, bool)
    ):
        raise ValueError("invalid control state version or generation")
    if not state.run_id or not state.configuration_fingerprint or not state.strategy:
        raise ValueError("control state identity fields are required")
    identities = [actuator.target_identity for actuator in state.actuators]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate actuator identity")
    return json.dumps(asdict(state), sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_control_state(serialized: str | None) -> ControlLoadResult:
    """Load one exact schema, preserving corrupt raw data for repair diagnostics."""

    if serialized is None:
        return ControlLoadResult(None, "storage_missing")
    try:
        raw = json.loads(serialized)
        if not isinstance(raw, dict):
            raise ValueError("control store root must be an object")
        if raw.get("schema_version") != CONTROL_STORAGE_VERSION:
            return ControlLoadResult(None, "unsupported_storage_version", serialized)
        actuators_raw = raw.get("actuators")
        if not isinstance(actuators_raw, list):
            raise ValueError("actuators must be a list")
        state = ControlStoreState(
            storage_generation=raw["storage_generation"],
            run_id=raw["run_id"],
            clean_shutdown=raw["clean_shutdown"],
            configuration_fingerprint=raw["configuration_fingerprint"],
            strategy=raw["strategy"],
            actuators=tuple(_actuator_from_dict(item) for item in actuators_raw),
            schema_version=raw["schema_version"],
        )
        serialize_control_state(state)
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        return ControlLoadResult(None, "corrupt_control_storage", serialized)
    return ControlLoadResult(state, None)


class VerifiedControlStore:
    """Await save, read back, and verify exact generation and payload identity."""

    def __init__(self, backend: ControlStorageBackend) -> None:
        self._backend = backend

    async def async_write_critical(self, state: ControlStoreState) -> ControlWriteResult:
        expected = serialize_control_state(state)
        try:
            await self._backend.async_save(expected)
            actual_raw = await self._backend.async_readback()
        except Exception:
            return ControlWriteResult(False, "storage_io_failed")
        actual = load_control_state(actual_raw)
        if actual.state is None:
            return ControlWriteResult(False, actual.reason or "storage_verification_failed")
        if actual.state.storage_generation != state.storage_generation:
            return ControlWriteResult(False, "storage_generation_mismatch")
        try:
            actual_serialized = serialize_control_state(actual.state)
        except ValueError:
            return ControlWriteResult(False, "storage_verification_failed")
        if actual_serialized != expected:
            return ControlWriteResult(False, "storage_payload_mismatch")
        return ControlWriteResult(True, "storage_verified")


def prepare_startup_recovery(
    loaded: ControlLoadResult,
    *,
    run_id: str,
    configuration_fingerprint: str,
    strategy: str,
) -> StartupRecovery:
    """Create an unclean current-run record and never replay a stored command."""

    if not run_id:
        raise ValueError("run_id is required")
    if loaded.state is None:
        state = ControlStoreState(1, run_id, False, configuration_fingerprint, strategy, ())
        return StartupRecovery(
            state, loaded.reason == "corrupt_control_storage", loaded.reason or "new_store"
        )
    prior = loaded.state
    unresolved = any(actuator.pending_command is not None for actuator in prior.actuators)
    configuration_changed = prior.configuration_fingerprint != configuration_fingerprint
    strategy_changed = prior.strategy != strategy
    requires_resume = (
        not prior.clean_shutdown or unresolved or configuration_changed or strategy_changed
    )
    reason = (
        "unresolved_command"
        if unresolved
        else "unclean_shutdown"
        if not prior.clean_shutdown
        else "configuration_changed"
        if configuration_changed or strategy_changed
        else "clean_restart"
    )
    actuators = tuple(
        replace(
            actuator,
            ownership="reconciling",
            resume_required=requires_resume,
            pending_command=None,
        )
        for actuator in prior.actuators
    )
    state = ControlStoreState(
        prior.storage_generation + 1,
        run_id,
        False,
        configuration_fingerprint,
        strategy,
        actuators,
    )
    return StartupRecovery(state, requires_resume, reason)


def mark_clean_shutdown(state: ControlStoreState, *, now: datetime) -> ControlStoreState:
    """Mark clean only as an explicit, timezone-aware controlled-unload action."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if any(actuator.pending_command is not None for actuator in state.actuators):
        raise ValueError("cannot mark clean with unresolved commands")
    return replace(state, storage_generation=state.storage_generation + 1, clean_shutdown=True)

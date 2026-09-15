"""Versioned and verified control-state persistence boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .broker import PendingCommand, service_payload

CONTROL_STORAGE_VERSION = 1
LAST_VALID_OUTPUT_KEYS = (
    "thermal_sensation",
    "lower_comfort_boundary",
    "comfort_range_current",
    "comfort_range_neutral_delta",
    "heating_control_target",
    "thermal_neutral",
    "cooling_control_target",
    "upper_comfort_boundary",
    "root_sensation_votes",
    "comfort_status",
    "surface_temperature",
    "surface_relative_humidity",
    "surface_high_humidity",
    "surface_saturation",
    "effective_targets",
    "effective_target_details",
    "target_scenarios",
)


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
    control_enabled_intent: bool = False
    selected_profile: str = "comfort"
    previous_non_boost_profile: str = "comfort"
    boost_expiry_utc: str | None = None
    boost_mode: str = "off"
    rapid_boost_reached: bool = False
    last_valid_values_json: str | None = None
    last_valid_at: str | None = None


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
    reasons: tuple[str, ...] = ()


class ControlStorageBackend(Protocol):
    async def async_save(self, serialized: str) -> None: ...

    async def async_readback(self) -> str | None: ...


class HomeAssistantControlStorageBackend:
    """HA Store boundary; Store performs disk work outside the event loop."""

    def __init__(self, hass: HomeAssistant, zone_uuid: str) -> None:
        self._store: Store[str] = Store(
            hass,
            CONTROL_STORAGE_VERSION,
            f"athb.control.{zone_uuid}",
            atomic_writes=True,
        )

    async def async_save(self, serialized: str) -> None:
        await self._store.async_save(serialized)

    async def async_readback(self) -> str | None:
        return await self._store.async_load()

    async def async_remove(self) -> None:
        await self._store.async_remove()


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
        or not isinstance(state.control_enabled_intent, bool)
        or not state.selected_profile
        or not state.previous_non_boost_profile
        or state.boost_mode not in {"off", "adaptive", "rapid"}
        or not isinstance(state.rapid_boost_reached, bool)
    ):
        raise ValueError("invalid control state version or generation")
    if state.boost_expiry_utc is not None:
        boost_expiry = datetime.fromisoformat(state.boost_expiry_utc)
        if boost_expiry.tzinfo is None or boost_expiry.utcoffset() is None:
            raise ValueError("invalid boost expiry")
    _validate_last_valid_output(state.last_valid_values_json, state.last_valid_at)
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
            control_enabled_intent=raw.get("control_enabled_intent", False),
            selected_profile=raw.get("selected_profile", "comfort"),
            previous_non_boost_profile=raw.get("previous_non_boost_profile", "comfort"),
            boost_expiry_utc=raw.get("boost_expiry_utc"),
            boost_mode=raw.get(
                "boost_mode",
                "adaptive" if raw.get("selected_profile") == "boost" else "off",
            ),
            rapid_boost_reached=raw.get("rapid_boost_reached", False),
            last_valid_values_json=raw.get("last_valid_values_json"),
            last_valid_at=raw.get("last_valid_at"),
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


class ZoneCommandPersistence:
    """Versioned per-zone command journal used by the production broker."""

    def __init__(
        self,
        backend: ControlStorageBackend,
        *,
        run_id: str,
        configuration_fingerprint: str,
        strategy: str,
        target_identities: tuple[str, ...],
        control_enabled: bool = False,
        boost_mode: str = "off",
    ) -> None:
        self._backend = backend
        self._verified = VerifiedControlStore(backend)
        self._run_id = run_id
        self._configuration_fingerprint = configuration_fingerprint
        self._strategy = strategy
        self._target_identities = target_identities
        self._control_enabled = control_enabled
        self._boost_mode = boost_mode
        self._lock = asyncio.Lock()
        self.state: ControlStoreState | None = None
        self.requires_resume = False
        self.startup_reason = "not_started"
        self.startup_reasons: tuple[str, ...] = ("not_started",)

    async def async_start(self) -> bool:
        async with self._lock:
            raw = await self._backend.async_readback()
            recovery = prepare_startup_recovery(
                load_control_state(raw),
                run_id=self._run_id,
                configuration_fingerprint=self._configuration_fingerprint,
                strategy=self._strategy,
            )
            known = {item.target_identity: item for item in recovery.state.actuators}
            actuators = tuple(
                known.get(
                    identity,
                    StoredActuator(
                        identity,
                        "reconciling",
                        0,
                        0,
                        None,
                        None,
                        recovery.requires_resume,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )
                for identity in self._target_identities
            )
            self.state = replace(
                recovery.state,
                actuators=actuators,
                control_enabled_intent=self._control_enabled,
                selected_profile="comfort",
                previous_non_boost_profile="comfort",
                boost_mode=self._boost_mode,
            )
            self.requires_resume = recovery.requires_resume
            self.startup_reason = recovery.reason
            self.startup_reasons = recovery.reasons
            return (await self._verified.async_write_critical(self.state)).verified

    def _replace_actuator(self, identity: str, actuator: StoredActuator) -> None:
        if self.state is None:
            raise RuntimeError("control persistence has not started")
        self.state = replace(
            self.state,
            storage_generation=self.state.storage_generation + 1,
            actuators=tuple(
                actuator if item.target_identity == identity else item
                for item in self.state.actuators
            ),
        )

    @staticmethod
    def _stored_command(command: PendingCommand, *, dispatched: bool) -> StoredCommand:
        payload = json.dumps(service_payload(command.intent), sort_keys=True, separators=(",", ":"))
        return StoredCommand(
            command.command_id,
            command.intent.target_registry_identity,
            sha256(payload.encode()).hexdigest(),
            command.context.context_id,
            command.intent.entry_generation,
            command.intent.input_generation,
            command.intent.capability_generation,
            command.intent.ownership_revision,
            command.registered_at.isoformat(),
            command.intent.expires_at.isoformat(),
            dispatched,
        )

    async def async_persist_pending(self, command: PendingCommand) -> bool:
        return await self._write_command(command, dispatched=False, reason=None)

    async def async_mark_dispatched(self, command: PendingCommand) -> bool:
        return await self._write_command(command, dispatched=True, reason=None)

    async def async_resolve(self, command: PendingCommand, reason: str) -> bool:
        return await self._write_command(command, dispatched=True, reason=reason)

    async def _write_command(
        self, command: PendingCommand, *, dispatched: bool, reason: str | None
    ) -> bool:
        async with self._lock:
            if self.state is None:
                return False
            current = next(
                (
                    item
                    for item in self.state.actuators
                    if item.target_identity == command.intent.target_registry_identity
                ),
                None,
            )
            if current is None:
                return False
            payload = self._stored_command(command, dispatched=dispatched)
            updated = replace(
                current,
                last_command_id=command.command_id,
                last_command_payload_fingerprint=payload.payload_fingerprint,
                last_command_context_id=command.context.context_id,
                pending_command=None if reason is not None else payload,
            )
            self._replace_actuator(current.target_identity, updated)
            assert self.state is not None
            return (await self._verified.async_write_critical(self.state)).verified

    async def async_update_runtime(
        self,
        *,
        control_enabled: bool,
        boost_mode: str,
        rapid_boost_reached: bool,
        boost_expiry_utc: str | None,
        configuration_fingerprint: str,
        strategy: str,
    ) -> bool:
        """Persist authoritative non-command runtime intent."""

        async with self._lock:
            if self.state is None:
                return False
            self.state = replace(
                self.state,
                storage_generation=self.state.storage_generation + 1,
                control_enabled_intent=control_enabled,
                selected_profile="comfort",
                previous_non_boost_profile="comfort",
                boost_expiry_utc=boost_expiry_utc,
                boost_mode=boost_mode,
                rapid_boost_reached=rapid_boost_reached,
                configuration_fingerprint=configuration_fingerprint,
                strategy=strategy,
            )
            return (await self._verified.async_write_critical(self.state)).verified

    async def async_update_last_valid_output(self, *, values_json: str, observed_at: str) -> bool:
        """Persist a display-only last-valid output snapshot."""

        _validate_last_valid_output(values_json, observed_at)
        async with self._lock:
            if self.state is None:
                return False
            if self.state.last_valid_at is not None:
                stored_at = datetime.fromisoformat(self.state.last_valid_at)
                candidate_at = datetime.fromisoformat(observed_at)
                if candidate_at < stored_at:
                    return True
            self.state = replace(
                self.state,
                storage_generation=self.state.storage_generation + 1,
                last_valid_values_json=values_json,
                last_valid_at=observed_at,
            )
            return (await self._verified.async_write_critical(self.state)).verified

    async def async_update_actuator(
        self,
        identity: str,
        *,
        ownership: str,
        ownership_revision: int,
        external_revision: int,
        override_reason: str | None,
        override_expiry: str | None,
        resume_required: bool,
    ) -> bool:
        """Persist ownership/override state without altering command correlation."""

        async with self._lock:
            if self.state is None:
                return False
            current = next(
                (item for item in self.state.actuators if item.target_identity == identity), None
            )
            if current is None:
                return False
            self._replace_actuator(
                identity,
                replace(
                    current,
                    ownership=ownership,
                    ownership_revision=ownership_revision,
                    external_revision=external_revision,
                    override_reason=override_reason,
                    override_expiry=override_expiry,
                    resume_required=resume_required,
                ),
            )
            assert self.state is not None
            return (await self._verified.async_write_critical(self.state)).verified

    async def async_mark_clean(self, *, now: datetime) -> bool:
        async with self._lock:
            if self.state is None:
                return False
            try:
                self.state = mark_clean_shutdown(self.state, now=now)
            except ValueError:
                return False
            return (await self._verified.async_write_critical(self.state)).verified


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
        reason = loaded.reason or "new_store"
        return StartupRecovery(
            state,
            loaded.reason == "corrupt_control_storage",
            reason,
            (reason,),
        )
    prior = loaded.state
    unresolved = any(actuator.pending_command is not None for actuator in prior.actuators)
    legacy_restart_gates = {
        actuator.target_identity
        for actuator in prior.actuators
        if actuator.resume_required
        and actuator.override_reason == "unclean_restart"
        and actuator.pending_command is None
        and actuator.ownership != "command_fault"
    }
    stored_resume_required = any(
        actuator.resume_required and actuator.target_identity not in legacy_restart_gates
        for actuator in prior.actuators
    )
    stored_command_fault = any(
        actuator.resume_required and actuator.ownership == "command_fault"
        for actuator in prior.actuators
    )
    configuration_changed = prior.configuration_fingerprint != configuration_fingerprint
    strategy_changed = prior.strategy != strategy
    # Persistence-before-dispatch makes an unresolved command or an explicit
    # persisted recovery flag authoritative. Configuration drift and an unclean
    # exit without either can safely use a fresh, live-target reconciliation.
    requires_resume = unresolved or stored_resume_required
    reasons = tuple(
        reason
        for active, reason in (
            (unresolved, "unresolved_command"),
            (stored_command_fault, "command_fault"),
            (stored_resume_required and not stored_command_fault, "stored_resume_required"),
            (bool(legacy_restart_gates), "legacy_unclean_restart_gate_cleared"),
            (configuration_changed, "configuration_changed"),
            (strategy_changed, "strategy_changed"),
            (not prior.clean_shutdown, "unclean_shutdown"),
            (prior.clean_shutdown, "clean_restart"),
        )
        if active
    )
    reason = reasons[0]
    actuators = tuple(
        replace(
            actuator,
            ownership="reconciling",
            resume_required=requires_resume,
            override_reason=(
                None
                if actuator.target_identity in legacy_restart_gates and not requires_resume
                else actuator.override_reason
            ),
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
        control_enabled_intent=prior.control_enabled_intent,
        selected_profile=prior.selected_profile,
        previous_non_boost_profile=prior.previous_non_boost_profile,
        boost_expiry_utc=prior.boost_expiry_utc,
        boost_mode=prior.boost_mode,
        rapid_boost_reached=prior.rapid_boost_reached,
        last_valid_values_json=(
            None if configuration_changed or strategy_changed else prior.last_valid_values_json
        ),
        last_valid_at=(None if configuration_changed or strategy_changed else prior.last_valid_at),
    )
    return StartupRecovery(state, requires_resume, reason, reasons)


def mark_clean_shutdown(state: ControlStoreState, *, now: datetime) -> ControlStoreState:
    """Mark clean only as an explicit, timezone-aware controlled-unload action."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if any(actuator.pending_command is not None for actuator in state.actuators):
        raise ValueError("cannot mark clean with unresolved commands")
    return replace(state, storage_generation=state.storage_generation + 1, clean_shutdown=True)


def _validate_last_valid_output(values_json: str | None, observed_at: str | None) -> None:
    """Validate the bounded display snapshot without trusting executable state."""

    if (values_json is None) != (observed_at is None):
        raise ValueError("last valid output fields must be present together")
    if values_json is None:
        return
    try:
        values: Any = json.loads(values_json)
        timestamp = datetime.fromisoformat(observed_at or "")
        if not isinstance(values, dict) or not set(values) <= set(LAST_VALID_OUTPUT_KEYS):
            raise ValueError("invalid last valid output keys")
        json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as err:
        raise ValueError("invalid last valid output") from err
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("invalid last valid output timestamp")

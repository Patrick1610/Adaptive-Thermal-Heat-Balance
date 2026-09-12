"""Control-storage verification and restart simulations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from custom_components.athb.adapters.storage import (
    ControlLoadResult,
    ControlStoreState,
    StoredActuator,
    StoredCommand,
    VerifiedControlStore,
    ZoneCommandPersistence,
    load_control_state,
    mark_clean_shutdown,
    prepare_startup_recovery,
    serialize_control_state,
)


class MemoryBackend:
    def __init__(self) -> None:
        self.value: str | None = None
        self.save_error = False
        self.read_error = False
        self.rewrite: str | None = None

    async def async_save(self, serialized: str) -> None:
        if self.save_error:
            raise OSError("save failed")
        self.value = serialized

    async def async_readback(self) -> str | None:
        if self.read_error:
            raise OSError("read failed")
        return self.rewrite if self.rewrite is not None else self.value


def _command(*, dispatched: bool = False) -> StoredCommand:
    return StoredCommand(
        "cmd-1",
        "registry-1",
        "temperature=20",
        "ctx-1",
        1,
        2,
        3,
        4,
        "2026-09-11T12:00:00+00:00",
        "2026-09-11T12:01:00+00:00",
        dispatched,
    )


def _actuator(command: StoredCommand | None = None) -> StoredActuator:
    return StoredActuator(
        "registry-1",
        "owned",
        4,
        0,
        None,
        None,
        False,
        "temperature=19",
        None,
        None,
        None,
        command,
    )


def _state(*, clean: bool = False, command: StoredCommand | None = None) -> ControlStoreState:
    return ControlStoreState(7, "run-old", clean, "config-a", "balanced", (_actuator(command),))


def test_control_state_round_trip_is_canonical_and_versioned() -> None:
    state = _state(command=_command())
    serialized = serialize_control_state(state)
    loaded = load_control_state(serialized)
    assert loaded == ControlLoadResult(state, None)
    assert serialize_control_state(loaded.state) == serialized


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "storage_missing"),
        ("not-json", "corrupt_control_storage"),
        ('{"schema_version":99}', "unsupported_storage_version"),
        ('{"schema_version":1,"actuators":false}', "corrupt_control_storage"),
    ],
)
def test_missing_corrupt_and_unknown_storage_fail_closed(raw: str | None, reason: str) -> None:
    result = load_control_state(raw)
    assert result.state is None
    assert result.reason == reason
    if raw is not None:
        assert result.corrupt_raw == raw


def test_loaded_control_types_and_command_identity_are_validated_strictly() -> None:
    state = _state(command=_command())
    raw = serialize_control_state(state)
    assert load_control_state(raw).state == state
    malformed_values = (
        raw.replace('"storage_generation":7', '"storage_generation":true'),
        raw.replace('"clean_shutdown":false', '"clean_shutdown":0'),
        raw.replace('"target_identity":"registry-1"', '"target_identity":"other"', 1),
        raw.replace("2026-09-11T12:01:00+00:00", "2026-09-11T11:00:00+00:00"),
    )
    for malformed in malformed_values:
        assert load_control_state(malformed).reason == "corrupt_control_storage"


@pytest.mark.parametrize(
    "state",
    [
        replace(_state(), schema_version=2),
        replace(_state(), storage_generation=-1),
        replace(_state(), clean_shutdown=cast(bool, 0)),
        replace(_state(), control_enabled_intent=cast(bool, 1)),
        replace(_state(), selected_profile=""),
        replace(_state(), previous_non_boost_profile=""),
        replace(_state(), boost_mode="turbo"),
        replace(_state(), rapid_boost_reached=cast(bool, 1)),
        replace(_state(), run_id=""),
        replace(_state(), boost_expiry_utc="2026-09-11T13:00:00"),
        replace(_state(), actuators=(_actuator(), _actuator())),
    ],
)
def test_serialization_rejects_invalid_state_invariants(state: ControlStoreState) -> None:
    with pytest.raises(ValueError, match=r"invalid|identity|duplicate"):
        serialize_control_state(state)


def test_loading_rejects_non_object_actuators_and_naive_override() -> None:
    raw = serialize_control_state(_state())
    malformed = (
        raw.replace('"actuators":[{', '"actuators":["bad",{', 1),
        raw.replace('"override_expiry":null', '"override_expiry":"2026-09-11T13:00:00"'),
        raw.replace('"pending_command":null', '"pending_command":false'),
    )
    for payload in malformed:
        assert load_control_state(payload).reason == "corrupt_control_storage"


def test_verified_critical_write_reads_back_exact_payload() -> None:
    backend = MemoryBackend()
    result = asyncio.run(VerifiedControlStore(backend).async_write_critical(_state()))
    assert result.verified
    assert result.reason == "storage_verified"


def test_failed_save_and_readback_inhibit_dispatch() -> None:
    for save_error, read_error in ((True, False), (False, True)):
        backend = MemoryBackend()
        backend.save_error = save_error
        backend.read_error = read_error
        result = asyncio.run(VerifiedControlStore(backend).async_write_critical(_state()))
        assert not result.verified
        assert result.reason == "storage_io_failed"


def test_generation_and_payload_mismatch_fail_verification() -> None:
    backend = MemoryBackend()
    backend.rewrite = serialize_control_state(replace(_state(), storage_generation=8))
    result = asyncio.run(VerifiedControlStore(backend).async_write_critical(_state()))
    assert not result.verified
    assert result.reason == "storage_generation_mismatch"

    backend.rewrite = serialize_control_state(replace(_state(), strategy="comfort"))
    result = asyncio.run(VerifiedControlStore(backend).async_write_critical(_state()))
    assert not result.verified
    assert result.reason == "storage_payload_mismatch"


def test_clean_restart_restores_intent_only_and_starts_new_run_unclean() -> None:
    loaded = load_control_state(serialize_control_state(_state(clean=True)))
    recovery = prepare_startup_recovery(
        loaded, run_id="run-new", configuration_fingerprint="config-a", strategy="balanced"
    )
    assert not recovery.requires_resume
    assert recovery.reason == "clean_restart"
    assert not recovery.state.clean_shutdown
    assert recovery.state.run_id == "run-new"
    assert recovery.state.actuators[0].ownership == "reconciling"


def test_unclean_or_unresolved_restart_requires_resume_and_never_replays_command() -> None:
    for prior, reason in (
        (_state(clean=False), "unclean_shutdown"),
        (_state(clean=True, command=_command(dispatched=True)), "unresolved_command"),
    ):
        recovery = prepare_startup_recovery(
            load_control_state(serialize_control_state(prior)),
            run_id="run-new",
            configuration_fingerprint="config-a",
            strategy="balanced",
        )
        assert recovery.requires_resume
        assert recovery.reason == reason
        assert recovery.state.actuators[0].pending_command is None


def test_corrupt_store_is_preserved_and_requires_resume() -> None:
    loaded = load_control_state("broken")
    recovery = prepare_startup_recovery(
        loaded, run_id="run-new", configuration_fingerprint="config-a", strategy="balanced"
    )
    assert recovery.requires_resume
    assert recovery.reason == "corrupt_control_storage"
    assert recovery.state.actuators == ()


def test_strategy_or_configuration_drift_cannot_restore_stale_state() -> None:
    prior = load_control_state(serialize_control_state(_state(clean=True)))
    for fingerprint, strategy in (("config-b", "balanced"), ("config-a", "comfort")):
        recovery = prepare_startup_recovery(
            prior,
            run_id="run-new",
            configuration_fingerprint=fingerprint,
            strategy=strategy,
        )
        assert recovery.requires_resume
        assert recovery.reason == "configuration_changed"


def test_clean_shutdown_rejects_pending_commands_and_naive_clock() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    clean = mark_clean_shutdown(_state(), now=now)
    assert clean.clean_shutdown
    assert clean.storage_generation == 8
    with pytest.raises(ValueError, match="unresolved"):
        mark_clean_shutdown(_state(command=_command()), now=now)
    with pytest.raises(ValueError, match="timezone-aware"):
        mark_clean_shutdown(_state(), now=now.replace(tzinfo=None))
    with pytest.raises(ValueError, match="run_id"):
        prepare_startup_recovery(
            load_control_state(None),
            run_id="",
            configuration_fingerprint="config-a",
            strategy="balanced",
        )


async def test_zone_persistence_serializes_runtime_and_ownership_updates() -> None:
    backend = MemoryBackend()
    persistence = ZoneCommandPersistence(
        backend,
        run_id="run-current",
        configuration_fingerprint="config-current",
        strategy="balanced",
        target_identities=("registry-1",),
        control_enabled=True,
        boost_mode="off",
    )
    assert await persistence.async_start()
    assert await persistence.async_update_runtime(
        control_enabled=True,
        boost_mode="rapid",
        rapid_boost_reached=True,
        boost_expiry_utc="2026-09-11T13:00:00+00:00",
    )
    assert await persistence.async_update_actuator(
        "registry-1",
        ownership="manual_override",
        ownership_revision=3,
        external_revision=2,
        override_reason="external_temperature_target",
        override_expiry="2026-09-11T14:00:00+00:00",
        resume_required=False,
    )
    loaded = load_control_state(backend.value)
    assert loaded.state is not None
    assert loaded.state.control_enabled_intent is True
    assert loaded.state.boost_mode == "rapid"
    assert loaded.state.rapid_boost_reached is True
    assert loaded.state.boost_expiry_utc == "2026-09-11T13:00:00+00:00"
    assert loaded.state.actuators[0].ownership == "manual_override"
    assert loaded.state.actuators[0].external_revision == 2


async def test_unstarted_or_unknown_persistence_operations_fail_closed() -> None:
    persistence = ZoneCommandPersistence(
        MemoryBackend(),
        run_id="run-current",
        configuration_fingerprint="config-current",
        strategy="balanced",
        target_identities=("registry-1",),
    )
    with pytest.raises(RuntimeError, match="not started"):
        persistence._replace_actuator("registry-1", _actuator())
    assert not await persistence.async_persist_pending(cast(Any, None))
    assert not await persistence.async_mark_dispatched(cast(Any, None))
    assert not await persistence.async_resolve(cast(Any, None), "resolved")
    assert not await persistence.async_update_runtime(
        control_enabled=False,
        boost_mode="off",
        rapid_boost_reached=False,
        boost_expiry_utc=None,
    )
    assert not await persistence.async_update_actuator(
        "registry-1",
        ownership="owned",
        ownership_revision=1,
        external_revision=0,
        override_reason=None,
        override_expiry=None,
        resume_required=False,
    )
    assert not await persistence.async_mark_clean(now=datetime.now(UTC))

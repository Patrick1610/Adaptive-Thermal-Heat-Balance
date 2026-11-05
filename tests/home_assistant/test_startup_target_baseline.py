"""Startup observations must not impersonate users or hide later interventions."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.athb import runtime as runtime_module
from custom_components.athb.adapters.broker import CommandBroker
from custom_components.athb.adapters.climate import capability_from_state
from custom_components.athb.adapters.storage import ControlStoreState, StoredActuator
from custom_components.athb.calculation import calculate_runtime_snapshot
from custom_components.athb.core.contracts import DispatchStatus
from custom_components.athb.core.ownership import (
    DataReadiness,
    Ownership,
    OwnershipState,
    TargetReadiness,
)
from custom_components.athb.runtime import ZoneRuntime
from tests.home_assistant.test_runtime import _runtime, _start_delivery_runtime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios

IDENTITY = "registry-1"


def _attributes(*, unit: str = "°C", ranged: bool = False, **targets: Any) -> dict[str, Any]:
    return {
        "hvac_modes": ["off", "heat_cool"] if ranged else ["off", "heat"],
        "supported_features": 2 if ranged else 1,
        "min_temp": 40.0 if unit == "°F" else 4.0,
        "max_temp": 95.0 if unit == "°F" else 35.0,
        "target_temp_step": 0.1,
        "unit_of_measurement": unit,
        **targets,
    }


def _feedback_runtime(
    hass: HomeAssistant, initial: State | None, *, service: Any = None
) -> ZoneRuntime:
    """Use the actual feedback classifier without dispatching heating services."""

    if initial is not None:
        hass.states.async_set(initial.entity_id, initial.state, initial.attributes)
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.ownership[IDENTITY] = OwnershipState(
        IDENTITY, Ownership.RECONCILING, DataReadiness.READY, TargetReadiness.SUSPENDED_MODE
    )
    runtime.capability_generations[IDENTITY] = 1

    runtime_module.get_lease_registry(hass).acquire(IDENTITY, runtime.entry.entry_id)
    runtime.broker = CommandBroker(
        service=service or cast(Any, SimpleNamespace(async_set_temperature=AsyncMock())),
        persistence=cast(
            Any,
            SimpleNamespace(
                async_persist_pending=AsyncMock(return_value=True),
                async_mark_dispatched=AsyncMock(return_value=True),
                async_resolve=AsyncMock(return_value=True),
            ),
        ),
        preflight=runtime._broker_preflight,
        command_id_factory=lambda: "unused-startup-command",
        context_factory=runtime._context_token,
    )
    runtime._initialize_target_fingerprints()
    return runtime


def _cancel_timers(runtime: ZoneRuntime) -> None:
    for cancel in runtime.timers.values():
        cancel()
    runtime_module.get_lease_registry(runtime.hass).release(IDENTITY, runtime.entry.entry_id)


async def _observe(runtime: ZoneRuntime, old: State | None, new: State) -> None:
    await runtime._async_handle_target_state(runtime.entry.data["targets"][0], old, new)


async def test_boot_target_restore_then_fresh_policy_and_later_hardware_change(
    hass: HomeAssistant,
) -> None:
    """A real startup/controller/broker run restores control without replaying heat."""

    runtime, calls = await _start_delivery_runtime(
        hass, mode="off", initial_target=cast(Any, None), outcome="ack"
    )
    try:
        assert IDENTITY in runtime.initial_target_baselines
        assert calls == []
        old = hass.states.get("climate.target")
        assert old is not None
        hass.states.async_set("climate.target", "heat", {**old.attributes, "temperature": 20.2})
        await hass.async_block_till_done()
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].external_revision == 0

        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()

        assert runtime.ownership[IDENTITY].ownership is Ownership.OWNED
        assert len(calls) == 1
        assert calls[0].data["temperature"] == 18.0
        assert runtime.broker is not None
        assert runtime.broker.delivery_state(IDENTITY)["status"] == "acknowledged"

        current = hass.states.get("climate.target")
        assert current is not None
        hass.states.async_set("climate.target", "heat", {**current.attributes, "temperature": 21.0})
        await hass.async_block_till_done()
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("missing_state", [False, True])
async def test_first_explicit_user_target_is_not_a_startup_baseline(
    hass: HomeAssistant, missing_state: bool
) -> None:
    initial = None if missing_state else State("climate.target", "off", _attributes())
    runtime = _feedback_runtime(hass, initial)
    try:
        observed = State(
            "climate.target",
            "heat",
            _attributes(temperature=20.2),
            context=Context(user_id="real-user"),
        )
        await _observe(runtime, initial, observed)

        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        _cancel_timers(runtime)


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf")])
async def test_user_mode_only_change_does_not_invent_a_temperature_override(
    hass: HomeAssistant,
    invalid: Any,
) -> None:
    initial = State("climate.target", "off", _attributes())
    runtime = _feedback_runtime(hass, initial)
    try:
        mode_only = State(
            "climate.target",
            "heat",
            _attributes(temperature=invalid),
            context=Context(user_id="real-user"),
        )
        await _observe(runtime, initial, mode_only)
        assert IDENTITY in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert runtime.ownership[IDENTITY].external_revision == 0

        complete = State("climate.target", "heat", _attributes(temperature=20.2))
        await _observe(runtime, mode_only, complete)
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert runtime.ownership[IDENTITY].external_revision == 0
    finally:
        _cancel_timers(runtime)


async def test_user_context_on_unavailable_startup_state_is_not_a_target_change(
    hass: HomeAssistant,
) -> None:
    initial = State("climate.target", "off", _attributes())
    runtime = _feedback_runtime(hass, initial)
    try:
        unavailable = State("climate.target", "unavailable", context=Context(user_id="real-user"))
        await _observe(runtime, initial, unavailable)
        assert IDENTITY in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert runtime.ownership[IDENTITY].external_revision == 0

        complete = State("climate.target", "heat", _attributes(temperature=20.2))
        await _observe(runtime, unavailable, complete)
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert runtime.ownership[IDENTITY].external_revision == 0
    finally:
        _cancel_timers(runtime)


async def test_valid_off_target_is_already_a_baseline(hass: HomeAssistant) -> None:
    initial = State("climate.target", "off", _attributes(temperature=17.0))
    runtime = _feedback_runtime(hass, initial)
    try:
        assert IDENTITY not in runtime.initial_target_baselines
        await _observe(
            runtime, initial, State("climate.target", "heat", _attributes(temperature=18.0))
        )
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        _cancel_timers(runtime)


@pytest.mark.parametrize("clean_shutdown", [False, True])
async def test_saved_manual_override_survives_first_automatic_target(
    hass: HomeAssistant, clean_shutdown: bool
) -> None:
    expiry = dt_util.utcnow() + timedelta(minutes=37)
    actuator = StoredActuator(
        IDENTITY,
        "manual_override",
        9,
        7,
        "external_temperature_target",
        expiry.isoformat(),
        False,
        None,
        None,
        None,
        None,
        None,
    )
    stored = ControlStoreState(4, "previous-run", clean_shutdown, "old", "balanced", (actuator,))
    runtime, calls = await _start_delivery_runtime(
        hass, stored=stored, mode="off", initial_target=cast(Any, None), outcome="ack"
    )
    try:
        assert runtime.ownership[IDENTITY].override_expiry == expiry
        old = hass.states.get("climate.target")
        assert old is not None
        hass.states.async_set("climate.target", "heat", {**old.attributes, "temperature": 20.2})
        await hass.async_block_till_done()
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()

        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].override_expiry == expiry
        assert runtime.ownership[IDENTITY].external_revision == 7
        assert calls == []
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("unit", ["°C", "°F"])
@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("auto_mode", [False, True])
async def test_first_complete_scalar_or_range_is_consumed_only_once(
    hass: HomeAssistant, unit: str, ranged: bool, auto_mode: bool
) -> None:
    scalar = 68.0 if unit == "°F" else 20.0
    low, high = (66.0, 75.0) if unit == "°F" else (19.0, 23.0)
    initial_targets = {"target_temp_low": low} if ranged else {}
    initial = State(
        "climate.target",
        "auto" if auto_mode else "heat_cool" if ranged else "heat",
        _attributes(unit=unit, ranged=ranged, **initial_targets),
    )
    runtime = _feedback_runtime(hass, initial)
    try:
        complete_targets = (
            {"target_temp_low": low, "target_temp_high": high}
            if ranged
            else {"temperature": scalar}
        )
        complete = State(
            "climate.target",
            initial.state,
            _attributes(unit=unit, ranged=ranged, **complete_targets),
        )
        await _observe(runtime, initial, complete)
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].external_revision == 0

        changed_targets = (
            {**complete_targets, "target_temp_high": high + 0.5}
            if ranged
            else {"temperature": scalar + 0.5}
        )
        changed = State(
            "climate.target",
            initial.state,
            _attributes(unit=unit, ranged=ranged, **changed_targets),
        )
        await _observe(runtime, complete, changed)
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        _cancel_timers(runtime)


@pytest.mark.parametrize("invalid", [None, True, float("nan"), float("inf"), float("-inf")])
async def test_incomplete_or_nonfinite_observations_do_not_prime_a_baseline(
    hass: HomeAssistant, invalid: Any
) -> None:
    initial = State("climate.target", "off", _attributes(temperature=invalid))
    runtime = _feedback_runtime(hass, initial)
    try:
        assert IDENTITY in runtime.initial_target_baselines
        incomplete = State("climate.target", "heat", _attributes(temperature=invalid))
        await _observe(runtime, initial, incomplete)
        assert IDENTITY in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].external_revision == 0

        live = State("climate.target", "heat", _attributes(temperature=20.2))
        await _observe(runtime, incomplete, live)
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].external_revision == 0
    finally:
        _cancel_timers(runtime)


@pytest.mark.parametrize("user_initiated", [False, True])
async def test_same_restored_target_with_user_context_is_not_a_temperature_intervention(
    hass: HomeAssistant, user_initiated: bool
) -> None:
    restored = State("climate.target", "heat", _attributes(temperature=20.2, restored=True))
    runtime = _feedback_runtime(hass, restored)
    try:
        live = State(
            "climate.target",
            "heat",
            _attributes(temperature=20.2),
            context=Context(user_id="real-user" if user_initiated else None),
        )
        await _observe(runtime, restored, live)
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].external_revision == 0
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
    finally:
        _cancel_timers(runtime)


async def test_permanent_nonfinite_target_is_visible_and_blocks_normal_and_safety_writes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="custom_components.athb.runtime")
    runtime, calls = await _start_delivery_runtime(hass, initial_target=float("nan"), outcome="ack")
    try:
        assert runtime.controller is not None
        for _ in range(3):
            runtime.async_request_snapshot()
            await runtime.controller.async_wait_idle()
            await hass.async_block_till_done()
            assert runtime.values["control_status"] == "awaiting_initial_target"
            assert runtime.values["startup_target_baselines_pending"] == 1
            assert not runtime._broker_preflight(IDENTITY).target_ready
            assert calls == []

        warnings = [
            record
            for record in caplog.records
            if record.name == "custom_components.athb.runtime"
            and record.levelno >= logging.WARNING
            and "initial climate target" in record.getMessage()
        ]
        assert len(warnings) == 1

        calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
        target = replace(
            calculation.targets[0], registry_identity=IDENTITY, entity_id="climate.target"
        )
        assert target.mapping is not None
        assert target.result is not None
        assert target.result.normalized is not None
        now = dt_util.utcnow()
        safety_intent = runtime._intent_from_result(
            target,
            target.result.normalized,
            target.mapping,
            now,
            explicit_transition=True,
            safety_deescalation=True,
        )
        assert runtime.broker is not None
        outcome = await runtime.broker.async_submit(safety_intent, now=now)
        assert outcome.dispatch_status is DispatchStatus.NOT_DISPATCHED
        assert calls == []

        current = hass.states.get("climate.target")
        assert current is not None
        hass.states.async_set("climate.target", "heat", {**current.attributes, "temperature": 20.2})
        await hass.async_block_till_done()
        runtime.async_request_snapshot()
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.OWNED
        assert runtime.values["control_status"] != "awaiting_initial_target"
        assert runtime.values["startup_target_baselines_pending"] == 0
    finally:
        await runtime.async_unload()


async def test_snapshot_cannot_skip_queued_restore_then_hardware_change(
    hass: HomeAssistant,
) -> None:
    initial = State("climate.target", "off", _attributes())
    runtime = _feedback_runtime(hass, initial)
    try:
        # The latest captured snapshot can already contain B before the queued
        # state callbacks have processed the actual first-live A, then B order.
        restored = State("climate.target", "heat", _attributes(temperature=20.2))
        hardware_changed = State("climate.target", "heat", _attributes(temperature=21.0))
        hass.states.async_set("climate.target", hardware_changed.state, hardware_changed.attributes)
        calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
        target = replace(
            calculation.targets[0],
            registry_identity=IDENTITY,
            entity_id="climate.target",
            capability=capability_from_state(hardware_changed),
        )
        calculation = replace(calculation, targets=(target,))
        recovery = runtime._reconcile_targets(calculation)
        assert IDENTITY in runtime.initial_target_baselines
        assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert recovery == frozenset()

        await _observe(runtime, initial, restored)
        assert IDENTITY not in runtime.initial_target_baselines
        for captured in (restored, hardware_changed):
            queued_target = replace(target, capability=capability_from_state(captured))
            assert (
                runtime._reconcile_targets(replace(calculation, targets=(queued_target,)))
                == frozenset()
            )
            assert runtime.ownership[IDENTITY].ownership is Ownership.RECONCILING
        assert not runtime._broker_preflight(IDENTITY).target_ready

        await _observe(runtime, restored, hardware_changed)
        runtime._reconcile_targets(calculation)

        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        _cancel_timers(runtime)


async def test_live_target_change_blocks_dispatch_before_its_state_callback(
    hass: HomeAssistant,
) -> None:
    initial = State("climate.target", "off", _attributes())
    service = SimpleNamespace(async_set_temperature=AsyncMock())
    runtime = _feedback_runtime(hass, initial, service=service)
    try:
        restored = State("climate.target", "heat", _attributes(temperature=20.2))
        hass.states.async_set("climate.target", restored.state, restored.attributes)
        await _observe(runtime, initial, restored)
        calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
        target = replace(
            calculation.targets[0],
            registry_identity=IDENTITY,
            entity_id="climate.target",
            capability=capability_from_state(restored),
        )
        calculation = replace(calculation, targets=(target,))
        assert runtime._reconcile_targets(calculation) == frozenset({IDENTITY})
        assert runtime.ownership[IDENTITY].ownership is Ownership.OWNED
        assert target.mapping is not None
        assert target.result is not None
        assert target.result.normalized is not None
        now = dt_util.utcnow()
        intent = runtime._intent_from_result(
            target, target.result.normalized, target.mapping, now, explicit_transition=True
        )

        # The hardware changed, but its queued callback has not run yet.
        changed = State("climate.target", "heat", _attributes(temperature=21.0))
        hass.states.async_set("climate.target", changed.state, changed.attributes)
        assert not runtime._broker_preflight(IDENTITY).target_ready
        assert runtime.broker is not None
        outcome = await runtime.broker.async_submit(intent, now=now)
        assert outcome.reason == "target_not_ready"
        service.async_set_temperature.assert_not_awaited()

        await _observe(runtime, restored, changed)
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].external_revision == 1
    finally:
        _cancel_timers(runtime)


async def test_pending_write_acknowledgement_is_not_swallowed_by_startup_baseline(
    hass: HomeAssistant,
) -> None:
    runtime, calls = await _start_delivery_runtime(hass, outcome="ignore")
    try:
        assert len(calls) == 1
        assert runtime.broker is not None
        assert runtime.broker.state_counts(IDENTITY) == (1, 0)
        # Exercise the defensive case where an initial observation and an
        # already-dispatched command coexist; the command owns this feedback.
        runtime.initial_target_baselines.add(IDENTITY)
        current = hass.states.get("climate.target")
        assert current is not None
        hass.states.async_set(
            "climate.target",
            "heat",
            {**current.attributes, "temperature": calls[0].data["temperature"]},
            context=calls[0].context,
        )
        await hass.async_block_till_done()

        assert IDENTITY not in runtime.initial_target_baselines
        assert runtime.broker.state_counts(IDENTITY) == (0, 0)
        assert runtime.broker.delivery_state(IDENTITY)["status"] == "acknowledged"
        assert runtime.ownership[IDENTITY].ownership is Ownership.OWNED
        assert runtime.ownership[IDENTITY].external_revision == 0
        assert len(calls) == 1
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("user_initiated", [False, True])
async def test_external_same_value_service_during_startup_preserves_manual_intent(
    hass: HomeAssistant, user_initiated: bool
) -> None:
    initial = State("climate.target", "heat", _attributes(temperature=20.2, restored=True))
    runtime = _feedback_runtime(hass, initial)
    try:
        runtime._service_event(
            cast(
                Any,
                SimpleNamespace(
                    data={
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": {"entity_id": "climate.target", "temperature": 20.2},
                    },
                    context=Context(user_id="real-user" if user_initiated else None),
                ),
            )
        )
        manual = runtime.ownership[IDENTITY]
        assert manual.ownership is Ownership.MANUAL_OVERRIDE
        assert IDENTITY not in runtime.initial_target_baselines
        observed = State("climate.target", "heat", _attributes(temperature=20.2))
        await _observe(runtime, initial, observed)
        assert runtime.ownership[IDENTITY].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership[IDENTITY].override_expiry == manual.override_expiry
    finally:
        _cancel_timers(runtime)

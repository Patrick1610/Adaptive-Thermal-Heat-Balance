"""Runtime action, lease, callback, and cleanup tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.athb import runtime as runtime_module
from custom_components.athb.adapters.broker import CommandBroker, CommandOutcome
from custom_components.athb.adapters.climate import (
    HomeAssistantClimateService,
    capability_from_state,
)
from custom_components.athb.adapters.storage import (
    ControlStoreState,
    HomeAssistantControlStorageBackend,
    StoredActuator,
    StoredCommand,
    serialize_control_state,
)
from custom_components.athb.calculation import CapturedTarget, calculate_runtime_snapshot
from custom_components.athb.core.climate import (
    ClimateCapabilitySnapshot,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    TemperatureUnit,
)
from custom_components.athb.core.contracts import (
    AcknowledgementStatus,
    BoostMode,
    DispatchStatus,
    Observation,
    ObservationValidity,
    Provenance,
)
from custom_components.athb.core.history import HistoryQuality, RunningMeanResult
from custom_components.athb.core.ownership import (
    DataReadiness,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    TargetReadiness,
)
from custom_components.athb.core.sources import SourceKind, SourceState
from custom_components.athb.runtime import ZoneRuntime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios


async def _start_delivery_runtime(
    hass: HomeAssistant,
    *,
    outcome: str = "ignore",
    stored: ControlStoreState | None = None,
    mode: str = "heat",
    control_enabled: bool = True,
    outside: str = "5",
    initial_target: float = 23.0,
) -> tuple[ZoneRuntime, list[ServiceCall]]:
    calls: list[ServiceCall] = []
    attributes = {
        "hvac_modes": ["off", "heat_cool"] if mode != "heat" else ["off", "heat"],
        "supported_features": 1,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "temperature": initial_target,
        "current_temperature": 20.0,
        "unit_of_measurement": "°C",
    }

    async def write(call: ServiceCall) -> None:
        calls.append(call)
        if outcome == "error":
            raise TimeoutError("temporary transport error")
        if outcome == "ack":
            current = hass.states.get("climate.target")
            assert current is not None
            hass.states.async_set(
                "climate.target",
                current.state,
                {**current.attributes, "temperature": call.data["temperature"]},
                context=call.context,
            )

    hass.services.async_register("climate", "set_temperature", write)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", outside, {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.target", mode, attributes)
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        entry_id="delivery-test",
        data={
            "zone_uuid": "delivery-test",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": control_enabled,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(
        hass, cast(Any, entry), "delivery-test", "balanced", "off", control_enabled
    )
    if stored is not None:
        await HomeAssistantControlStorageBackend(hass, "delivery-test").async_save(
            serialize_control_state(
                replace(
                    stored,
                    configuration_fingerprint=runtime._configuration_fingerprint(),
                    strategy="balanced",
                )
            )
        )
    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()
    return runtime, calls


@pytest.mark.parametrize(
    ("enabled", "mode"), [(False, "heat_cool"), (True, "heat_cool"), (True, "off"), (True, "auto")]
)
async def test_bidirectional_runtime_previews_and_optional_scalar_writes(
    hass, monkeypatch, enabled, mode
):
    monkeypatch.setattr(
        runtime_module.OutdoorHistoryCollector,
        "result",
        lambda *args, **kwargs: RunningMeanResult(5.0, HistoryQuality.COMPLETE, 7, 1.0, None, ()),
    )
    runtime, calls = await _start_delivery_runtime(
        hass,
        outcome="ack",
        mode=mode,
        control_enabled=enabled,
        outside="22",
        initial_target=18.0,
    )
    try:
        assert runtime.values["effective_targets"]["target-1"]["temperature"] == 23.0
        details = runtime.values["effective_target_details"]["target-1"]
        assert details["scalar_selection"]["branch"] == "cooling"
        assert "heating_demand" not in details
        assert len(calls) == int(enabled and mode != "off")
        if calls:
            assert dict(calls[0].data) == {"entity_id": "climate.target", "temperature": 23.0}
            assert runtime.values["command_delivery"]["registry-1"]["status"] == "acknowledged"
        hass.states.async_set("sensor.outdoor", "20", {"unit_of_measurement": "°C"})
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert (
            runtime.values["effective_target_details"]["target-1"]["scalar_selection"]["branch"]
            == "heating"
        )
        if enabled and mode != "off":
            attributes = dict(hass.states.get("climate.target").attributes)
            hass.states.async_set(
                "climate.target",
                mode,
                {**attributes, "temperature": 18.0},
                context=Context(user_id="test-user"),
            )
            await hass.async_block_till_done()
            assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
            before = len(calls)
            runtime.async_request_snapshot()
            await runtime.controller.async_wait_idle()
            await hass.async_block_till_done()
            assert len(calls) == before
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["off", "heat_cool"])
async def test_bidirectional_diagnostic_preview_is_fresh_but_never_written(
    hass, monkeypatch, enabled, mode
):
    monkeypatch.setattr(
        runtime_module.OutdoorHistoryCollector,
        "result",
        lambda *args, **kwargs: RunningMeanResult(
            5.0, HistoryQuality.DIAGNOSTIC, 1, 0.25, None, ("history_not_control_eligible",)
        ),
    )
    runtime, calls = await _start_delivery_runtime(
        hass, outcome="ack", mode=mode, control_enabled=enabled, outside="22", initial_target=18.0
    )
    try:
        assert runtime.values["effective_targets"]["target-1"]["temperature"] == 23.0
        detail = runtime.values["effective_target_details"]["target-1"]
        assert detail["mode"] == "diagnostic_preview"
        assert detail["preview_only"] is True
        assert detail["scalar_selection"]["branch"] == "cooling"
        assert runtime.values["data_quality"] == "diagnostic_estimate"
        assert runtime.values["control_eligible"] is False
        assert runtime.last_valid_values == {}
        assert calls == []

        # A previous trusted target and expired failure hold cannot override or
        # dispatch the fresh diagnostic preview.
        runtime.last_valid_values = {
            "effective_targets": {"target-1": {"temperature": 18.0}},
            "effective_target_details": {"target-1": {"mode": "adaptive"}},
        }
        runtime.failure_hold_elapsed = True
        hass.states.async_set("sensor.outdoor", "20", {"unit_of_measurement": "°C"})
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        detail = runtime.values["effective_target_details"]["target-1"]
        assert detail["mode"] == "diagnostic_preview"
        assert detail["stale"] is False
        assert detail["scalar_selection"]["branch"] == "heating"
        assert runtime.values["control_eligible"] is False
        assert calls == []
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("enabled", [False, True])
async def test_bidirectional_hvac_on_automatically_uses_latest_target(hass, monkeypatch, enabled):
    monkeypatch.setattr(
        runtime_module.OutdoorHistoryCollector,
        "result",
        lambda *args, **kwargs: RunningMeanResult(5.0, HistoryQuality.COMPLETE, 7, 1.0, None, ()),
    )
    runtime, calls = await _start_delivery_runtime(
        hass, outcome="ack", mode="off", control_enabled=enabled, outside="22", initial_target=18.0
    )
    try:
        assert calls == []
        previous = runtime.values["effective_targets"]["target-1"]["temperature"]
        hass.states.async_set("sensor.outdoor", "20", {"unit_of_measurement": "°C"})
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        current = runtime.values["effective_targets"]["target-1"]["temperature"]
        assert current != previous
        target = hass.states.get("climate.target")
        assert target is not None
        hass.states.async_set(
            "climate.target", "heat_cool", target.attributes, context=Context(user_id="test-user")
        )
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
        await hass.async_block_till_done()
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert len(calls) == int(enabled)
        if enabled:
            assert calls[0].data["temperature"] == current
            assert runtime.values["command_delivery"]["registry-1"]["status"] == "acknowledged"
            assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
        else:
            assert runtime.ownership["registry-1"].ownership is Ownership.DISABLED
    finally:
        await runtime.async_unload()


async def test_bidirectional_estimated_target_becomes_executable_only_after_history_recovers(
    hass, monkeypatch
):
    result = RunningMeanResult(
        5.0, HistoryQuality.DIAGNOSTIC, 1, 0.25, None, ("history_not_control_eligible",)
    )
    monkeypatch.setattr(
        runtime_module.OutdoorHistoryCollector, "result", lambda *args, **kwargs: result
    )
    runtime, calls = await _start_delivery_runtime(
        hass, outcome="ack", mode="heat_cool", outside="22", initial_target=18.0
    )
    try:
        assert calls == []
        assert runtime.values["effective_target_details"]["target-1"]["preview_only"] is True
        result = RunningMeanResult(5.0, HistoryQuality.COMPLETE, 7, 1.0, None, ())
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert len(calls) == 1
        assert calls[0].data["temperature"] == 23.0
        assert runtime.values["effective_target_details"]["target-1"]["preview_only"] is False
        assert runtime.values["command_delivery"]["registry-1"]["status"] == "acknowledged"
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("service_outcome", ["ignore", "error"])
async def test_runtime_retries_five_times_without_resume_and_recalculates_latest_target(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    service_outcome: str,
) -> None:
    runtime, calls = await _start_delivery_runtime(hass, outcome=service_outcome)
    clock = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock[0])
    try:
        assert len(calls) == 1
        assert runtime.broker is not None
        for attempt in range(5):
            clock[0] += timedelta(seconds=30)
            await runtime._async_acknowledgement_timeout("registry-1")
            assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
            assert not runtime.ownership["registry-1"].resume_required
            assert runtime.values["command_delivery"]["registry-1"]["attempts"] == attempt + 1
            clock[0] += timedelta(seconds=1)
            runtime.async_request_snapshot()
            assert runtime.controller is not None
            await runtime.controller.async_wait_idle()
            await hass.async_block_till_done()
            assert len(calls) == attempt + 1
            clock[0] += timedelta(seconds=29)
            runtime.async_request_snapshot()
            assert runtime.controller is not None
            await runtime.controller.async_wait_idle()
            await hass.async_block_till_done()
        assert len(calls) == 5
        assert runtime.values["control_status"] == "write_failed"
        assert "retry:registry-1" not in runtime.timers
        # Current policy, not the stale earlier command, controls the next series.
        hass.config_entries.async_update_entry(
            runtime.entry,
            options={
                **runtime.entry.options,
                "minimum_control_temperature": 16.0,
                "fallback_heating_c": 16.0,
            },
        )
        runtime._mark_explicit_transition("comfort")
        runtime.async_request_snapshot()
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert len(calls) == 6
        assert calls[-1].data["temperature"] < calls[0].data["temperature"]
        assert runtime.values["command_delivery"]["registry-1"]["attempts"] == 1
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("manual", [False, True])
async def test_pending_target_unavailable_and_return_never_create_manual_override(
    hass: HomeAssistant,
    manual: bool,
) -> None:
    runtime, calls = await _start_delivery_runtime(hass)
    try:
        assert len(calls) == 1
        if manual:
            runtime._transition("registry-1", OwnershipEvent.EXTERNAL_TARGET)
        old = hass.states.get("climate.target")
        assert old is not None
        hass.states.async_set("climate.target", "unavailable")
        await hass.async_block_till_done()
        assert runtime.broker is not None
        assert runtime.broker.state_counts("registry-1")[0] == 0
        assert runtime.ownership["registry-1"].external_revision == int(manual)
        assert runtime.ownership["registry-1"].ownership is (
            Ownership.MANUAL_OVERRIDE if manual else Ownership.OWNED
        )
        assert not any(f"{prefix}:registry-1" in runtime.timers for prefix in ("ack", "retry"))
        hass.states.async_set("climate.target", "heat", old.attributes)
        await hass.async_block_till_done()
        assert runtime.ownership["registry-1"].external_revision == int(manual)
        runtime.async_request_snapshot()
        assert runtime.controller is not None
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()
        assert runtime.ownership["registry-1"].ownership is (
            Ownership.MANUAL_OVERRIDE if manual else Ownership.OWNED
        )
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("expired", [False, True])
async def test_restart_restores_manual_override_until_its_original_expiry(
    hass: HomeAssistant,
    expired: bool,
) -> None:
    expiry = dt_util.utcnow() + timedelta(minutes=-1 if expired else 60)
    actuator = StoredActuator(
        "registry-1",
        "manual_override",
        4,
        1,
        "external_temperature_target",
        expiry.isoformat(),
        False,
        None,
        None,
        None,
        None,
        None,
    )
    stored = ControlStoreState(2, "prior", False, "old", "balanced", (actuator,))
    runtime, calls = await _start_delivery_runtime(hass, stored=stored)
    try:
        assert runtime.ownership["registry-1"].ownership is (
            Ownership.OWNED if expired else Ownership.MANUAL_OVERRIDE
        )
        if not expired:
            assert calls == []
            assert runtime.ownership["registry-1"].override_expiry == expiry
            assert "override:registry-1" in runtime.timers
        else:
            assert len(calls) == 1
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("legacy_fault", [False, True])
async def test_restart_with_uncertain_write_reconciles_without_replaying_or_resume(
    hass: HomeAssistant,
    legacy_fault: bool,
) -> None:
    command = StoredCommand(
        "obsolete",
        "registry-1",
        "old-high-target",
        "old-context",
        1,
        1,
        1,
        1,
        "2026-09-11T12:00:00+00:00",
        "2026-09-11T12:05:00+00:00",
        True,
    )
    actuator = StoredActuator(
        "registry-1",
        "command_fault" if legacy_fault else "owned",
        4,
        0,
        "command_outcome_unknown" if legacy_fault else None,
        None,
        legacy_fault,
        None,
        "obsolete",
        "old-high-target",
        "old-context",
        command,
    )
    runtime, calls = await _start_delivery_runtime(
        hass,
        outcome="ack",
        stored=ControlStoreState(2, "prior", False, "old", "balanced", (actuator,)),
    )
    try:
        assert len(calls) == 1
        assert calls[0].data["temperature"] == 18.0
        assert calls[0].context.id != "old-context"
        assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
        assert not runtime.ownership["registry-1"].resume_required
        assert runtime.values["command_delivery"]["registry-1"]["status"] == "acknowledged"
    finally:
        await runtime.async_unload()


def _runtime(*, entry_id: str = "entry-1", target_identity: str = "registry-1") -> ZoneRuntime:
    entry = SimpleNamespace(
        title="Zone",
        entry_id=entry_id,
        data={
            "zone_uuid": "zone-1",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": target_identity,
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_update_entry=MagicMock()))
    return ZoneRuntime(cast(Any, hass), cast(Any, entry), "zone-1", "balanced", "off", False)


async def test_runtime_keeps_diagnostic_history_in_the_calculation_snapshot(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.controller = MagicMock()
    runtime.history_collector = cast(
        Any,
        SimpleNamespace(
            result=lambda **kwargs: RunningMeanResult(
                10.0,
                HistoryQuality.DIAGNOSTIC,
                0,
                0.0,
                None,
                ("history_not_control_eligible", "history_limited_coverage"),
            )
        ),
    )
    monkeypatch.setattr(ZoneRuntime, "_schedule_freshness_expiries", lambda *_args: None)
    runtime.async_request_snapshot()
    captured = runtime.controller.request.call_args.args[0]
    assert captured.running_mean_c == 10.0
    assert captured.history_quality == HistoryQuality.DIAGNOSTIC.value
    assert "history_limited_coverage" in captured.context_reasons


def test_diagnostic_history_is_visible_even_with_hvac_off_without_changing_control() -> None:
    snapshot = _captured(load_scenarios()[0])
    target = snapshot.targets[0]
    snapshot = replace(
        snapshot,
        history_quality=HistoryQuality.DIAGNOSTIC.value,
        targets=(replace(target, capability=replace(target.capability, hvac_mode="off")),),
    )
    result = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity=target.registry_identity)
    visible = runtime._observable_values(result)
    assert visible["outdoor_running_mean"] is not None
    assert visible["thermal_sensation"] is not None
    assert visible["lower_comfort_boundary"] is not None
    assert visible["thermal_neutral"] is not None
    assert visible["upper_comfort_boundary"] is not None
    assert visible["data_quality"] == "diagnostic_estimate"
    assert visible["input_status"] == "history_not_control_eligible"
    assert runtime.last_valid_values == {}
    numerical = result.targets[0].result
    assert numerical is not None
    assert numerical.policy is not None
    assert numerical.policy.fallback
    assert result.targets[0].suppression_reason == "hvac_off"
    assert not runtime.control_enabled


async def test_diagnostic_estimate_clears_missing_history_repair_without_claiming_full_history(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    updates: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: updates.append((name, active)))
    )
    runtime.transition_logger = MagicMock()
    snapshot = replace(
        _captured(load_scenarios()[0]), history_quality=HistoryQuality.DIAGNOSTIC.value
    )
    runtime._update_observability_conditions(calculate_runtime_snapshot(snapshot))
    assert ("missing_history_24h", False) in updates
    assert "missing_history_24h" not in runtime.repair_condition_started


def test_heating_demand_buffer_uses_actuator_grid_and_stateful_thresholds() -> None:
    calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = calculation.targets[0]
    assert target.result is not None
    assert isinstance(target.result.normalized, NormalizedScalarTarget)
    desired = target.result.normalized
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 30.0,
            "target_rounding_mode": "mathematical",
            "heating_demand_activation_delta_c": 0.5,
            "heating_demand_deactivation_delta_c": 0.1,
        }
    )

    below_activation = replace(calculation, primary_value_c=desired.normalized_room_c - 0.3)
    idle, transitions = runtime._apply_heating_demand_hysteresis(below_activation)
    idle_normalized = idle.targets[0].result
    assert idle_normalized is not None
    assert isinstance(idle_normalized.normalized, NormalizedScalarTarget)
    assert idle_normalized.normalized.normalized_room_c <= below_activation.primary_value_c
    assert idle_normalized.normalized.normalized_room_c == pytest.approx(
        desired.normalized_room_c - 0.5
    )
    assert transitions == {}
    assert runtime.heating_demand_details[target.target_uuid]["state"] == "idle"
    assert idle_normalized.occupied_normalized == target.result.occupied_normalized

    at_activation = replace(calculation, primary_value_c=desired.normalized_room_c - 0.5)
    active, transitions = runtime._apply_heating_demand_hysteresis(at_activation)
    assert active.targets[0].result is not None
    assert active.targets[0].result.normalized == desired
    assert transitions == {target.registry_identity: False}

    held, transitions = runtime._apply_heating_demand_hysteresis(
        replace(calculation, primary_value_c=desired.normalized_room_c - 0.2)
    )
    assert held.targets[0].result is not None
    assert held.targets[0].result.normalized == desired
    assert transitions == {}

    released, transitions = runtime._apply_heating_demand_hysteresis(
        replace(calculation, primary_value_c=desired.normalized_room_c - 0.1)
    )
    assert released.targets[0].result is not None
    assert isinstance(released.targets[0].result.normalized, NormalizedScalarTarget)
    assert released.targets[0].result.normalized.normalized_room_c <= released.primary_value_c
    assert transitions == {target.registry_identity: True}


def test_heating_demand_buffer_preserves_range_high_and_boost_bypasses_idle() -> None:
    ranged_scenario = next(
        scenario for scenario in load_scenarios() if scenario["scenario_id"] == "VI-018"
    )
    ranged = calculate_runtime_snapshot(_captured(ranged_scenario))
    target = ranged.targets[0]
    assert target.result is not None
    assert isinstance(target.result.normalized, NormalizedRangeTarget)
    desired = target.result.normalized
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.entry.options.update(
        {"minimum_control_temperature": 16.0, "maximum_control_temperature": 30.0}
    )

    idle, _ = runtime._apply_heating_demand_hysteresis(
        replace(ranged, primary_value_c=desired.heating.normalized_room_c - 0.3)
    )
    assert idle.targets[0].result is not None
    assert isinstance(idle.targets[0].result.normalized, NormalizedRangeTarget)
    assert idle.targets[0].result.normalized.cooling == desired.cooling

    scalar = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    scalar_target = scalar.targets[0]
    assert scalar_target.result is not None
    assert isinstance(scalar_target.result.normalized, NormalizedScalarTarget)
    assert scalar_target.result.policy is not None
    boosted_result = replace(
        scalar_target.result,
        policy=replace(scalar_target.result.policy, boost_mode=BoostMode.ADAPTIVE),
    )
    boosted = replace(
        scalar,
        primary_value_c=scalar_target.result.normalized.normalized_room_c - 0.1,
        targets=(replace(scalar_target, result=boosted_result),),
    )
    runtime = _runtime(target_identity=scalar_target.registry_identity)
    transformed, _ = runtime._apply_heating_demand_hysteresis(boosted)
    assert transformed.targets[0].result is not None
    assert transformed.targets[0].result.normalized == scalar_target.result.normalized
    assert runtime.heating_demand_details[scalar_target.target_uuid]["reason"] == "boost_override"


def test_heating_demand_buffer_does_not_change_target_for_stale_primary() -> None:
    calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = calculation.targets[0]
    assert target.result is not None
    runtime = _runtime(target_identity=target.registry_identity)

    transformed, transitions = runtime._apply_heating_demand_hysteresis(
        replace(calculation, primary_temperature_stale=True)
    )

    assert transformed.targets[0].result is not None
    assert transformed.targets[0].result.normalized == target.result.normalized
    assert transitions == {}
    assert (
        runtime.heating_demand_details[target.target_uuid]["reason"]
        == "primary_temperature_not_fresh"
    )


def test_heating_demand_buffer_explains_suppression_and_fails_closed_without_idle() -> None:
    calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = calculation.targets[0]
    assert target.result is not None
    assert isinstance(target.result.normalized, NormalizedScalarTarget)
    runtime = _runtime(target_identity=target.registry_identity)

    suppressed, transitions = runtime._apply_heating_demand_hysteresis(
        replace(calculation, targets=(replace(target, suppression_reason="manual_override"),))
    )
    assert suppressed.targets[0].suppression_reason == "manual_override"
    assert transitions == {}
    assert runtime.heating_demand_details[target.target_uuid]["reason"] == "manual_override"

    runtime.entry.options.update(
        {"minimum_control_temperature": 30.0, "maximum_control_temperature": 30.0}
    )
    no_idle, transitions = runtime._apply_heating_demand_hysteresis(
        replace(
            calculation,
            primary_value_c=target.result.normalized.normalized_room_c - 0.2,
        )
    )
    assert no_idle.targets[0].suppression_reason == "heating_demand_idle_target_unavailable"
    assert transitions == {}
    assert runtime.heating_demand_details[target.target_uuid]["reason"] == "idle_target_unavailable"


async def test_heating_demand_transition_is_persisted_before_dispatch_or_rolled_back(
    hass: HomeAssistant,
) -> None:
    calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = calculation.targets[0]
    assert target.result is not None
    assert isinstance(target.result.normalized, NormalizedScalarTarget)
    active_result = replace(
        calculation,
        primary_value_c=target.result.normalized.normalized_room_c - 0.5,
    )

    class Persistence:
        def __init__(self, saved: bool) -> None:
            self.saved = saved
            self.payloads: list[str] = []

        async def async_update_heating_demand_states(self, payload: str) -> bool:
            self.payloads.append(payload)
            return self.saved

    class Broker:
        async def async_submit(self, _intent: Any, *, now: datetime) -> CommandOutcome:
            raise AssertionError("suppressed test target must not dispatch")

    for saved in (True, False):
        runtime = _runtime(target_identity=target.registry_identity)
        runtime.hass = hass
        runtime.control_enabled = True
        runtime.broker = cast(Any, Broker())
        runtime.persistence = cast(Any, Persistence(saved))
        runtime.heating_demand_active[target.registry_identity] = True
        suppressed = replace(
            active_result,
            targets=(replace(target, suppression_reason="manual_override"),),
        )
        await runtime._async_apply_calculation(
            suppressed,
            demand_transitions={target.registry_identity: None},
            demand_states_json=f'{{"{target.registry_identity}":true}}',
        )
        assert runtime.persistence.payloads == [f'{{"{target.registry_identity}":true}}']
        if saved:
            assert runtime.heating_demand_active[target.registry_identity] is True
            assert runtime.values["command_outcomes"][target.target_uuid] == "manual_override"
        else:
            assert target.registry_identity not in runtime.heating_demand_active
            assert (
                runtime.values["command_outcomes"][target.target_uuid]
                == "heating_demand_storage_fault"
            )

    runtime = _runtime(target_identity=target.registry_identity)
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.broker = cast(Any, Broker())
    runtime.persistence = cast(Any, Persistence(False))
    runtime.heating_demand_active[target.registry_identity] = True
    await runtime._async_apply_calculation(
        suppressed,
        demand_transitions={target.registry_identity: False},
        demand_states_json=f'{{"{target.registry_identity}":true}}',
    )
    assert runtime.heating_demand_active[target.registry_identity] is False

    runtime = _runtime(target_identity=target.registry_identity)
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.broker = cast(Any, Broker())
    runtime.persistence = cast(Any, Persistence(False))
    runtime.heating_demand_active[target.registry_identity] = False
    await runtime._async_apply_calculation(
        suppressed,
        demand_transitions={target.registry_identity: None},
        demand_states_json=f'{{"{target.registry_identity}":true}}',
    )
    assert runtime.heating_demand_active[target.registry_identity] is False


def test_ownership_trace_payload_serializes_override_expiry() -> None:
    """Decision traces keep strict JSON values when a manual override is timed."""
    expiry = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    state = OwnershipState(
        target_identity="registry-1",
        ownership=Ownership.MANUAL_OVERRIDE,
        override_expiry=expiry,
    )

    payload = runtime_module._ownership_trace_payload(state)

    assert payload["override_expiry"] == expiry.isoformat()
    runtime = _runtime()
    trace = runtime.trace_ring.add(
        generation=1,
        payload={"ownership": {"registry-1": payload}},
    )
    assert trace is not None


def test_runtime_callbacks_boost_resume_and_lightweight_strategy() -> None:
    runtime = _runtime()
    updates: list[dict[str, Any]] = []
    remove = runtime.subscribe(lambda: updates.append(dict(runtime.values)))
    runtime.publish({"thermal_sensation": 0.1})
    assert updates[-1]["thermal_sensation"] == 0.1
    asyncio.run(runtime.async_set_boost_mode("adaptive"))
    assert runtime.boost_mode == "adaptive"
    asyncio.run(runtime.async_resume())
    assert runtime.values["resume_requested"] is True
    asyncio.run(runtime.async_set_strategy("comfort"))
    assert runtime.strategy == "comfort"
    calls = runtime.hass.config_entries.async_update_entry.call_count
    asyncio.run(runtime.async_set_strategy("comfort"))
    assert runtime.hass.config_entries.async_update_entry.call_count == calls
    remove()
    runtime.publish({"after_remove": True})
    assert updates[-1].get("after_remove") is None


def test_runtime_resume_resolves_broker_pending_before_reconciliation() -> None:
    runtime = _runtime()
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.COMMAND_FAULT,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
        resume_required=True,
    )

    class Broker:
        def __init__(self) -> None:
            self.resumed: list[str] = []

        async def async_resume_target(self, identity: str) -> None:
            self.resumed.append(identity)

        def invalidate(self, _identity: str) -> None:
            return None

    broker = Broker()
    runtime.broker = cast(Any, broker)
    asyncio.run(runtime.async_resume())

    assert broker.resumed == ["registry-1"]
    assert runtime.ownership["registry-1"].ownership is Ownership.RECONCILING
    assert runtime.ownership["registry-1"].resume_required is False


def test_control_status_keeps_ownership_target_and_data_health_separate() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.HOLD_LAST_GOOD,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    assert runtime._control_status() == "hold_last_good"
    runtime.ownership["registry-1"] = replace(
        runtime.ownership["registry-1"],
        data_readiness=DataReadiness.READY,
        target_readiness=TargetReadiness.SUSPENDED_MODE,
    )
    assert runtime._control_status() == "suspended_mode"
    runtime.ownership["registry-1"] = replace(
        runtime.ownership["registry-1"],
        ownership=Ownership.MANUAL_OVERRIDE,
    )
    assert runtime._control_status() == "manual_override"


@pytest.mark.parametrize(
    ("ownership", "data_readiness", "target_readiness", "expected"),
    [
        (
            Ownership.COMMAND_FAULT,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "command_fault",
        ),
        (
            Ownership.RECONCILING,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "reconciling",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.UNAVAILABLE,
            "unavailable",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.INCOMPATIBLE,
            "incompatible",
        ),
        (
            Ownership.OWNED,
            DataReadiness.INVALID,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "invalid",
        ),
        (
            Ownership.OWNED,
            DataReadiness.FALLBACK_READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "fallback_ready",
        ),
        (
            Ownership.OWNED,
            DataReadiness.DEGRADED_READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "degraded_ready",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "ready",
        ),
    ],
)
def test_control_status_exposes_each_safety_state(
    ownership: Ownership,
    data_readiness: DataReadiness,
    target_readiness: TargetReadiness,
    expected: str,
) -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", ownership, data_readiness, target_readiness
    )
    assert runtime._control_status() == expected


def test_control_status_is_unknown_without_a_registered_target() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    assert runtime._control_status() == "unknown"
    runtime.stale_safety_applied.add("registry-1")
    assert runtime._control_status() == "stale_safety"


def test_ownership_transition_is_published_without_waiting_for_calculation() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        target_identity="registry-1",
        ownership=Ownership.OWNED,
        data_readiness=DataReadiness.READY,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.values = {
        "control_status": "owned",
        "control_eligible": True,
        "resume_required": False,
    }
    runtime._transition("registry-1", OwnershipEvent.DISABLE)

    assert runtime.values["control_status"] == "ready"
    assert runtime.values["control_eligible"] is False
    assert runtime.values["resume_required"] is False
    assert runtime.values["ownership"] == {"registry-1": "disabled"}


def test_source_noise_is_coalesced_but_cumulative_and_availability_changes_are_material() -> None:
    runtime = _runtime()
    runtime.entry.data["primary_temperature"] = "sensor.room"
    attributes = {"unit_of_measurement": "°C"}

    assert runtime._source_report_is_material("sensor.room", State("sensor.room", "20", attributes))
    assert not runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "20.02", attributes)
    )
    assert not runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "20.04", attributes)
    )
    assert runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "20.06", attributes)
    )
    assert runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "unavailable", attributes)
    )
    assert not runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "unavailable", attributes)
    )
    assert runtime._source_report_is_material(
        "sensor.room", State("sensor.room", "20.07", attributes)
    )
    assert runtime.suppressed_source_reports == 3


def test_relative_humidity_uses_a_bounded_half_percent_deadband() -> None:
    runtime = _runtime()
    runtime.entry.data["rh_entity"] = "sensor.rh"
    attributes = {"unit_of_measurement": "%"}

    assert runtime._source_report_is_material("sensor.rh", State("sensor.rh", "50", attributes))
    assert not runtime._source_report_is_material(
        "sensor.rh", State("sensor.rh", "50.4", attributes)
    )
    assert runtime._source_report_is_material("sensor.rh", State("sensor.rh", "50.6", attributes))


def test_source_noise_classifies_all_configured_source_shapes(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.data.update(
        {
            "primary_temperature": "climate.room",
            "rh_entity": "sensor.rh",
            "outdoor_source": "sensor.outdoor",
        }
    )
    runtime.entry.options.update(
        {
            "air_speed_entity": "sensor.air_speed",
            "globe_temperature_entity": "sensor.globe",
            "surface_temperature_entity": "sensor.surface",
            "mold_indicator_entity": "sensor.mold",
        }
    )

    assert runtime._source_kind("climate.room") is SourceKind.PRIMARY_AIR
    assert runtime._source_kind("sensor.rh") is SourceKind.RELATIVE_HUMIDITY
    assert runtime._source_kind("sensor.outdoor") is SourceKind.OUTDOOR
    assert runtime._source_kind("sensor.air_speed") is SourceKind.AIR_SPEED
    assert runtime._source_kind("sensor.globe") is SourceKind.GLOBE
    assert runtime._source_kind("sensor.surface") is SourceKind.SURFACE
    assert runtime._source_kind("sensor.mold") is SourceKind.DIRECT_MRT
    assert runtime._source_numeric_value("sensor.none", None, SourceKind.PRIMARY_AIR) is None
    assert runtime._source_numeric_value(
        "climate.room",
        State("climate.room", "heat", {"current_temperature": 21.25}),
        SourceKind.PRIMARY_AIR,
    ) == pytest.approx(21.25)
    assert runtime._source_numeric_value(
        "sensor.mold",
        State("sensor.mold", "75", {"estimated_critical_temp": 16.75}),
        SourceKind.DIRECT_MRT,
    ) == pytest.approx(16.75)


def test_stale_primary_keeps_last_valid_outputs_visible_and_labelled() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")

    current = runtime._observable_values(valid)
    runtime.values = current
    assert current["thermal_sensation"] is not None
    assert current["data_quality"] == "current"

    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    held = runtime._observable_values(stale)

    assert stale.hold_condition == "primary_rh_stale"
    assert held["thermal_sensation"] == current["thermal_sensation"]
    assert held["lower_comfort_boundary"] == current["lower_comfort_boundary"]
    assert held["heating_control_target"] == current["heating_control_target"]
    assert held["upper_comfort_boundary"] == current["upper_comfort_boundary"]
    assert held["root_sensation_votes"] == current["root_sensation_votes"]
    assert held["effective_targets"] == current["effective_targets"]
    assert held["target_scenarios"] == current["target_scenarios"]
    assert held["input_status"] == "primary_rh_stale"
    assert held["data_quality"] == "stale"
    assert all(
        detail["stale"] is True and detail["mode"] == "stale_hold"
        for detail in held["effective_target_details"].values()
    )

    runtime.stale_safety_applied.add("registry-climate-living-room")
    runtime.values["effective_targets"] = {"target-living-room": {"temperature": 18.0}}
    runtime.values["effective_target_details"] = {"target-living-room": {"mode": "stale_safety"}}
    guarded = runtime._observable_values(stale)
    assert guarded["effective_targets"] == {"target-living-room": {"temperature": 18.0}}
    assert guarded["effective_target_details"] == {"target-living-room": {"mode": "stale_safety"}}


def test_stale_secondary_source_cannot_replace_fully_valid_snapshot() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")
    current = runtime._observable_values(valid)
    saved_values = dict(runtime.last_valid_values)
    saved_at = runtime.last_valid_at
    assert snapshot.primary is not None
    assert snapshot.relative_humidity is not None

    stale_now = snapshot.now + timedelta(hours=1)
    stale_rh = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=stale_now,
            primary=replace(snapshot.primary, observed_at=stale_now),
            source_states=valid.source_states,
        )
    )
    held = runtime._observable_values(stale_rh)

    assert stale_rh.hold_condition == "primary_rh_stale"
    assert runtime.last_valid_values == saved_values
    assert runtime.last_valid_at == saved_at
    assert held["target_scenarios"] == current["target_scenarios"]
    assert held["data_quality"] == "stale"


def test_stale_primary_projects_targets_and_heat_guard_uses_one_trial() -> None:
    scenario = load_scenarios()[0]
    baseline = _captured(scenario)
    assert baseline.primary is not None
    assert baseline.relative_humidity is not None
    initial = replace(baseline, primary=replace(baseline.primary, raw_state="18.0"))
    accepted = calculate_runtime_snapshot(initial)
    stale_at = initial.now + timedelta(hours=8)
    stale = calculate_runtime_snapshot(
        replace(
            initial,
            now=stale_at,
            relative_humidity=replace(initial.relative_humidity, observed_at=stale_at),
            source_states=accepted.source_states,
        )
    )
    assert stale.primary_temperature_stale
    assert stale.hold_condition is None
    target = stale.targets[0]
    assert target.result is not None
    assert target.result.normalized is not None
    assert target.mapping is not None
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.entry.options.update({"minimum_control_temperature": 16.0, "fallback_heating_c": 16.0})
    runtime.primary_feedback_at = initial.now

    normal, _changed = runtime._guard_heating_target(
        target, target.result.normalized, target.mapping, stale, stale_at
    ) or (None, False)
    assert normal == target.result.normalized
    assert runtime.heat_guards[target.registry_identity].phase.value == "stale_start"
    runtime._guard_heating_target(
        target, target.result.normalized, target.mapping, stale, stale_at + timedelta(minutes=60)
    )
    assert runtime.heat_guards[target.registry_identity].phase.value == "ramp"
    withdrawn, _changed = runtime._guard_heating_target(
        target, target.result.normalized, target.mapping, stale, stale_at + timedelta(minutes=70)
    ) or (None, False)
    assert withdrawn is not None
    assert withdrawn.normalized_room_c < target.result.normalized.normalized_room_c
    runtime._guard_heating_target(
        target, target.result.normalized, target.mapping, stale, stale_at + timedelta(hours=2)
    )
    assert runtime.heat_guards[target.registry_identity].phase.value == "exhausted"
    runtime._guard_heating_target(
        target, target.result.normalized, target.mapping, stale, stale_at + timedelta(hours=3)
    )
    assert runtime.heat_guards[target.registry_identity].phase.value == "exhausted"


async def test_stale_start_and_ramp_send_broker_intents_only(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    assert snapshot.primary is not None
    assert snapshot.relative_humidity is not None
    snapshot = replace(snapshot, primary=replace(snapshot.primary, raw_state="18.0"))
    accepted = calculate_runtime_snapshot(snapshot)
    stale_at = snapshot.now + timedelta(hours=8)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=stale_at,
            relative_humidity=replace(snapshot.relative_humidity, observed_at=stale_at),
            source_states=accepted.source_states,
        )
    )
    target = stale.targets[0]
    assert target.result is not None
    assert target.result.normalized is not None
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update({"minimum_control_temperature": 16.0, "fallback_heating_c": 16.0})
    runtime.primary_feedback_at = snapshot.now
    runtime.ownership[target.registry_identity] = OwnershipState(
        target.registry_identity,
        Ownership.OWNED,
        DataReadiness.DEGRADED_READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations[target.registry_identity] = 1

    class Persistence:
        async def async_update_heat_guards(self, _serialized: str) -> bool:
            return True

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "guarded",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.persistence = cast(Any, Persistence())
    runtime.broker = cast(Any, broker)
    await runtime._async_apply_calculation(stale)
    assert len(broker.intents) == 1
    assert broker.intents[0].temperature_ha == target.result.normalized.normalized_ha
    assert broker.intents[0].safety_deescalation is False
    guard = runtime.heat_guards[target.registry_identity]
    assert guard.phase.value == "stale_start"
    runtime.heat_guards[target.registry_identity] = replace(
        guard,
        phase=type(guard.phase).RAMP,
        ramp_at=dt_util.utcnow() - timedelta(minutes=10),
        ramp_start_c=target.result.normalized.normalized_room_c,
    )
    current_capability = replace(
        target.capability, scalar_target_ha=target.result.normalized.normalized_ha
    )
    ramp_result = replace(stale, targets=(replace(target, capability=current_capability),))
    await runtime._async_apply_calculation(ramp_result)
    assert len(broker.intents) == 2
    assert broker.intents[1].temperature_ha < broker.intents[0].temperature_ha
    assert broker.intents[1].safety_deescalation is True
    for cancel in runtime.timers.values():
        cancel()


async def test_stale_room_without_heat_demand_still_updates_climate_target(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    assert snapshot.primary is not None
    assert snapshot.relative_humidity is not None
    snapshot = replace(snapshot, primary=replace(snapshot.primary, raw_state="24.0"))
    accepted = calculate_runtime_snapshot(snapshot)
    stale_at = snapshot.now + timedelta(hours=8)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=stale_at,
            relative_humidity=replace(snapshot.relative_humidity, observed_at=stale_at),
            source_states=accepted.source_states,
        )
    )
    assert stale.primary_temperature_stale
    assert stale.targets[0].result is not None
    assert stale.targets[0].result.normalized is not None
    target = stale.targets[0]
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.primary_feedback_at = snapshot.now
    runtime.ownership[target.registry_identity] = OwnershipState(
        target.registry_identity,
        Ownership.OWNED,
        DataReadiness.DEGRADED_READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations[target.registry_identity] = 1

    class Persistence:
        async def async_update_heat_guards(self, _serialized: str) -> bool:
            return True

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "updated",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.persistence = cast(Any, Persistence())
    runtime.broker = cast(Any, broker)
    await runtime._async_apply_calculation(stale)
    assert len(broker.intents) == 1
    assert broker.intents[0].temperature_ha == target.result.normalized.normalized_ha
    assert runtime.heat_guards[target.registry_identity].phase.value == "idle"
    assert "heat_guard:" + target.registry_identity not in runtime.timers


async def test_unavailable_primary_with_existing_heat_demand_ramps_to_fallback(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 18.0,
        }
    )
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Persistence:
        async def async_update_heat_guards(self, _serialized: str) -> bool:
            return True

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "guarded",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.persistence = cast(Any, Persistence())
    runtime.broker = cast(Any, broker)
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    target = runtime_module.TargetCalculation(
        "target-1",
        "registry-1",
        "climate.target",
        runtime_module.capability_from_state(hass.states.get("climate.target")),
        None,
        None,
        "primary_temperature_invalid",
    )
    assert await runtime._async_guard_invalid_primary(target, now) == "dispatched"
    assert broker.intents[-1].safety_deescalation
    guard = runtime.heat_guards["registry-1"]
    assert guard.phase.value == "ramp"
    runtime.heat_guards["registry-1"] = replace(guard, ramp_at=now - timedelta(minutes=10))
    assert await runtime._async_guard_invalid_primary(target, now) == "dispatched"
    assert broker.intents[-1].temperature_ha < 22.0
    runtime.heat_guards["registry-1"] = replace(
        runtime.heat_guards["registry-1"], ramp_at=now - timedelta(minutes=30)
    )
    assert await runtime._async_guard_invalid_primary(target, now) == "dispatched"
    assert broker.intents[-1].temperature_ha == 18.0
    assert runtime.heat_guards["registry-1"].phase.value == "exhausted"


async def test_invalid_primary_ramps_range_heat_without_increasing_cooling(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 15.0,
            "maximum_control_temperature": 30.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 27.0,
        }
    )
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat_cool",
        {
            "hvac_modes": ["off", "heat_cool"],
            "supported_features": 2,
            "min_temp": 5.0,
            "max_temp": 35.0,
            "target_temp_step": 0.5,
            "target_temp_low": 22.0,
            "target_temp_high": 26.0,
            "unit_of_measurement": "°C",
        },
    )

    class Persistence:
        async def async_update_heat_guards(self, _serialized: str) -> bool:
            return True

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "range-ramp",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.persistence = cast(Any, Persistence())
    runtime.broker = cast(Any, broker)
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    target = runtime_module.TargetCalculation(
        "target-1",
        "registry-1",
        "climate.target",
        runtime_module.capability_from_state(hass.states.get("climate.target")),
        None,
        None,
        "primary_temperature_invalid",
    )
    assert await runtime._async_guard_invalid_primary(target, now) == "dispatched"
    assert broker.intents[-1].target_temp_low_ha == 22.0
    runtime.heat_guards["registry-1"] = replace(
        runtime.heat_guards["registry-1"], ramp_at=now - timedelta(minutes=30)
    )
    assert await runtime._async_guard_invalid_primary(target, now) == "dispatched"
    assert broker.intents[-1].target_temp_low_ha == 18.0
    assert broker.intents[-1].target_temp_high_ha == 26.0


async def test_invalid_primary_guard_refuses_unsafe_or_unpersisted_actions(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    now = dt_util.utcnow()
    empty = runtime_module.TargetCalculation(
        "target-1",
        "registry-1",
        "climate.target",
        runtime_module.capability_from_state(None),
        None,
        None,
        "primary_temperature_invalid",
    )
    assert await runtime._async_guard_invalid_primary(empty, now) is None
    runtime.broker = cast(Any, SimpleNamespace())
    assert await runtime._async_guard_invalid_primary(empty, now) is None
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            20.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    assert await runtime._async_guard_invalid_primary(empty, now) is None
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 20.0,
            "unit_of_measurement": "°C",
        },
    )
    target = replace(
        empty, capability=runtime_module.capability_from_state(hass.states.get("climate.target"))
    )
    assert await runtime._async_guard_invalid_primary(target, now) is None
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )
    assert (
        await runtime._async_guard_invalid_primary(target, now) == "stale_heat_guard_storage_fault"
    )
    hass.states.async_set(
        "climate.target",
        "off",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 22.0,
        },
    )
    assert await runtime._async_guard_invalid_primary(target, now) is None


async def test_invalid_primary_safety_uses_real_broker_and_lease(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def capture(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture)
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "fallback_heating_c": 18.0,
        }
    )
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Persistence:
        async def async_update_heat_guards(self, _guards: str) -> bool:
            return True

        async def async_persist_pending(self, _command: Any) -> bool:
            return True

        async def async_mark_dispatched(self, _command: Any) -> bool:
            return True

        async def async_resolve(self, _command: Any, _reason: str) -> bool:
            return True

    persistence = Persistence()
    runtime.persistence = cast(Any, persistence)
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime_module.get_lease_registry(hass).acquire("registry-1", runtime.entry.entry_id)
    runtime.broker = CommandBroker(
        service=HomeAssistantClimateService(hass),
        persistence=cast(Any, persistence),
        preflight=runtime._broker_preflight,
        command_id_factory=lambda: "stale-guard-command",
        context_factory=runtime._context_token,
    )
    target = runtime_module.TargetCalculation(
        "target-1",
        "registry-1",
        "climate.target",
        runtime_module.capability_from_state(hass.states.get("climate.target")),
        None,
        None,
        "primary_temperature_invalid",
    )
    outcome = await runtime._async_guard_invalid_primary(target, now)
    for cancel in runtime.timers.values():
        cancel()
    assert outcome == "awaiting_acknowledgement"
    assert len(calls) == 1
    assert calls[0].data["temperature"] == 22.0
    assert calls[0].data["entity_id"] == "climate.target"
    assert runtime.heat_guards["registry-1"].phase.value == "ramp"


def test_persisted_last_valid_outputs_are_used_after_runtime_reload() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    prior = _runtime(target_identity="registry-climate-living-room")
    prior_values = prior._observable_values(valid)

    reloaded = _runtime(target_identity="registry-climate-living-room")
    reloaded.last_valid_values = dict(prior.last_valid_values)
    reloaded.last_valid_at = prior.last_valid_at
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )

    restored = reloaded._observable_values(stale)

    assert restored["thermal_sensation"] == prior_values["thermal_sensation"]
    assert restored["effective_targets"] == prior_values["effective_targets"]
    assert restored["data_quality"] == "stale"
    assert restored["input_status"] == "primary_rh_stale"


async def test_changed_last_valid_output_is_persisted_once() -> None:
    class Persistence:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def async_update_last_valid_output(
            self, *, values_json: str, observed_at: str
        ) -> bool:
            self.calls.append((values_json, observed_at))
            return True

    runtime = _runtime()
    runtime.hass.async_create_task = asyncio.create_task
    persistence = Persistence()
    runtime.persistence = cast(Any, persistence)
    runtime.last_valid_values = {"thermal_sensation": -0.2}
    runtime.last_valid_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    runtime._schedule_last_valid_persistence()
    await asyncio.gather(*tuple(runtime.background_tasks))
    runtime._schedule_last_valid_persistence()

    assert persistence.calls == [('{"thermal_sensation":-0.2}', "2026-09-13T12:00:00+00:00")]


async def test_failed_last_valid_output_persistence_can_retry() -> None:
    class Persistence:
        async def async_update_last_valid_output(self, **_kwargs: Any) -> bool:
            return False

    runtime = _runtime()
    runtime.persistence = cast(Any, Persistence())
    runtime.last_valid_persistence_payload = '{"thermal_sensation":-0.2}'

    await runtime._async_persist_last_valid_output(
        runtime.last_valid_persistence_payload,
        datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )

    assert runtime.last_valid_persistence_payload is None


async def test_stale_safety_uses_configured_fallback_without_hvac_command(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "control_enabled": True,
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 17.5,
            "fallback_cooling_c": 26.0,
        }
    )
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.HOLD_LAST_GOOD,
        TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.capability_generations["registry-1"] = 1
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=1, minutes=1),
            now - timedelta(hours=1, minutes=1),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        ),
        recovering=True,
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "safety-command",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.broker = cast(Any, broker)

    await runtime._async_apply_stale_safety()

    # Heating is now managed by the feedback guard, never the legacy cooling path.
    assert broker.intents == []

    await runtime._async_apply_stale_safety()
    assert broker.intents == []


async def test_stale_safety_runs_through_real_broker_to_exact_service_payload(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def capture(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture)
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 26.0,
        }
    )
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime_module.get_lease_registry(hass).acquire("registry-1", runtime.entry.entry_id)
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Persistence:
        async def async_persist_pending(self, _command: Any) -> bool:
            return True

        async def async_mark_dispatched(self, _command: Any) -> bool:
            return True

        async def async_resolve(self, _command: Any, _reason: str) -> bool:
            return True

    runtime.broker = CommandBroker(
        service=HomeAssistantClimateService(hass),
        persistence=Persistence(),
        preflight=runtime._broker_preflight,
        command_id_factory=lambda: "stale-safety-command",
        context_factory=runtime._context_token,
    )

    await runtime._async_apply_stale_safety()

    assert calls == []
    for cancel in runtime.timers.values():
        cancel()


async def test_stale_safety_reports_unready_and_unneeded_targets(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.data["targets"] = [
        {
            "target_uuid": "unready",
            "entity_id": "climate.unready",
            "registry_identity": "registry-unready",
        },
        {
            "target_uuid": "settled",
            "entity_id": "climate.settled",
            "registry_identity": "registry-settled",
        },
    ]
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set("climate.unready", "unavailable")
    hass.states.async_set(
        "climate.settled",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 18.0,
            "unit_of_measurement": "°C",
        },
    )

    class UnusedBroker:
        async def async_submit(self, _intent: Any, *, now: datetime) -> CommandOutcome:
            raise AssertionError("no safety command expected")

    runtime.broker = cast(Any, UnusedBroker())
    await runtime._async_apply_stale_safety()

    assert runtime.values["command_outcomes"] == {
        "unready": "stale_safety_target_not_ready",
    }
    assert runtime.values["stale_safety_active"] is False


async def test_stale_cooling_safety_uses_broker_and_never_lowers_cooling_target(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update({"fallback_cooling_c": 27.0, "maximum_control_temperature": 30.0})
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            29.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "cool",
        {
            "hvac_modes": ["off", "cool"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 35.0,
            "target_temp_step": 0.5,
            "temperature": 23.0,
            "unit_of_measurement": "°C",
        },
    )

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "cooling-safety",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.broker = cast(Any, broker)
    await runtime._async_apply_stale_safety()
    assert len(broker.intents) == 1
    assert broker.intents[0].temperature_ha >= 27.0
    assert broker.intents[0].safety_deescalation
    assert runtime.stale_safety_applied == {"registry-1"}
    await runtime._async_apply_stale_safety()
    assert len(broker.intents) == 1


async def test_stale_range_cooling_safety_preserves_heat_endpoint(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 15.0,
            "maximum_control_temperature": 30.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 27.0,
        }
    )
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            29.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat_cool",
        {
            "hvac_modes": ["off", "heat_cool"],
            "supported_features": 2,
            "min_temp": 5.0,
            "max_temp": 35.0,
            "target_temp_step": 0.5,
            "target_temp_low": 20.0,
            "target_temp_high": 24.0,
            "unit_of_measurement": "°C",
        },
    )

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "range-safety",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.broker = cast(Any, broker)
    await runtime._async_apply_stale_safety()
    assert len(broker.intents) == 1
    assert broker.intents[0].target_temp_low_ha <= 20.0
    assert broker.intents[0].target_temp_high_ha >= 27.0
    assert broker.intents[0].safety_deescalation
    assert runtime.stale_safety_applied == {"registry-1"}
    await runtime._async_apply_stale_safety()
    assert len(broker.intents) == 1


async def test_stale_safety_requires_control_broker_and_old_accepted_value(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    await runtime._async_apply_stale_safety()

    runtime.control_enabled = True
    runtime.broker = cast(Any, SimpleNamespace())
    await runtime._async_apply_stale_safety()

    assert runtime.values == {}


def test_stale_safety_can_recover_timestamped_sensor_evidence_after_reload() -> None:
    runtime = _runtime()
    observed_at = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    state = State(
        "sensor.room",
        "19.25",
        {"unit_of_measurement": "°C"},
        last_changed=observed_at,
        last_reported=observed_at,
        last_updated=observed_at,
    )
    runtime.entry.data["primary_temperature"] = "sensor.room"
    runtime.hass = cast(
        Any,
        SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state)),
    )

    assert runtime._primary_safety_reference() == (19.25, observed_at)


def test_stale_safety_never_increases_heating_demand(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 18.0,
        }
    )
    capability = ClimateCapabilitySnapshot(
        "heat",
        ("off", "heat"),
        1,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        18.0,
    )
    mapping = runtime_module.resolve_capability(capability)
    assert not isinstance(mapping, runtime_module.ClimateFailure)

    assert runtime._stale_safety_target("target-1", capability, mapping, 19.0) is None


def test_stale_safety_normalizes_cooling_and_range_away_from_demand(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 28.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 27.0,
            "minimum_range_gap": 1.0,
        }
    )
    cooling = ClimateCapabilitySnapshot(
        "cool",
        ("off", "cool"),
        1,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        21.0,
    )
    cooling_mapping = runtime_module.resolve_capability(cooling)
    assert not isinstance(cooling_mapping, runtime_module.ClimateFailure)
    cooling_result = runtime._stale_safety_target("target-1", cooling, cooling_mapping, 24.0)
    assert isinstance(cooling_result, NormalizedScalarTarget)
    assert cooling_result.normalized_actuator_c == 27.0

    ranged = ClimateCapabilitySnapshot(
        "heat_cool",
        ("off", "heat_cool"),
        2,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        None,
        21.0,
        24.0,
    )
    ranged_mapping = runtime_module.resolve_capability(ranged)
    assert not isinstance(ranged_mapping, runtime_module.ClimateFailure)
    ranged_result = runtime._stale_safety_target("target-1", ranged, ranged_mapping, 19.0)
    assert isinstance(ranged_result, NormalizedRangeTarget)
    assert ranged_result.heating.normalized_actuator_c == 18.0
    assert ranged_result.cooling.normalized_actuator_c == 27.0

    runtime._publish_stale_safety_target("target-1", ranged_result)
    assert runtime.values["effective_targets"] == {
        "target-1": {"target_low": 18.0, "target_high": 27.0}
    }
    assert (
        runtime._stale_safety_target(
            "target-1", replace(ranged, target_temp_low_ha=None), ranged_mapping, 19.0
        )
        is None
    )
    assert (
        runtime._stale_safety_target(
            "target-1", replace(cooling, scalar_target_ha=None), cooling_mapping, 24.0
        )
        is None
    )


def test_heat_guard_on_range_preserves_existing_cooling_endpoint(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 28.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 27.0,
        }
    )
    capability = ClimateCapabilitySnapshot(
        "heat_cool",
        ("off", "heat_cool"),
        2,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        None,
        21.0,
        24.0,
    )
    mapping = runtime_module.resolve_capability(capability)
    assert not isinstance(mapping, runtime_module.ClimateFailure)
    normal = runtime_module.normalize_range_target(
        requested_heating_room_c=22.0,
        requested_cooling_room_c=25.0,
        snapshot=capability,
        options=runtime_module.GridOptions(16.0, 28.0, 0.0),
    )
    assert isinstance(normal, NormalizedRangeTarget)
    target = runtime_module.TargetCalculation(
        "target-1", "registry-1", "climate.target", capability, mapping, None, None
    )
    now = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    runtime.primary_feedback_at = now - timedelta(hours=8)
    result = replace(
        calculate_runtime_snapshot(_captured(load_scenarios()[0])),
        primary_value_c=18.0,
        primary_temperature_stale=True,
    )
    guarded = runtime._guard_heating_target(target, normal, mapping, result, now)
    assert guarded is not None
    assert isinstance(guarded[0], NormalizedRangeTarget)
    assert guarded[0].heating.normalized_room_c == normal.heating.normalized_room_c
    assert guarded[0].cooling.normalized_room_c == 24.0
    runtime.heat_guards["registry-1"] = replace(
        runtime.heat_guards["registry-1"],
        phase=runtime_module.HeatGuardPhase.RAMP,
        ramp_at=now - timedelta(minutes=10),
        ramp_start_c=22.0,
    )
    withdrawn = runtime._guard_heating_target(target, normal, mapping, result, now)
    assert withdrawn is not None
    assert isinstance(withdrawn[0], NormalizedRangeTarget)
    assert withdrawn[0].heating.normalized_room_c < 22.0
    assert withdrawn[0].cooling.normalized_room_c == 24.0
    runtime.stale_safety_applied.add("registry-1")
    with_cooling_safety = runtime._guard_heating_target(target, normal, mapping, result, now)
    assert with_cooling_safety is not None
    assert isinstance(with_cooling_safety[0], NormalizedRangeTarget)
    assert with_cooling_safety[0].cooling.normalized_room_c >= 27.0
    for cancel in runtime.timers.values():
        cancel()


def test_stale_safety_timer_arms_cancels_and_clears_after_valid_recovery(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    hass.states.async_set(
        "climate.target",
        "cool",
        {
            "hvac_modes": ["off", "cool"],
            "supported_features": 1,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    runtime._schedule_stale_safety(stale)
    assert "stale_safety" in runtime.timers

    runtime.stale_safety_applied.add("registry-1")
    runtime._schedule_stale_safety(valid)
    assert "stale_safety" not in runtime.timers
    assert runtime.stale_safety_applied == set()

    runtime._schedule_stale_safety(replace(stale, source_states=()))
    assert "stale_safety" not in runtime.timers

    runtime._schedule_stale_safety(stale)
    assert "stale_safety" in runtime.timers
    runtime.stale_safety_applied.add("registry-1")
    runtime.control_enabled = False
    runtime._schedule_stale_safety(stale)
    assert "stale_safety" not in runtime.timers
    assert runtime.stale_safety_applied == set()


def test_control_enable_uses_domain_lease_and_rolls_back_conflict() -> None:
    first = _runtime(entry_id="first")
    second = _runtime(entry_id="second")
    second.hass = first.hass
    asyncio.run(first.async_set_control_enabled(True))
    assert first.control_enabled
    with pytest.raises(ValueError, match="already_controlled"):
        asyncio.run(second.async_set_control_enabled(True))
    assert not second.control_enabled
    asyncio.run(first.async_set_control_enabled(False))
    asyncio.run(second.async_set_control_enabled(True))
    assert second.control_enabled


def test_multi_target_enable_is_atomic_when_a_later_lease_conflicts() -> None:
    owner = _runtime(entry_id="owner", target_identity="registry-2")
    contender = _runtime(entry_id="contender", target_identity="registry-1")
    contender.hass = owner.hass
    contender.entry.data["targets"].append(
        {
            "target_uuid": "target-2",
            "entity_id": "climate.second",
            "registry_identity": "registry-2",
        }
    )
    asyncio.run(owner.async_set_control_enabled(True))

    with pytest.raises(ValueError, match="already_controlled"):
        asyncio.run(contender.async_set_control_enabled(True))

    leases = owner.hass.data["athb"]["target_leases"]
    assert leases.owner("registry-1") is None
    assert leases.owner("registry-2") == "owner"
    assert "registry-1" not in contender.ownership


def test_unload_cancels_listeners_releases_lease_and_clears_callbacks() -> None:
    runtime = _runtime()
    asyncio.run(runtime.async_set_control_enabled(True))
    removed: list[bool] = []
    runtime.listeners.append(lambda: removed.append(True))
    runtime.subscribe(lambda: None)
    asyncio.run(runtime.async_unload())
    assert removed == [True]
    assert runtime.listeners == []
    assert runtime.update_callbacks == set()
    assert runtime.hass.data["athb"]["target_leases"].owner("registry-1") is None


async def test_runtime_tracks_reports_debounces_and_captures_coherent_snapshot(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-1",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "measured",
            "rh_entity": "sensor.rh",
            "targets": [],
        },
        options={"comfort_strategy": "balanced"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-1", "balanced", "off", False)
    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    hass.states.async_set("sensor.room", "21.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "12.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.rh", "48", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert runtime.debounce_cancel is not None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    await runtime.controller.async_wait_idle()
    assert runtime.values["source_states"]["sensor.room"]["state"] == "21.5"
    assert runtime.values["source_states"]["sensor.rh"]["unit"] == "%"
    assert runtime.values["strategy"] == "balanced"
    await runtime.async_unload()


async def test_unchanged_source_report_renews_freshness_without_target_feedback(
    hass: HomeAssistant,
) -> None:
    attributes = {"unit_of_measurement": "°C"}
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "temperature": 18.0,
        "unit_of_measurement": "°C",
    }
    hass.states.async_set("sensor.room", "21.5", attributes)
    hass.states.async_set("climate.target", "heat", target_attributes)
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-reported",
            "primary_temperature": "sensor.room",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-reported", "balanced", "off", False)
    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    initial_generation = runtime.input_generation
    initial_state = hass.states.get("sensor.room")
    assert initial_state is not None
    initial_reported = initial_state.last_reported

    hass.states.async_set("sensor.room", "21.5", attributes)
    await hass.async_block_till_done()

    reported_state = hass.states.get("sensor.room")
    assert reported_state is not None
    assert reported_state.last_reported > initial_reported
    assert runtime.input_generation == initial_generation + 1
    assert runtime.debounce_cancel is not None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    await runtime.controller.async_wait_idle()

    assert (
        runtime.values["source_states"]["sensor.room"]["last_reported"]
        == reported_state.last_reported.isoformat()
    )
    accepted_primary = runtime.source_states["primary"].last_accepted
    assert accepted_primary is not None
    assert accepted_primary.observed_at == reported_state.last_reported
    assert not runtime.source_states["primary"].recovering
    assert runtime.debounce_cancel is None

    target_generation = runtime.input_generation
    hass.states.async_set("climate.target", "heat", target_attributes)
    await hass.async_block_till_done()

    assert runtime.input_generation == target_generation
    assert runtime.debounce_cancel is None
    await runtime.async_unload()


async def test_runtime_uses_pipeline_and_broker_for_exact_target_only_service(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def capture(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 16.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 17.0,
            "unit_of_measurement": "°C",
        },
    )
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-command",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-command", "balanced", "off", True)

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert [dict(call.data) for call in calls] == [
        {"entity_id": "climate.target", "temperature": 18.0}
    ]
    assert all("hvac_mode" not in call.data for call in calls)
    assert runtime.values["rh_provenance"] == "declared"
    assert runtime.values["calculation"].targets[0].result.policy.fallback
    await runtime.async_unload()


@pytest.mark.parametrize(
    ("stored_fingerprint", "expected_reason", "expected_reasons"),
    [
        (None, "unclean_shutdown", ("unclean_shutdown",)),
        (
            "stale-configuration",
            "configuration_changed",
            ("configuration_changed", "unclean_shutdown"),
        ),
    ],
)
async def test_restart_without_pending_command_reasserts_without_resume(
    hass: HomeAssistant,
    stored_fingerprint: str | None,
    expected_reason: str,
    expected_reasons: tuple[str, ...],
) -> None:
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "temperature": 17.0,
        "unit_of_measurement": "°C",
    }
    acknowledged_attributes = {**target_attributes, "temperature": 18.0}
    calls: list[ServiceCall] = []

    async def acknowledge(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set(
            "climate.target", "heat", acknowledged_attributes, context=call.context
        )

    hass.services.async_register("climate", "set_temperature", acknowledge)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.target", "heat", target_attributes)
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-unclean-reassert",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(
        hass,
        cast(Any, entry),
        "zone-unclean-reassert",
        "balanced",
        "off",
        True,
    )
    stored = ControlStoreState(
        storage_generation=4,
        run_id="prior-run",
        clean_shutdown=False,
        configuration_fingerprint=stored_fingerprint or runtime._configuration_fingerprint(),
        strategy="balanced",
        actuators=(
            StoredActuator(
                target_identity="registry-1",
                ownership="owned",
                ownership_revision=3,
                external_revision=0,
                override_reason=None,
                override_expiry=None,
                resume_required=False,
                last_observed_target_fingerprint=None,
                last_command_id=None,
                last_command_payload_fingerprint=None,
                last_command_context_id=None,
                pending_command=None,
            ),
        ),
        control_enabled_intent=True,
    )
    await HomeAssistantControlStorageBackend(hass, "zone-unclean-reassert").async_save(
        serialize_control_state(stored)
    )

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert [dict(call.data) for call in calls] == [
        {"entity_id": "climate.target", "temperature": 18.0}
    ]
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    assert not runtime.ownership["registry-1"].resume_required
    assert runtime.values["recovery_reason"] == expected_reason
    assert runtime.values["recovery_reasons"] == expected_reasons
    assert runtime.values["command_outcomes"]["target-1"] == "own_context_match"
    await runtime.async_unload()


async def test_same_value_climate_report_acknowledges_command_end_to_end(
    hass: HomeAssistant,
) -> None:
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "temperature": 18.0,
        "unit_of_measurement": "°C",
    }
    calls: list[ServiceCall] = []

    async def report_unchanged(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set("climate.target", "heat", target_attributes, context=call.context)

    hass.services.async_register("climate", "set_temperature", report_unchanged)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.target", "heat", {**target_attributes, "temperature": 17.0})
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-same-value",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-same-value", "balanced", "off", True)

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert runtime.broker is not None
    assert runtime.broker.state_counts("registry-1") == (0, 0)
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    await runtime.async_unload()


async def test_manual_override_and_boost_deadlines_expire_without_polling(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-timers",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "manual_override_minutes": 15.0,
            "boost_duration_minutes": 5.0,
            "preheat_duration_minutes": 5.0,
        },
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-timers", "balanced", "off", True)
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime._transition("registry-1", OwnershipEvent.EXTERNAL_TARGET)
    expiry = runtime.ownership["registry-1"].override_expiry
    assert expiry is not None
    assert "override:registry-1" in runtime.timers
    runtime.timers.pop("override:registry-1")()
    runtime._transition(
        "registry-1", OwnershipEvent.OVERRIDE_EXPIRED, now=expiry + timedelta(seconds=1)
    )
    assert runtime.ownership["registry-1"].ownership is Ownership.RECONCILING

    await runtime.async_set_boost_mode("adaptive")
    boost_expiry = cast(datetime, runtime.values["boost_expiry"])
    assert runtime.boost_mode == "adaptive"
    assert boost_expiry > expiry - timedelta(minutes=15)
    runtime.timers.pop("boost")()
    await runtime.async_set_boost_mode("off")
    assert runtime.boost_mode == "off"

    await runtime.async_set_preheat_active(True)
    preheat_expiry = cast(datetime, runtime.values["preheat_expiry"])
    first_timer = runtime.timers["preheat"]
    assert runtime.preheat_active
    await runtime.async_set_preheat_active(True)
    renewed_expiry = cast(datetime, runtime.values["preheat_expiry"])
    assert renewed_expiry >= preheat_expiry
    assert runtime.timers["preheat"] is not first_timer
    async_fire_time_changed(hass, renewed_expiry + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not runtime.preheat_active
    assert runtime.values["preheat_expiry"] is None


async def test_failure_hold_uses_event_timer_and_elapses_after_fifteen_minutes(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass

    runtime._update_failure_hold("extrapolation_rejected")

    assert runtime.failure_hold_reason == "extrapolation_rejected"
    assert runtime.failure_hold_elapsed is False
    assert "failure_hold" in runtime.timers
    original_timer = runtime.timers["failure_hold"]
    runtime._update_failure_hold("extrapolation_rejected")
    assert runtime.timers["failure_hold"] is original_timer

    runtime._update_failure_hold(None)
    assert runtime.failure_hold_reason is None
    assert runtime.failure_hold_elapsed is False
    assert "failure_hold" not in runtime.timers

    runtime._update_failure_hold("extrapolation_rejected")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done()

    assert runtime.failure_hold_elapsed is True
    assert runtime.input_generation == 2
    assert "failure_hold" not in runtime.timers

    runtime._update_failure_hold(None)
    assert runtime.failure_hold_reason is None
    assert runtime.failure_hold_elapsed is False
    assert runtime.timers == {}


def test_occupancy_setback_holds_last_known_state_then_reports_unknown(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-auto", "targets": []},
        options={"occupancy_entity": "binary_sensor.occupied"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-auto", "balanced", "off", False)
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    hass.states.async_set("binary_sensor.occupied", "off")
    assert runtime._resolve_profile(now).resolved.value == "eco"
    hass.states.async_set("binary_sensor.occupied", "unknown")
    assert runtime._resolve_profile(now + timedelta(minutes=20)).resolved.value == "eco"
    expired = runtime._resolve_profile(now + timedelta(minutes=31))
    assert expired.resolved.value == "comfort"
    assert expired.reasons == ("occupancy_unknown",)


def test_preheat_overrides_effective_occupancy_without_hiding_source(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-preheat", "targets": []},
        options={"occupancy_entity": "binary_sensor.heating_active"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-preheat", "balanced", "off", False)
    now = dt_util.utcnow()
    hass.states.async_set("binary_sensor.heating_active", "off")
    assert runtime._resolve_profile(now).resolved.value == "eco"

    runtime.values["preheat_expiry"] = now + timedelta(minutes=60)
    assert runtime._resolve_profile(now + timedelta(seconds=1)).resolved.value == "comfort"
    assert runtime.values["occupancy_source_state"] == "off"
    assert runtime.values["occupancy_effective_state"] == "on"
    assert runtime.values["occupancy_override_reason"] == "preheat"
    assert runtime.values["occupancy_available"] is True

    runtime.values["preheat_expiry"] = None
    assert runtime._resolve_profile(now + timedelta(seconds=2)).resolved.value == "eco"
    assert runtime.values["occupancy_source_state"] == "off"
    assert runtime.values["occupancy_effective_state"] == "off"
    assert runtime.values["occupancy_override_reason"] is None


def test_outdoor_history_sample_converts_fahrenheit_exactly_once(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-unit", "outdoor_source": "sensor.outdoor", "targets": []},
        options={},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-unit", "balanced", "off", False)
    hass.states.async_set("sensor.outdoor", "50", {"unit_of_measurement": "°F"})
    sample = runtime._outdoor_sample(datetime(2026, 9, 10, 10, tzinfo=UTC))
    assert sample is not None
    assert sample.value_c == pytest.approx(10.0)


def test_runtime_coordination_uses_manual_opposing_target_observation() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    cooling_capability = ClimateCapabilitySnapshot(
        "cool",
        ("off", "cool"),
        1,
        16.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        20.0,
        None,
        None,
    )
    snapshot = replace(
        snapshot,
        targets=(
            snapshot.targets[0],
            CapturedTarget(
                "cool-target",
                "registry-cool",
                "climate.cool",
                cooling_capability,
            ),
        ),
    )
    calculation = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")
    runtime.entry.data["targets"].append(
        {
            "target_uuid": "cool-target",
            "entity_id": "climate.cool",
            "registry_identity": "registry-cool",
        }
    )
    runtime.ownership = {
        "registry-climate-living-room": OwnershipState(
            "registry-climate-living-room", Ownership.OWNED
        ),
        "registry-cool": OwnershipState("registry-cool", Ownership.MANUAL_OVERRIDE),
    }
    assert (
        runtime._runtime_coordination_reason(calculation.targets[0], calculation)
        == "cross_actuator_conflict"
    )


def test_runtime_tracks_all_progressive_sources_and_critical_freshness(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        entry_id="entry-sources",
        data={
            "zone_uuid": "zone-sources",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_entity": "sensor.rh",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "occupancy_entity": "binary_sensor.occupied",
            "air_speed_entity": "sensor.air_speed",
            "radiant_model": "surface",
            "surface_temperature_entity": "sensor.surface",
            "critical_locations": [
                {
                    "location_id": "seat",
                    "entity_id": "sensor.seat",
                    "mode": "heating_guard",
                },
                "invalid-entry",
            ],
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-sources", "balanced", "off", False)
    for entity_id, value, unit in (
        ("sensor.room", "20", "°C"),
        ("sensor.outdoor", "5", "°C"),
        ("sensor.rh", "50", "%"),
        ("sensor.air_speed", "0.1", "m/s"),
        ("sensor.surface", "16", "°C"),
        ("sensor.seat", "18.5", "°C"),
    ):
        hass.states.async_set(entity_id, value, {"unit_of_measurement": unit})
    tracked = runtime._tracked_entity_ids()
    assert {
        "sensor.room",
        "sensor.rh",
        "sensor.air_speed",
        "sensor.surface",
        "sensor.seat",
        "binary_sensor.occupied",
        "climate.target",
    } <= tracked
    assert "sensor.outdoor" not in tracked
    runtime._update_critical_delta("sensor.seat", hass.states.get("sensor.seat"))
    assert runtime.critical_delta_states["seat"].report_count == 1
    runtime._schedule_freshness_expiries(dt_util.utcnow(), "sensor.surface")
    assert "freshness:critical:seat" in runtime.timers
    assert "freshness:radiant" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


def test_runtime_reads_mold_indicator_critical_point_attribute_as_temperature(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    hass.states.async_set(
        "sensor.mold_indicator",
        "86.2",
        {
            "unit_of_measurement": "%",
            "estimated_critical_temp": 16.7,
            "dewpoint": 13.2,
        },
    )

    captured = runtime._snapshot_mold_indicator(hass.states.get("sensor.mold_indicator"))

    assert captured is not None
    assert captured.raw_state == 16.7
    assert captured.unit == str(hass.config.units.temperature_unit)
    assert captured.attributes == {"estimated_critical_temp": 16.7}


def test_service_and_registry_events_distinguish_own_context_and_removal(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    stored = SimpleNamespace(target_identity="registry-1", last_command_context_id="own-context")
    runtime.persistence = cast(Any, SimpleNamespace(state=SimpleNamespace(actuators=(stored,))))

    ignored = SimpleNamespace(
        data={"domain": "light", "service": "turn_on"}, context=Context(id="external")
    )
    runtime._service_event(cast(Any, ignored))
    own = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": "climate.target"},
        },
        context=Context(id="own-context"),
    )
    runtime._service_event(cast(Any, own))
    runtime._service_event(
        cast(
            Any,
            SimpleNamespace(data=own.data, context=Context(id="child", parent_id="own-context")),
        )
    )
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    runtime.persistence = None
    external = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": ["climate.target"]},
        },
        context=Context(id="external"),
    )
    runtime._service_event(cast(Any, external))
    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE

    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "update", "entity_id": "climate.target"}))
    )
    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "remove", "entity_id": "climate.target"}))
    )
    assert repair_calls == []
    assert runtime_module.REMOVED_ENTITY_REPAIR_TIMER in runtime.timers
    for cancel in runtime.timers.values():
        cancel()
    if runtime.debounce_cancel is not None:
        runtime.debounce_cancel()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_registry_removal_repair_ignores_transient_startup_removal(
    hass: HomeAssistant,
) -> None:
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("climate", "test", "target", suggested_object_id="target")
    assert entity.entity_id == "climate.target"
    runtime = _runtime()
    runtime.hass = hass
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )

    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "remove", "entity_id": entity.entity_id}))
    )
    assert repair_calls == []

    async_fire_time_changed(
        hass,
        dt_util.utcnow() + runtime_module.REMOVED_ENTITY_REPAIR_SETTLE + timedelta(seconds=1),
    )
    await hass.async_block_till_done()

    assert repair_calls == [("removed_source_or_target", False)]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_registry_removal_repair_reports_entity_still_missing(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )

    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "remove", "entity_id": "climate.target"}))
    )
    assert repair_calls == []

    async_fire_time_changed(
        hass,
        dt_util.utcnow() + runtime_module.REMOVED_ENTITY_REPAIR_SETTLE + timedelta(seconds=1),
    )
    await hass.async_block_till_done()

    assert repair_calls == [("removed_source_or_target", True)]


def test_registry_rename_preserves_target_identity_and_updates_live_tracking(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-rename",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename", "balanced", "off", False)
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": "climate.target",
                    "entity_id": "climate.renamed_target",
                }
            ),
        )
    )

    assert entry.data["targets"][0] == {
        "target_uuid": "target-1",
        "entity_id": "climate.renamed_target",
        "registry_identity": "registry-1",
    }
    assert runtime.input_generation == 2
    assert runtime.listeners
    assert repair_calls == []
    for unsubscribe in runtime.listeners:
        unsubscribe()
    if runtime.debounce_cancel is not None:
        runtime.debounce_cancel()


def test_registry_rename_updates_every_configured_source_kind(hass: HomeAssistant) -> None:
    old_entity_id = "sensor.shared"
    new_entity_id = "sensor.renamed_shared"
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-rename-all",
            "primary_temperature": old_entity_id,
            "outdoor_source": old_entity_id,
            "rh_entity": old_entity_id,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": old_entity_id,
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "occupancy_entity": old_entity_id,
            "air_speed_entity": old_entity_id,
            "mrt_entity": old_entity_id,
            "globe_temperature_entity": old_entity_id,
            "surface_temperature_entity": old_entity_id,
            "mold_indicator_entity": old_entity_id,
            "critical_locations": [
                {"location_id": "window", "entity_id": old_entity_id},
                "preserved-extension-value",
            ],
        },
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename-all", "balanced", "off", False)
    renamed_history: list[str] = []
    runtime.history_collector = cast(Any, SimpleNamespace(update_entity_id=renamed_history.append))
    runtime.repair_manager = cast(Any, SimpleNamespace(update=lambda *_args: None))

    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": old_entity_id,
                    "entity_id": new_entity_id,
                }
            ),
        )
    )

    assert entry.data["primary_temperature"] == new_entity_id
    assert entry.data["outdoor_source"] == new_entity_id
    assert entry.data["rh_entity"] == new_entity_id
    assert entry.data["targets"][0]["entity_id"] == new_entity_id
    for key in (
        "occupancy_entity",
        "air_speed_entity",
        "mrt_entity",
        "globe_temperature_entity",
        "surface_temperature_entity",
        "mold_indicator_entity",
    ):
        assert entry.options[key] == new_entity_id
    assert entry.options["critical_locations"] == [
        {"location_id": "window", "entity_id": new_entity_id},
        "preserved-extension-value",
    ]
    assert renamed_history == [new_entity_id]

    generation = runtime.input_generation
    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": "sensor.unconfigured",
                    "entity_id": "sensor.still_unconfigured",
                }
            ),
        )
    )
    assert runtime.input_generation == generation
    for unsubscribe in runtime.listeners:
        unsubscribe()
    if runtime.debounce_cancel is not None:
        runtime.debounce_cancel()


def test_external_temperature_target_uses_home_assistant_area_selection(
    hass: HomeAssistant,
) -> None:
    area = ar.async_get(hass).async_create("Living room")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("climate", "test", "target", suggested_object_id="target")
    registry.async_update_entity(entry.entity_id, area_id=area.id)
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )

    event = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"area_id": area.id, "temperature": 21.0},
        },
        context=Context(id="external-area-call"),
    )
    runtime._service_event(cast(Any, event))

    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    for cancel in runtime.timers.values():
        cancel()


def test_external_temperature_target_all_selector_is_not_missed(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    event = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": "all", "temperature": 21.0},
        },
        context=Context(id="external-all-call"),
    )

    runtime._service_event(cast(Any, event))

    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    for cancel in runtime.timers.values():
        cancel()


async def test_service_event_range_echo_requires_complete_temperature_payload(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", Ownership.OWNED, DataReadiness.READY, TargetReadiness.AVAILABLE_SUPPORTED
    )
    runtime.capability_generations["registry-1"] = 1
    broker = MagicMock(spec=CommandBroker)
    broker.expected_automatic_echo.return_value = True
    runtime.broker = broker
    event = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {
                "entity_id": "climate.target",
                "target_temp_low": 19.0,
                "target_temp_high": 23.0,
            },
        },
        context=Context(id="range-echo"),
    )
    runtime._service_event(cast(Any, event))
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    observation = broker.expected_automatic_echo.call_args.args[0]
    assert observation.observed.low_ha == 19.0
    assert observation.observed.high_ha == 23.0
    assert not observation.user_initiated
    del event.data["service_data"]["target_temp_high"]
    runtime._service_event(cast(Any, event))
    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    broker.expected_automatic_echo.assert_called_once()
    for cancel in runtime.timers.values():
        cancel()


class _FeedbackBroker:
    def __init__(self, outcomes: list[CommandOutcome], *, pending: int = 0) -> None:
        self.outcomes = outcomes
        self.pending = pending
        self.feedbacks: list[Any] = []
        self.invalidated: list[str] = []

    def state_counts(self, _identity: str) -> tuple[int, int]:
        return (self.pending, 0)

    async def async_feedback(self, feedback: Any, *, now: datetime) -> CommandOutcome:
        self.feedbacks.append(feedback)
        return self.outcomes.pop(0)

    def invalidate(self, identity: str) -> None:
        self.invalidated.append(identity)

    async def async_drain_queued(self, _identity: str, *, now: datetime) -> CommandOutcome | None:
        return None


@pytest.mark.parametrize(
    "intervention", [None, "user_same", "user_different", "automation_different"]
)
async def test_comfort_command_delegated_echo_preserves_ownership_and_real_interventions(
    hass: HomeAssistant, intervention: str | None
) -> None:
    """Exercise both service and state routes through the real runtime/broker."""

    runtime = _runtime()
    runtime.hass = hass
    entry = MockConfigEntry(domain="athb", data=runtime.entry.data, options=runtime.entry.options)
    entry.add_to_hass(hass)
    runtime.entry = entry
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", Ownership.OWNED, DataReadiness.READY, TargetReadiness.AVAILABLE_SUPPORTED
    )
    runtime.capability_generations["registry-1"] = 1
    runtime_module.get_lease_registry(hass).acquire("registry-1", entry.entry_id)
    persistence = SimpleNamespace(
        state=None,
        async_persist_pending=AsyncMock(return_value=True),
        async_mark_dispatched=AsyncMock(return_value=True),
        async_resolve=AsyncMock(return_value=True),
        async_update_runtime=AsyncMock(return_value=True),
        async_update_actuator=AsyncMock(return_value=True),
    )
    runtime.persistence = cast(Any, persistence)
    runtime.broker = CommandBroker(
        service=HomeAssistantClimateService(hass),
        persistence=cast(Any, persistence),
        preflight=runtime._broker_preflight,
        command_id_factory=lambda: "comfort-command",
        context_factory=runtime._context_token,
    )
    calculation = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = replace(
        calculation.targets[0], registry_identity="registry-1", entity_id="climate.target"
    )
    assert target.result is not None
    assert target.mapping is not None
    normalized = target.result.normalized
    assert isinstance(normalized, NormalizedScalarTarget)
    desired = normalized.normalized_ha
    attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "temperature": desired - 1.0,
        "unit_of_measurement": "°C",
    }
    hass.states.async_set("climate.target", "heat", attributes)
    old = hass.states.get("climate.target")
    runtime.last_target_fingerprints["registry-1"] = runtime._fingerprint(
        capability_from_state(old)
    )

    def service_event(value: float, context: Context) -> None:
        runtime._service_event(
            cast(
                Any,
                SimpleNamespace(
                    data={
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": {"entity_id": "climate.target", "temperature": value},
                    },
                    context=context,
                ),
            )
        )

    async def capture(call: ServiceCall) -> None:
        service_event(call.data["temperature"], call.context)

    hass.services.async_register("climate", "set_temperature", capture)
    await runtime.async_set_strategy("efficient")
    intent = runtime._intent_from_result(
        target, normalized, target.mapping, dt_util.utcnow(), explicit_transition=True
    )
    await runtime.broker.async_submit(intent, now=dt_util.utcnow())
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    # Another comfort change/measurement arrives while RoomMind's timer is pending.
    await runtime.async_set_strategy("eco")
    runtime.input_generation += 1
    service_event(desired, Context(id="delegated-service"))
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    assert runtime.broker.state_counts("registry-1") == (1, 0)

    if intervention is not None:
        service_event(
            desired if intervention == "user_same" else desired + 1.0,
            Context(
                id="real-intervention",
                user_id=None if intervention == "automation_different" else "user",
            ),
        )
        assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    new = State(
        "climate.target",
        "heat",
        {**attributes, "temperature": desired},
        context=Context(id="roommind-feedback"),
    )
    await runtime._async_handle_target_state(runtime.entry.data["targets"][0], old, new)
    await hass.async_block_till_done()
    if intervention is None:
        assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
        assert runtime.broker.state_counts("registry-1") == (0, 0)
        assert persistence.async_resolve.call_args.args[1] == "delegated_exact_pending_match"
        # A duplicate delegated call after acknowledgement is harmless, still bounded.
        service_event(desired, Context(id="duplicate-service"))
        assert runtime.ownership["registry-1"].external_revision == 0
    else:
        assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
        assert runtime.ownership["registry-1"].external_revision == 1
        persistence.async_resolve.assert_not_awaited()
    for cancel in runtime.timers.values():
        cancel()


async def test_target_return_after_startup_is_reconciled_not_manual_override(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.RECONCILING,
        DataReadiness.READY,
        TargetReadiness.UNAVAILABLE,
    )
    runtime.capability_generations["registry-1"] = 1
    unavailable = State("climate.target", "unavailable")
    runtime.last_target_fingerprints["registry-1"] = runtime._fingerprint(
        capability_from_state(unavailable)
    )
    broker = _FeedbackBroker([])
    runtime.broker = cast(Any, broker)
    available = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 19.5,
            "unit_of_measurement": "°C",
        },
    )

    await runtime._async_handle_target_state(
        runtime.entry.data["targets"][0], unavailable, available
    )

    state = runtime.ownership["registry-1"]
    assert state.ownership is Ownership.RECONCILING
    assert state.target_readiness is TargetReadiness.AVAILABLE_SUPPORTED
    assert state.external_revision == 0
    assert runtime.capability_generations["registry-1"] == 2
    assert broker.feedbacks == []


async def test_unchanged_target_report_acknowledges_pending_command_without_recalculation(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    acknowledged = CommandOutcome(
        "command",
        DispatchStatus.DISPATCHED,
        AcknowledgementStatus.ACKNOWLEDGED,
        "own_context_match",
    )
    broker = _FeedbackBroker([acknowledged], pending=1)
    runtime.broker = cast(Any, broker)
    generation = runtime.input_generation
    current = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 19.5,
            "unit_of_measurement": "°C",
        },
    )

    await runtime._async_handle_target_report(
        runtime.entry.data["targets"][0], current, "athb-context", None
    )
    await hass.async_block_till_done()

    assert len(broker.feedbacks) == 1
    assert broker.feedbacks[0].context_id == "athb-context"
    assert broker.feedbacks[0].observed.temperature_ha == 19.5
    assert runtime.input_generation == generation
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED


async def test_target_feedback_rejection_repair_and_hvac_mode_reconciliation(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime.last_target_fingerprints["registry-1"] = runtime._fingerprint(
        ClimateCapabilitySnapshot(
            "heat", ("off", "heat"), 1, 5.0, 35.0, 0.5, TemperatureUnit.CELSIUS, 18.0
        )
    )
    rejected = CommandOutcome(
        "command",
        DispatchStatus.DISPATCHED,
        AcknowledgementStatus.REJECTED,
        "target_rejected",
    )
    acknowledged = replace(rejected, acknowledgement_status=AcknowledgementStatus.ACKNOWLEDGED)
    broker = _FeedbackBroker([rejected, rejected, rejected, acknowledged])
    runtime.broker = cast(Any, broker)
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    target = runtime.entry.data["targets"][0]
    old = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 18.0,
            "unit_of_measurement": "°C",
        },
    )
    for temperature in (18.5, 19.0, 19.5):
        new = State(
            "climate.target",
            "heat",
            {
                "hvac_modes": ["off", "heat"],
                "supported_features": 1,
                "temperature": temperature,
                "unit_of_measurement": "°C",
            },
        )
        await runtime._async_handle_target_state(target, old, new)
        runtime.ownership["registry-1"] = replace(
            runtime.ownership["registry-1"], ownership=Ownership.OWNED
        )
        old = new
    assert runtime.rejection_counts["registry-1"] == 3
    assert ("persistent_target_rejection", True) in repair_calls

    new = State(
        "climate.target",
        "off",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 20.0,
            "unit_of_measurement": "°C",
        },
    )
    await runtime._async_handle_target_state(target, old, new)
    await hass.async_block_till_done()
    assert runtime.capability_generations["registry-1"] == 2
    assert runtime.rejection_counts["registry-1"] == 0
    assert ("persistent_target_rejection", False) in repair_calls


async def test_runtime_apply_schedules_deadlines_and_marks_unknown(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    calculation = calculate_runtime_snapshot(_captured(scenario))
    runtime = _runtime(target_identity="registry-climate-living-room")
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.entry_id = "zone"
    runtime.ownership = {
        "registry-climate-living-room": OwnershipState(
            "registry-climate-living-room",
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            revision=1,
        )
    }
    runtime.capability_generations = {"registry-climate-living-room": 1}

    class Broker:
        async def async_submit(self, _intent: Any, *, now: datetime) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.UNKNOWN,
                "command_outcome_unknown",
            )

        def invalidate(self, _identity: str) -> None:
            return None

    runtime.broker = cast(Any, Broker())
    await runtime._async_apply_calculation(calculation)
    assert runtime.values["command_outcomes"] == {"target-living-room": "command_outcome_unknown"}
    assert runtime.ownership["registry-climate-living-room"].ownership is Ownership.OWNED
    assert "ack:registry-climate-living-room" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


def test_runtime_marks_startup_and_data_recovery_for_live_target_reassertion() -> None:
    scenario = load_scenarios()[0]
    calculation = calculate_runtime_snapshot(_captured(scenario))
    identity = "registry-climate-living-room"
    runtime = _runtime(target_identity=identity)
    runtime.control_enabled = True
    runtime.ownership[identity] = OwnershipState(
        identity,
        Ownership.RECONCILING,
        DataReadiness.INVALID,
        TargetReadiness.UNAVAILABLE,
    )

    startup = runtime._reconcile_targets(calculation)

    assert startup == frozenset({identity})
    assert runtime.ownership[identity].ownership is Ownership.OWNED
    assert runtime._reconcile_targets(calculation) == frozenset()

    runtime.ownership[identity] = replace(
        runtime.ownership[identity], data_readiness=DataReadiness.HOLD_LAST_GOOD
    )
    recovered = runtime._reconcile_targets(calculation)

    assert recovered == frozenset({identity})


def test_explicit_transition_survives_invalid_input_and_is_consumed_by_valid_result(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = replace(_captured(scenario), explicit_transition=False)
    valid = calculate_runtime_snapshot(snapshot)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    identity = "registry-climate-living-room"
    runtime = _runtime(target_identity=identity)
    runtime.hass = hass
    runtime.pending_transition_reasons.clear()
    runtime.explicit_transition = False
    runtime.ownership[identity] = OwnershipState(
        identity,
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )

    runtime._publish_calculation(1, stale)

    assert runtime.explicit_transition
    assert runtime.pending_transition_reasons == {"input_recovery"}

    recovered = calculate_runtime_snapshot(replace(snapshot, explicit_transition=True))
    runtime._publish_calculation(2, recovered)

    assert not runtime.explicit_transition
    assert runtime.pending_transition_reasons == set()


def test_occupancy_change_is_an_explicit_transition(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options["occupancy_entity"] = "binary_sensor.room_occupied"
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    hass.states.async_set("binary_sensor.room_occupied", "on")
    assert runtime._resolve_profile(now).resolved.value == "comfort"
    runtime.pending_transition_reasons.clear()
    runtime.explicit_transition = False

    hass.states.async_set("binary_sensor.room_occupied", "off")
    assert runtime._resolve_profile(now + timedelta(seconds=1)).resolved.value == "eco"

    assert runtime.explicit_transition
    assert runtime.pending_transition_reasons == {"occupancy"}


@pytest.mark.parametrize(("old_value", "new_value"), [("off", "on"), ("on", "off")])
def test_occupancy_source_state_change_schedules_immediate_snapshot(
    old_value: str,
    new_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.entry.options["occupancy_entity"] = "binary_sensor.room_occupied"
    scheduler = MagicMock()
    monkeypatch.setattr(ZoneRuntime, "schedule_environmental_snapshot", scheduler)
    generation = runtime.input_generation

    runtime._handle_state_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "entity_id": "binary_sensor.room_occupied",
                    "old_state": State("binary_sensor.room_occupied", old_value),
                    "new_state": State("binary_sensor.room_occupied", new_value),
                }
            ),
        )
    )

    assert runtime.input_generation == generation + 1
    scheduler.assert_called_once_with()


def test_occupancy_source_attribute_only_update_does_not_recalculate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.entry.options["occupancy_entity"] = "binary_sensor.room_occupied"
    scheduler = MagicMock()
    monkeypatch.setattr(ZoneRuntime, "schedule_environmental_snapshot", scheduler)
    generation = runtime.input_generation

    runtime._handle_state_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "entity_id": "binary_sensor.room_occupied",
                    "old_state": State("binary_sensor.room_occupied", "on", {"source": "old"}),
                    "new_state": State("binary_sensor.room_occupied", "on", {"source": "new"}),
                }
            ),
        )
    )

    assert runtime.input_generation == generation
    scheduler.assert_not_called()


async def test_climate_can_be_primary_source_and_target_on_the_same_event(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.data["primary_temperature"] = "climate.target"
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime.broker = None
    old = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
    )
    new = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.5, "temperature": 19.0},
    )
    generation = runtime.input_generation
    event = SimpleNamespace(
        data={"entity_id": "climate.target", "old_state": old, "new_state": new}
    )

    runtime._handle_state_event(cast(Any, event))
    await hass.async_block_till_done()
    assert runtime.input_generation == generation + 1
    assert runtime.debounce_cancel is not None
    runtime.debounce_cancel()


def test_primary_report_filter_distinguishes_real_feedback_from_target_ack(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.data["primary_temperature"] = "climate.target"
    before = datetime(2026, 9, 15, 10, tzinfo=UTC)
    later = before + timedelta(minutes=1)
    baseline = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
        last_reported=before,
    )
    same_reading = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
        last_reported=later,
    )
    assert not runtime._record_primary_feedback("sensor.other", baseline, same_reading)
    assert not runtime._record_primary_feedback("climate.target", baseline, None)
    assert not runtime._record_primary_feedback(
        "climate.target",
        baseline,
        State("climate.target", "unavailable", last_reported=later),
    )
    assert not runtime._record_primary_feedback(
        "climate.target",
        baseline,
        State("climate.target", "heat", {"current_temperature": "invalid"}, last_reported=later),
    )
    runtime.primary_feedback_at = before
    assert runtime._record_primary_feedback("climate.target", baseline, same_reading)
    assert runtime.primary_feedback_at == later
    assert not runtime._record_primary_feedback("climate.target", baseline, same_reading)
    target_only = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 21.0},
        last_reported=later + timedelta(minutes=1),
    )
    assert not runtime._record_primary_feedback("climate.target", baseline, target_only)
    assert runtime.primary_feedback_at == later
    runtime.persistence = cast(
        Any,
        SimpleNamespace(
            state=SimpleNamespace(
                actuators=(SimpleNamespace(last_command_context_id="own-context"),)
            )
        ),
    )
    own_command_report = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
        last_reported=later + timedelta(minutes=2),
        context=Context(id="own-context"),
    )
    assert not runtime._record_primary_feedback("climate.target", baseline, own_command_report)
    assert runtime.primary_feedback_at == later
    runtime.persistence = None
    runtime.broker = cast(Any, SimpleNamespace(state_counts=lambda _identity: (1, 0)))
    assert not runtime._record_primary_feedback(
        "climate.target",
        baseline,
        State(
            "climate.target",
            "heat",
            {
                "supported_features": 1,
                "current_temperature": 20.0,
                "temperature": 19.0,
            },
            last_reported=later + timedelta(minutes=3),
        ),
    )
    assert runtime.primary_feedback_at == later


def test_climate_target_ack_does_not_renew_primary_temperature_feedback(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.data["primary_temperature"] = "climate.target"
    before = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    after = before + timedelta(minutes=1)
    old = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
        last_changed=before,
        last_updated=before,
        last_reported=before,
    )
    target_ack = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 21.0},
        last_changed=before,
        last_updated=after,
        last_reported=after,
    )
    runtime.primary_feedback_at = before
    assert not runtime._record_primary_feedback("climate.target", old, target_ack)
    assert runtime.primary_feedback_at == before
    independent_report = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
        last_changed=before,
        last_updated=before,
        last_reported=after,
    )
    assert runtime._record_primary_feedback("climate.target", old, independent_report)
    assert runtime.primary_feedback_at == after


async def test_broker_timer_callbacks_keep_ownership_and_recalculate_fresh_policy(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", Ownership.OWNED, DataReadiness.READY, TargetReadiness.AVAILABLE_SUPPORTED
    )

    class Broker:
        async def async_acknowledgement_timeout(
            self, _identity: str, *, now: datetime
        ) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.UNKNOWN,
                "command_outcome_unknown",
            )

        async def async_drain_queued(self, _identity: str, *, now: datetime) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.PENDING,
                "command_interval",
            )

        def invalidate(self, _identity: str) -> None:
            return None

    runtime.broker = cast(Any, Broker())
    await runtime._async_acknowledgement_timeout("registry-1")
    assert runtime.values["last_command_outcome"] == "command_outcome_unknown"
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    snapshot = MagicMock()
    monkeypatch.setattr(ZoneRuntime, "async_request_snapshot", snapshot)
    await runtime._async_drain_queued("registry-1")
    snapshot.assert_called_once()
    assert runtime.timers == {}
    for cancel in runtime.timers.values():
        cancel()


async def test_startup_storage_fault_and_boost_recovery_paths(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Persistence:
        start_ok = False
        expiry = "2000-01-01T00:00:00+00:00"
        preheat_expiry = "2000-01-01T00:00:00+00:00"

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=self.expiry,
                preheat_expiry_utc=self.preheat_expiry,
                rapid_boost_reached=False,
                actuators=(),
            )
            self.requires_resume = False

        async def async_start(self) -> bool:
            return self.start_ok

        async def async_mark_clean(self, *, now: datetime) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "ZoneCommandPersistence", Persistence)
    entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-paths",
        data={"zone_uuid": "startup-paths", "targets": []},
        options={"boost_mode": "adaptive", "control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "startup-paths", "balanced", "adaptive", False)
    await runtime.async_start()
    assert runtime.boost_mode == "off"
    assert not runtime.preheat_active
    assert "preheat" not in runtime.timers
    assert runtime.values["control_status"] == "storage_fault"
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await runtime.async_unload()

    Persistence.start_ok = True
    Persistence.expiry = "2099-01-01T00:00:00+00:00"
    Persistence.preheat_expiry = "2099-01-01T00:30:00+00:00"
    future_entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-future-boost",
        data={"zone_uuid": "startup-future-boost", "targets": []},
        options={"boost_mode": "adaptive", "control_enabled": False},
    )
    future_entry.add_to_hass(hass)
    future = ZoneRuntime(
        hass, cast(Any, future_entry), "startup-future-boost", "balanced", "adaptive", False
    )
    await future.async_start()
    assert future.boost_mode == "adaptive"
    assert "boost" in future.timers
    assert future.preheat_active
    assert "preheat" in future.timers
    assert future.values["preheat_expiry"] == datetime(2099, 1, 1, 0, 30, tzinfo=UTC)
    assert future.controller is not None
    await future.controller.async_wait_idle()
    await future.async_unload()


async def test_startup_restores_persisted_display_values_before_stale_calculation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Persistence:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=None,
                rapid_boost_reached=False,
                actuators=(),
                last_valid_values_json='{"thermal_sensation":-0.2}',
                last_valid_at="2026-09-13T12:00:00+00:00",
            )
            self.requires_resume = False

        async def async_start(self) -> bool:
            return True

        async def async_mark_clean(self, *, now: datetime) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "ZoneCommandPersistence", Persistence)
    hass.states.async_set("sensor.room", "20.0", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-restored-output",
        data={
            "zone_uuid": "startup-restored-output",
            "primary_temperature": "sensor.room",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "registry_identity": "registry-1",
                    "entity_id": "climate.target",
                }
            ],
        },
        options={"control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(
        hass,
        cast(Any, entry),
        "startup-restored-output",
        "balanced",
        "off",
        False,
    )

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()

    assert runtime.values["thermal_sensation"] == -0.2
    assert runtime.values["data_quality"] == "stale"
    assert runtime.last_valid_at == datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    assert runtime.primary_feedback_at == runtime.last_valid_at
    assert runtime.heat_guards["registry-1"].phase.value == "exhausted"

    await runtime.async_unload()


@pytest.mark.parametrize(
    ("stored_guards", "expected_phase"),
    [
        (
            '{"registry-1":{"phase":"stale_start","started_at":"2026-09-13T10:00:00+00:00","report_at":"2026-09-13T09:00:00+00:00","ramp_at":null,"ramp_start_c":null,"evidence_at":"2026-09-13T09:00:00+00:00"}}',
            "stale_start",
        ),
        ('{"registry-1":{"phase":"stale_start","started_at":"not-a-date"}}', "exhausted"),
        (
            '{"registry-1":{"phase":"stale_start","started_at":"2026-09-13T10:00:00","report_at":"2026-09-13T09:00:00+00:00"}}',
            "exhausted",
        ),
    ],
)
async def test_startup_restores_heat_guard_without_new_trial(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    stored_guards: str,
    expected_phase: str,
) -> None:
    class Persistence:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=None,
                rapid_boost_reached=False,
                actuators=(),
                heat_guards_json=stored_guards,
                last_valid_values_json=None,
                last_valid_at=None,
            )
            self.requires_resume = False

        async def async_start(self) -> bool:
            return True

        async def async_mark_clean(self, *, now: datetime) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "ZoneCommandPersistence", Persistence)
    hass.states.async_set("sensor.room", "20.0", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain="athb",
        entry_id=f"heat-guard-{expected_phase}",
        data={
            "zone_uuid": f"heat-guard-{expected_phase}",
            "primary_temperature": "sensor.room",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "registry_identity": "registry-1",
                    "entity_id": "climate.target",
                }
            ],
        },
        options={"control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), entry.entry_id, "balanced", "off", False)
    await runtime.async_start()
    assert runtime.heat_guards["registry-1"].phase.value == expected_phase
    if expected_phase == "stale_start":
        assert runtime.primary_feedback_at == datetime(2026, 9, 13, 9, tzinfo=UTC)
        assert runtime.heat_guards["registry-1"].started_at == datetime(2026, 9, 13, 10, tzinfo=UTC)
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await runtime.async_unload()


def test_readiness_radiant_modes_and_noop_timer_paths() -> None:
    runtime = _runtime()
    unavailable = ClimateCapabilitySnapshot(
        "heat", ("heat",), 1, 5.0, 35.0, 0.5, TemperatureUnit.CELSIUS, 20.0, available=False
    )
    off = replace(unavailable, available=True, hvac_mode="off")
    incompatible = replace(unavailable, available=True, hvac_mode="fan_only")
    assert runtime._target_readiness(unavailable) is TargetReadiness.UNAVAILABLE
    assert runtime._target_readiness(off) is TargetReadiness.SUSPENDED_MODE
    assert runtime._target_readiness(incompatible) is TargetReadiness.INCOMPATIBLE
    for mode, expected in (
        ("direct_mrt", "direct_mrt"),
        ("globe", "globe"),
        ("surface", "surface"),
        ("mold_indicator", "surface"),
        ("uniform", "direct_mrt"),
    ):
        runtime.entry.options["radiant_model"] = mode
        assert runtime._radiant_source_kind().value == expected
    runtime._replace_timer("ignored", 1.0, lambda: None)
    assert runtime.timers == {}
    assert runtime._timer_expiry("missing") is None


async def test_delayed_repair_activates_once_and_recovers(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    updates: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: updates.append((name, active)))
    )
    runtime._update_delayed_repair("missing_history_24h", True, timedelta(seconds=1))
    runtime._update_delayed_repair("missing_history_24h", True, timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert updates == [("missing_history_24h", True)]
    runtime._update_delayed_repair("missing_history_24h", False, timedelta(seconds=1))
    assert updates[-1] == ("missing_history_24h", False)


async def test_strategy_and_boost_changes_invalidate_active_runtime_without_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        entry_id="runtime-changes",
        data={"zone_uuid": "runtime-changes", "targets": []},
        options={"comfort_strategy": "balanced", "boost_mode": "off"},
    )
    entry.add_to_hass(hass)
    requested: list[Any] = []

    class Controller:
        def invalidate(self) -> None:
            requested.append("invalidate")

        def request(self, snapshot: Any) -> None:
            requested.append(snapshot)

    runtime = ZoneRuntime(hass, cast(Any, entry), "runtime-changes", "balanced", "off", False)
    runtime.controller = cast(Any, Controller())
    persisted: list[dict[str, Any]] = []

    class Persistence:
        async def async_update_runtime(self, **values: Any) -> bool:
            persisted.append(values)
            return True

    runtime.persistence = cast(Any, Persistence())
    cancelled: list[bool] = []
    runtime.debounce_cancel = lambda: cancelled.append(True)
    runtime.debounce_started = hass.loop.time()
    await runtime.async_set_strategy("comfort")
    assert requested[0] == "invalidate"
    assert cancelled == [True]
    assert runtime.configuration_generation == 2
    assert runtime.strategy == "comfort"
    assert entry.options["comfort_strategy"] == "comfort"

    await runtime.async_set_boost_mode("rapid")
    assert runtime.boost_mode == "rapid"
    assert "boost" in runtime.timers
    await runtime.async_set_boost_mode("off")
    assert runtime.boost_mode == "off"
    assert "boost" not in runtime.timers

    broker = MagicMock()
    runtime.broker = broker
    runtime.ownership = {"registry-1": cast(Any, object())}
    runtime.debounce_cancel = lambda: cancelled.append(True)
    runtime.debounce_started = hass.loop.time()
    await runtime.async_set_eco_intensity("workday")
    assert runtime.eco_intensity == "workday"
    assert runtime.configuration_generation == 3
    assert cancelled == [True, True]
    broker.invalidate.assert_called_once_with("registry-1")
    unchanged_generation = runtime.configuration_generation
    await runtime.async_set_eco_intensity("workday")
    assert runtime.configuration_generation == unchanged_generation
    await hass.async_block_till_done()
    assert len(persisted) == 4
    assert persisted[0]["strategy"] == "comfort"
    assert persisted[-1]["strategy"] == "comfort"
    assert persisted[-1]["configuration_fingerprint"] == runtime._configuration_fingerprint()

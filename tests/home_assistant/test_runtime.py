"""Runtime action, lease, callback, and cleanup tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.athb import runtime as runtime_module
from custom_components.athb.adapters.broker import CommandOutcome
from custom_components.athb.calculation import CapturedTarget, calculate_runtime_snapshot
from custom_components.athb.core.climate import ClimateCapabilitySnapshot, TemperatureUnit
from custom_components.athb.core.contracts import AcknowledgementStatus, DispatchStatus
from custom_components.athb.core.ownership import (
    DataReadiness,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    TargetReadiness,
)
from custom_components.athb.runtime import ZoneRuntime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios


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
    return ZoneRuntime(cast(Any, hass), cast(Any, entry), "zone-1", "balanced", "comfort", False)


def test_runtime_callbacks_profile_resume_and_lightweight_strategy() -> None:
    runtime = _runtime()
    updates: list[dict[str, Any]] = []
    remove = runtime.subscribe(lambda: updates.append(dict(runtime.values)))
    runtime.publish({"thermal_sensation": 0.1})
    assert updates[-1]["thermal_sensation"] == 0.1
    asyncio.run(runtime.async_set_profile("eco"))
    assert runtime.profile == "eco"
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
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-1", "balanced", "comfort", False)
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
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-command", "balanced", "comfort", True)

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
        options={"manual_override_minutes": 15.0, "boost_duration_minutes": 5.0},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-timers", "balanced", "comfort", True)
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

    await runtime.async_set_profile("boost")
    boost_expiry = cast(datetime, runtime.values["boost_expiry"])
    assert runtime.profile == "boost"
    assert boost_expiry > expiry - timedelta(minutes=15)
    runtime.timers.pop("boost")()
    await runtime.async_set_profile("comfort")
    assert runtime.profile == "comfort"


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


def test_auto_profile_holds_last_known_occupancy_then_reports_unknown(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-auto", "targets": []},
        options={"occupancy_entity": "binary_sensor.occupied"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-auto", "balanced", "auto", False)
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    hass.states.async_set("binary_sensor.occupied", "off")
    assert runtime._resolve_profile(now).resolved.value == "eco"
    hass.states.async_set("binary_sensor.occupied", "unknown")
    assert runtime._resolve_profile(now + timedelta(minutes=20)).resolved.value == "eco"
    expired = runtime._resolve_profile(now + timedelta(minutes=31))
    assert expired.resolved.value == "comfort"
    assert expired.reasons == ("occupancy_unknown",)


def test_outdoor_history_sample_converts_fahrenheit_exactly_once(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-unit", "outdoor_source": "sensor.outdoor", "targets": []},
        options={},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-unit", "balanced", "comfort", False)
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
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-sources", "balanced", "comfort", False)
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
    assert repair_calls[-1] == ("removed_source_or_target", True)
    for cancel in runtime.timers.values():
        cancel()


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
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename", "balanced", "comfort", False)
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
            "critical_locations": [
                {"location_id": "window", "entity_id": old_entity_id},
                "preserved-extension-value",
            ],
        },
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename-all", "balanced", "comfort", False)
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


class _FeedbackBroker:
    def __init__(self, outcomes: list[CommandOutcome]) -> None:
        self.outcomes = outcomes
        self.invalidated: list[str] = []

    async def async_feedback(self, _feedback: Any, *, now: datetime) -> CommandOutcome:
        return self.outcomes.pop(0)

    def invalidate(self, identity: str) -> None:
        self.invalidated.append(identity)

    async def async_drain_queued(self, _identity: str, *, now: datetime) -> CommandOutcome | None:
        return None


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
    assert runtime.ownership["registry-climate-living-room"].ownership is Ownership.COMMAND_FAULT
    assert "ack:registry-climate-living-room" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


async def test_broker_timer_callbacks_cover_timeout_and_queue_paths(
    hass: HomeAssistant,
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
    await runtime._async_drain_queued("registry-1")
    assert "ack:registry-1" in runtime.timers
    assert "queue:registry-1" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


async def test_startup_storage_fault_and_boost_recovery_paths(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Persistence:
        start_ok = False
        expiry = "2000-01-01T00:00:00+00:00"

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=self.expiry,
                previous_non_boost_profile="eco",
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
        options={"profile": "boost", "control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "startup-paths", "balanced", "boost", False)
    await runtime.async_start()
    assert runtime.profile == "eco"
    assert runtime.values["control_status"] == "storage_fault"
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await runtime.async_unload()

    Persistence.start_ok = True
    Persistence.expiry = "2099-01-01T00:00:00+00:00"
    future_entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-future-boost",
        data={"zone_uuid": "startup-future-boost", "targets": []},
        options={"profile": "boost", "control_enabled": False},
    )
    future_entry.add_to_hass(hass)
    future = ZoneRuntime(
        hass, cast(Any, future_entry), "startup-future-boost", "balanced", "boost", False
    )
    await future.async_start()
    assert future.profile == "boost"
    assert future.previous_non_boost_profile == "eco"
    assert "boost" in future.timers
    assert future.controller is not None
    await future.controller.async_wait_idle()
    await future.async_unload()


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


async def test_strategy_and_profile_changes_invalidate_active_runtime_without_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        entry_id="runtime-changes",
        data={"zone_uuid": "runtime-changes", "targets": []},
        options={"comfort_strategy": "balanced", "profile": "eco"},
    )
    entry.add_to_hass(hass)
    requested: list[Any] = []

    class Controller:
        def invalidate(self) -> None:
            requested.append("invalidate")

        def request(self, snapshot: Any) -> None:
            requested.append(snapshot)

    runtime = ZoneRuntime(hass, cast(Any, entry), "runtime-changes", "balanced", "eco", False)
    runtime.controller = cast(Any, Controller())
    cancelled: list[bool] = []
    runtime.debounce_cancel = lambda: cancelled.append(True)
    runtime.debounce_started = hass.loop.time()
    await runtime.async_set_strategy("comfort")
    assert requested[0] == "invalidate"
    assert cancelled == [True]
    assert runtime.configuration_generation == 2
    assert runtime.strategy == "comfort"
    assert entry.options["comfort_strategy"] == "comfort"

    await runtime.async_set_profile("boost")
    assert runtime.previous_non_boost_profile == "eco"
    assert "boost" in runtime.timers
    await runtime.async_set_profile("auto")
    assert runtime.profile == "auto"
    assert runtime.previous_non_boost_profile == "auto"
    assert "boost" not in runtime.timers

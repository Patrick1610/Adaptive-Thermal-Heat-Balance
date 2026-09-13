"""Repository-based Home Assistant 2026.9 API contract tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb import async_migrate_entry, async_remove_entry
from custom_components.athb.adapters.broker import ContextToken
from custom_components.athb.adapters.climate import HomeAssistantClimateService
from custom_components.athb.adapters.storage import HomeAssistantControlStorageBackend
from custom_components.athb.binary_sensor import (
    ControlEligibleBinarySensor,
    SurfaceSaturationBinarySensor,
)
from custom_components.athb.binary_sensor import async_setup_entry as async_setup_binary_entry
from custom_components.athb.button import ResumeButton
from custom_components.athb.core.climate import TARGET_TEMPERATURE, TARGET_TEMPERATURE_RANGE
from custom_components.athb.runtime import ZoneRuntime
from custom_components.athb.select import BoostModeSelect, EcoIntensitySelect, StrategySelect
from custom_components.athb.sensor import (
    DESCRIPTIONS,
    AthbSensor,
    TargetSensor,
    _target_display_name,
)
from custom_components.athb.sensor import async_setup_entry as async_setup_sensor_entry
from custom_components.athb.switch import AdaptiveControlSwitch


def _runtime() -> ZoneRuntime:
    entry = SimpleNamespace(
        title="Living room",
        entry_id="entry-1",
        data={"zone_uuid": "zone-1", "targets": ()},
        options={"comfort_strategy": "balanced"},
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    return ZoneRuntime(cast(Any, hass), cast(Any, entry), "zone-1", "balanced", "off", False)


def test_feature_bits_match_pinned_home_assistant_baseline() -> None:
    assert int(ClimateEntityFeature.TARGET_TEMPERATURE) == TARGET_TEMPERATURE == 1
    assert int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE) == TARGET_TEMPERATURE_RANGE == 2


def test_config_entry_schema_accepts_v2_and_rejects_unknown_future_version() -> None:
    assert asyncio.run(async_migrate_entry(cast(Any, None), cast(Any, SimpleNamespace(version=2))))
    assert not asyncio.run(
        async_migrate_entry(cast(Any, None), cast(Any, SimpleNamespace(version=3)))
    )


async def test_config_entry_removal_deletes_only_its_control_journal(hass: Any) -> None:
    backend = HomeAssistantControlStorageBackend(hass, "zone-remove")
    await backend.async_save('{"zone":"remove"}')
    retained = HomeAssistantControlStorageBackend(hass, "zone-retain")
    await retained.async_save('{"zone":"retain"}')

    await async_remove_entry(
        hass,
        cast(Any, SimpleNamespace(data={"zone_uuid": "zone-remove"})),
    )

    assert await backend.async_readback() is None
    assert await retained.async_readback() == '{"zone":"retain"}'


def test_climate_service_uses_exact_set_temperature_contract_and_context() -> None:
    calls: list[tuple[object, ...]] = []

    async def async_call(*args: object, **kwargs: object) -> None:
        calls.append((*args, kwargs))

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    context = Context(id="ctx-contract")
    asyncio.run(
        HomeAssistantClimateService(cast(Any, hass)).async_set_temperature(
            {"entity_id": "climate.test", "temperature": 20.0},
            ContextToken(context.id, context),
        )
    )
    assert calls == [
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.test", "temperature": 20.0},
            {"blocking": True, "context": context},
        )
    ]


def test_climate_service_rejects_non_native_context() -> None:
    hass = SimpleNamespace(services=SimpleNamespace(async_call=MagicMock()))
    service = HomeAssistantClimateService(cast(Any, hass))
    with pytest.raises(TypeError, match="Context"):
        asyncio.run(
            service.async_set_temperature(
                {"entity_id": "climate.test", "temperature": 20.0},
                ContextToken("bad", object()),
            )
        )


def test_native_entities_have_stable_zone_keys_and_no_proxy_climate() -> None:
    runtime = _runtime()
    entities = [
        AthbSensor(runtime, DESCRIPTIONS[0]),
        ControlEligibleBinarySensor(runtime),
        AdaptiveControlSwitch(runtime),
        StrategySelect(runtime),
        BoostModeSelect(runtime),
        EcoIntensitySelect(runtime),
        ResumeButton(runtime),
    ]
    assert [entity.unique_id for entity in entities] == [
        "zone-1_thermal_sensation",
        "zone-1_control_eligible",
        "zone-1_adaptive_control",
        "zone-1_comfort_strategy",
        "zone-1_boost_mode",
        "zone-1_eco_intensity",
        "zone-1_resume_control",
    ]
    assert all(entity.__class__.__module__.split(".")[-1] != "climate" for entity in entities)
    assert all(entity.device_info["identifiers"] == {("athb", "zone-1")} for entity in entities)
    assert all(entity.device_info["name"] == "Living room" for entity in entities)
    assert all(entity.device_info["manufacturer"] == "ATHB" for entity in entities)


def test_entity_presentation_groups_user_outputs_and_diagnostics_without_id_churn() -> None:
    runtime = _runtime()
    descriptions = {item.key: item for item in DESCRIPTIONS}

    assert AthbSensor(runtime, descriptions["thermal_sensation"]).entity_category is None
    assert AthbSensor(runtime, descriptions["comfort_status"]).entity_category is None
    assert AthbSensor(runtime, descriptions["outdoor_running_mean"]).entity_category is None
    assert AthbSensor(runtime, descriptions["surface_temperature"]).entity_category is None
    assert (
        AthbSensor(runtime, descriptions["heating_control_target"]).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        AthbSensor(runtime, descriptions["thermal_neutral"]).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        AthbSensor(runtime, descriptions["cooling_control_target"]).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        AthbSensor(runtime, descriptions["input_status"]).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert ControlEligibleBinarySensor(runtime).entity_category is EntityCategory.DIAGNOSTIC

    target = {
        "target_uuid": "target-1",
        "entity_id": "climate.roommind_living_room_override",
        "registry_identity": "registry-1",
    }
    effective = TargetSensor(runtime, target, "temperature", "Living Room")
    assert effective.unique_id == "zone-1_target-1_temperature"
    assert effective.translation_key == "effective_target"
    assert effective.translation_placeholders == {"target": "Living Room"}


def test_target_display_names_prefer_state_then_registry_and_never_show_raw_ids(hass: Any) -> None:
    hass.states.async_set(
        "climate.living_room", "heat", {"friendly_name": "Living Room Thermostat"}
    )
    assert _target_display_name(hass, "climate.living_room") == "Living Room Thermostat"

    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "climate", "test", "hallway", suggested_object_id="hallway", original_name="Hallway"
    )
    assert _target_display_name(hass, entry.entity_id) == "Hallway"
    assert _target_display_name(hass, "climate.roommind_bedroom_override") == (
        "Roommind Bedroom Override"
    )


async def test_full_entry_setup_registers_eco_intensity_select(
    hass: Any, enable_custom_integrations: None
) -> None:
    """Exercise HA platform forwarding and registry creation, not just the entity class."""

    del enable_custom_integrations

    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "10", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain="athb",
        title="Bedroom",
        data={
            "zone_uuid": "zone-select-platform",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [],
        },
        options={
            "comfort_strategy": "balanced",
            "boost_mode": "off",
            "eco_intensity": "workday",
            "occupancy_entity": "binary_sensor.occupancy",
        },
    )
    entry.add_to_hass(hass)

    assert await async_setup_component(hass, "athb", {})
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    registered = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "select"
    }
    assert set(registered) == {
        "zone-select-platform_comfort_strategy",
        "zone-select-platform_boost_mode",
        "zone-select-platform_eco_intensity",
    }
    eco_state = hass.states.get(registered["zone-select-platform_eco_intensity"])
    assert eco_state is not None
    assert eco_state.state == "workday"


def test_surface_values_and_inapplicable_target_endpoints_remain_truthful() -> None:
    runtime = _runtime()
    surface_temperature = next(item for item in DESCRIPTIONS if item.key == "surface_temperature")
    surface_humidity = next(
        item for item in DESCRIPTIONS if item.key == "surface_relative_humidity"
    )

    assert not AthbSensor(runtime, surface_temperature).available
    humidity_sensor = AthbSensor(runtime, surface_humidity)
    assert not humidity_sensor.available
    assert humidity_sensor.suggested_display_precision == 2
    assert SurfaceSaturationBinarySensor(runtime).is_on is None

    target = {
        "target_uuid": "target-1",
        "entity_id": "climate.target",
        "registry_identity": "registry-1",
    }
    runtime.publish({"effective_targets": {"target-1": {"temperature": 21.5}}})
    effective = TargetSensor(runtime, target, "temperature")
    assert effective.available
    assert not TargetSensor(runtime, target, "target_low").available
    assert not TargetSensor(runtime, target, "target_high").available
    runtime.publish(
        {
            "effective_targets": {"target-1": {"temperature": 18.5}},
            "effective_target_details": {
                "target-1": {
                    "mode": "fallback",
                    "reason": "running_mean_unavailable",
                    "fallback": True,
                }
            },
        }
    )
    assert effective.extra_state_attributes["mode"] == "fallback"
    assert effective.extra_state_attributes["reason"] == "running_mean_unavailable"


def test_restored_values_are_visible_but_explicitly_stale() -> None:
    runtime = _runtime()
    runtime.publish({"data_quality": "unavailable"})
    sensation = AthbSensor(runtime, DESCRIPTIONS[0])
    sensation._restored_native_value = -0.2
    assert sensation.native_value == -0.2
    assert sensation.extra_state_attributes["data_quality"] == "restored_stale"

    target = {
        "target_uuid": "target-1",
        "entity_id": "climate.target",
        "registry_identity": "registry-1",
    }
    effective = TargetSensor(runtime, target, "temperature")
    effective._restored_native_value = 19.5
    assert effective.native_value == 19.5
    assert effective.extra_state_attributes["data_quality"] == "restored_stale"

    saturation = SurfaceSaturationBinarySensor(runtime)
    saturation._restored_is_on = True
    assert saturation.is_on is True
    assert saturation.extra_state_attributes["data_quality"] == "restored_stale"


async def test_sensor_setup_exposes_only_supported_endpoints_and_removes_obsolete_entities(
    hass: Any,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    entry_id = "entry-capabilities"
    target = {
        "target_uuid": "target-1",
        "entity_id": "climate.scalar",
        "registry_identity": "registry-1",
    }
    entry = MockConfigEntry(
        domain="athb",
        entry_id=entry_id,
        data={"targets": [target]},
        options={"radiant_model": "uniform"},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    hass.states.async_set(
        "climate.scalar",
        "heat",
        {"supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE)},
    )
    registry = er.async_get(hass)
    obsolete_range = registry.async_get_or_create(
        "sensor",
        "athb",
        "zone-1_target-1_target_low",
        config_entry=entry,
        suggested_object_id="obsolete_range",
    )
    obsolete_surface = registry.async_get_or_create(
        "sensor",
        "athb",
        "zone-1_surface_temperature",
        config_entry=entry,
        suggested_object_id="obsolete_surface",
    )
    added: list[Any] = []
    await async_setup_sensor_entry(hass, cast(Any, entry), added.extend)

    unique_ids = {entity.unique_id for entity in added}
    assert "zone-1_target-1_temperature" in unique_ids
    assert "zone-1_target-1_target_low" not in unique_ids
    assert "zone-1_target-1_target_high" not in unique_ids
    assert "zone-1_surface_temperature" not in unique_ids
    assert registry.async_get(obsolete_range.entity_id) is None
    assert registry.async_get(obsolete_surface.entity_id) is None
    assert "zone-1_input_status" in unique_ids


async def test_mold_indicator_mode_exposes_surface_diagnostic_entities(hass: Any) -> None:
    runtime = _runtime()
    runtime.hass = hass
    entry = MockConfigEntry(
        domain="athb",
        entry_id="entry-mold-indicator",
        data={"targets": []},
        options={"radiant_model": "mold_indicator"},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    sensors: list[Any] = []
    binary_sensors: list[Any] = []

    await async_setup_sensor_entry(hass, cast(Any, entry), sensors.extend)
    await async_setup_binary_entry(hass, cast(Any, entry), binary_sensors.extend)

    assert "zone-1_surface_temperature" in {entity.unique_id for entity in sensors}
    assert "zone-1_surface_relative_humidity" in {entity.unique_id for entity in sensors}
    assert "zone-1_surface_saturation" in {entity.unique_id for entity in binary_sensors}


def test_strategy_select_persists_authoritative_option_without_reload() -> None:
    runtime = _runtime()
    generation = runtime.configuration_generation
    asyncio.run(StrategySelect(runtime).async_select_option("comfort"))
    runtime.hass.config_entries.async_update_entry.assert_called_once_with(
        runtime.entry,
        options={"comfort_strategy": "comfort"},
    )
    assert runtime.strategy == "comfort"
    assert runtime.configuration_generation == generation + 1
    assert not hasattr(runtime.hass.config_entries, "async_reload")


def test_eco_intensity_select_is_a_lightweight_runtime_change() -> None:
    runtime = _runtime()
    generation = runtime.configuration_generation

    asyncio.run(EcoIntensitySelect(runtime).async_select_option("workday"))

    runtime.hass.config_entries.async_update_entry.assert_called_once_with(
        runtime.entry,
        options={"comfort_strategy": "balanced", "eco_intensity": "workday"},
    )
    assert runtime.eco_intensity == "workday"
    assert runtime.configuration_generation == generation + 1
    assert runtime.explicit_transition
    assert not hasattr(runtime.hass.config_entries, "async_reload")


def test_native_entity_actions_and_values_delegate_to_one_runtime() -> None:
    runtime = _runtime()
    runtime.publish({"control_eligible": True, "thermal_sensation": 0.25})
    sensation = AthbSensor(runtime, DESCRIPTIONS[0])
    assert sensation.native_value == 0.25
    assert sensation.available
    assert sensation.extra_state_attributes["comfort_level"] == "balanced"
    assert sensation.extra_state_attributes["suppression_reason"] is None
    assert ControlEligibleBinarySensor(runtime).is_on
    asyncio.run(BoostModeSelect(runtime).async_select_option("rapid"))
    assert runtime.boost_mode == "rapid"
    asyncio.run(ResumeButton(runtime).async_press())
    assert runtime.values["resume_requested"] is True
    switch = AdaptiveControlSwitch(runtime)
    asyncio.run(switch.async_turn_on())
    assert switch.is_on
    asyncio.run(switch.async_turn_off())
    assert not switch.is_on

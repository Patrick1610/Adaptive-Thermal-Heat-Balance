"""Repository-based Home Assistant 2026.9 API contract tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import Context

from custom_components.athb import async_migrate_entry, async_remove_entry
from custom_components.athb.adapters.broker import ContextToken
from custom_components.athb.adapters.climate import HomeAssistantClimateService
from custom_components.athb.adapters.storage import HomeAssistantControlStorageBackend
from custom_components.athb.binary_sensor import (
    ControlEligibleBinarySensor,
    SurfaceSaturationBinarySensor,
)
from custom_components.athb.button import ResumeButton
from custom_components.athb.core.climate import TARGET_TEMPERATURE, TARGET_TEMPERATURE_RANGE
from custom_components.athb.runtime import ZoneRuntime
from custom_components.athb.select import ProfileSelect, StrategySelect
from custom_components.athb.sensor import DESCRIPTIONS, AthbSensor, TargetSensor
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
    return ZoneRuntime(cast(Any, hass), cast(Any, entry), "zone-1", "balanced", "comfort", False)


def test_feature_bits_match_pinned_home_assistant_baseline() -> None:
    assert int(ClimateEntityFeature.TARGET_TEMPERATURE) == TARGET_TEMPERATURE == 1
    assert int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE) == TARGET_TEMPERATURE_RANGE == 2


def test_config_entry_schema_accepts_v1_and_rejects_unknown_future_version() -> None:
    assert asyncio.run(async_migrate_entry(cast(Any, None), cast(Any, SimpleNamespace(version=1))))
    assert not asyncio.run(
        async_migrate_entry(cast(Any, None), cast(Any, SimpleNamespace(version=2)))
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
        ProfileSelect(runtime),
        ResumeButton(runtime),
    ]
    assert [entity.unique_id for entity in entities] == [
        "zone-1_thermal_sensation",
        "zone-1_control_eligible",
        "zone-1_adaptive_control",
        "zone-1_comfort_strategy",
        "zone-1_profile",
        "zone-1_resume_control",
    ]
    assert all(entity.__class__.__module__.split(".")[-1] != "climate" for entity in entities)
    assert all(entity.device_info["identifiers"] == {("athb", "zone-1")} for entity in entities)
    assert all(entity.device_info["name"] == "Living room" for entity in entities)
    assert all(entity.device_info["manufacturer"] == "ATHB" for entity in entities)


def test_optional_surface_entities_are_disabled_and_inapplicable_targets_unavailable() -> None:
    runtime = _runtime()
    surface_temperature = next(item for item in DESCRIPTIONS if item.key == "surface_temperature")
    surface_humidity = next(
        item for item in DESCRIPTIONS if item.key == "surface_relative_humidity"
    )

    assert AthbSensor(runtime, surface_temperature).entity_registry_enabled_default is False
    assert AthbSensor(runtime, surface_humidity).entity_registry_enabled_default is False
    assert SurfaceSaturationBinarySensor(runtime).entity_registry_enabled_default is False

    target = {
        "target_uuid": "target-1",
        "entity_id": "climate.target",
        "registry_identity": "registry-1",
    }
    runtime.publish({"effective_targets": {"target-1": {"temperature": 21.5}}})
    assert TargetSensor(runtime, target, "temperature").available
    assert not TargetSensor(runtime, target, "target_low").available
    assert not TargetSensor(runtime, target, "target_high").available


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


def test_native_entity_actions_and_values_delegate_to_one_runtime() -> None:
    runtime = _runtime()
    runtime.publish({"control_eligible": True, "thermal_sensation": 0.25})
    sensation = AthbSensor(runtime, DESCRIPTIONS[0])
    assert sensation.native_value == 0.25
    assert sensation.available
    assert sensation.extra_state_attributes["comfort_strategy"] == "balanced"
    assert sensation.extra_state_attributes["suppression_reason"] is None
    assert ControlEligibleBinarySensor(runtime).is_on
    asyncio.run(ProfileSelect(runtime).async_select_option("boost"))
    assert runtime.profile == "boost"
    asyncio.run(ResumeButton(runtime).async_press())
    assert runtime.values["resume_requested"] is True
    switch = AdaptiveControlSwitch(runtime)
    asyncio.run(switch.async_turn_on())
    assert switch.is_on
    asyncio.run(switch.async_turn_off())
    assert not switch.is_on

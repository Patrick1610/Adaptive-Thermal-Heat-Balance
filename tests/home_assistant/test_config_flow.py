"""Native config-flow and lifecycle integration tests."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb.const import DOMAIN, PLATFORMS


async def _complete_flow(hass: HomeAssistant) -> config_entries.ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living room"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.room",
            "rh_mode": "declared",
            "rh_declared": 47.0,
            "outdoor_source": "sensor.outdoor",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": ["climate.target"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"comfort_strategy": "balanced"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"minimum_control_temperature": 18.0, "maximum_control_temperature": 26.0},
    )


async def test_config_flow_stores_tagged_declaration_stable_target_identity_and_disabled_control(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    registry = er.async_get(hass)
    target = registry.async_get_or_create(
        "climate", "test", "target-unique", suggested_object_id="target"
    )
    result = await _complete_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    assert result["data"]["rh_mode"] == "declared"
    assert result["data"]["rh_declared"] == 47.0
    assert "rh_entity" not in result["data"]
    stored_target = result["data"]["targets"][0]
    assert stored_target["registry_identity"] == target.id
    assert stored_target["target_uuid"]
    assert result["options"]["comfort_strategy"] == "balanced"
    assert result["options"]["control_enabled"] is False


async def test_config_flow_rejects_invalid_bounds_without_creating_entry(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    er.async_get(hass).async_get_or_create(
        "climate", "test", "target-unique", suggested_object_id="target"
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Zone"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.room",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "outdoor_source": "sensor.outdoor",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": ["climate.target"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"comfort_strategy": "balanced"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"minimum_control_temperature": 26.0, "maximum_control_temperature": 18.0},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_control_bounds"}


async def test_setup_forwards_five_native_platforms_and_unloads_cleanly(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={"zone_uuid": "zone-1", "targets": []},
        options={"comfort_strategy": "balanced", "control_enabled": False},
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zone_uuid == "zone-1"
    assert all(hass.config_entries.async_loaded_entries(DOMAIN))
    assert {platform.value for platform in PLATFORMS} == {
        "sensor",
        "binary_sensor",
        "switch",
        "select",
        "button",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_use_uniform_default_and_progressively_disclose_selected_radiant_source(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-1", "targets": []},
        options={"comfort_strategy": "balanced", "profile": "comfort"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"comfort_strategy": "comfort", "profile": "eco", "radiant_model": "uniform"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["radiant_model"] == "uniform"
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"comfort_strategy": "balanced", "profile": "comfort", "radiant_model": "surface"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "radiant"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"surface_temperature_entity": "sensor.surface"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["surface_temperature_entity"] == "sensor.surface"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_preserves_target_uuid_for_registry_identity(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    registry = er.async_get(hass)
    target = registry.async_get_or_create(
        "climate", "test", "target-unique", suggested_object_id="target"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "zone_uuid": "zone-1",
            "primary_temperature": "sensor.old",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 40.0,
            "targets": [
                {
                    "target_uuid": "stable-target-uuid",
                    "entity_id": target.entity_id,
                    "registry_identity": target.id,
                }
            ],
        },
        options={"comfort_strategy": "balanced"},
        unique_id="zone-1",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.new",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 45.0,
            "targets": [target.entity_id],
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.data["targets"][0]["target_uuid"] == "stable-target-uuid"
    assert entry.data["primary_temperature"] == "sensor.new"

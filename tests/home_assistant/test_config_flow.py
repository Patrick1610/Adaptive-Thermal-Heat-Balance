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
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_advanced_options_validate_and_store_modelled_surface_and_target_calibration(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "zone_uuid": "zone-advanced",
            "targets": [
                {
                    "target_uuid": "target-stable",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-target",
                }
            ],
        },
        options={"comfort_strategy": "balanced", "profile": "comfort"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "comfort",
            "radiant_model": "surface",
            "advanced_settings": True,
        },
    )
    assert result["step_id"] == "radiant"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "surface_modelled": True,
            "surface_f_rsi": 0.65,
            "surface_view_factor": 0.25,
            "surface_rh_threshold_pct": 80.0,
        },
    )
    assert result["step_id"] == "advanced"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "lower_comfort_vote": -0.5,
            "upper_comfort_vote": 0.5,
            "running_mean_alpha": 0.8,
            "eco_heating_setback_c": 2.0,
            "eco_cooling_setback_c": 2.0,
            "boost_delta_c": 1.0,
            "boost_duration_minutes": 60.0,
            "manual_override_minutes": 120.0,
            "minimum_range_gap": 1.0,
            "minimum_meaningful_change": 0.1,
            "feedback_resolution": 0.01,
            "reject_extrapolation": False,
            "auto_mapping": "unmapped",
            "critical_locations_json": (
                '[{"location_id":"seat","entity_id":"sensor.seat","mode":"heating"}]'
            ),
            "calibration_target-stable": 0.5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["surface_modelled"] is True
    assert result["data"]["surface_f_rsi"] == 0.65
    assert result["data"]["critical_locations"][0]["location_id"] == "seat"
    assert result["data"]["calibration_target-stable"] == 0.5
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_reports_invalid_environment_and_target_registration(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Zone"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.room",
            "rh_mode": "measured",
            "outdoor_source": "sensor.outdoor",
        },
    )
    assert result["errors"] == {"rh_entity": "invalid_rh_source"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.room",
            "rh_mode": "measured",
            "rh_entity": "sensor.rh",
            "outdoor_source": "sensor.outdoor",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": ["climate.not_registered"]}
    )
    assert result["errors"] == {"targets": "target_not_registered"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"targets": []})
    assert result["errors"] == {"targets": "invalid_targets"}


async def test_config_flow_rejects_target_claimed_by_enabled_entry(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    registry = er.async_get(hass)
    target = registry.async_get_or_create(
        "climate", "test", "shared-target", suggested_object_id="shared_target"
    )
    claimed = MockConfigEntry(
        domain=DOMAIN,
        data={
            "zone_uuid": "claimed-zone",
            "targets": [
                {
                    "target_uuid": "claimed-target",
                    "entity_id": target.entity_id,
                    "registry_identity": target.id,
                }
            ],
        },
        options={"control_enabled": True},
    )
    claimed.add_to_hass(hass)
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
        result["flow_id"], {"targets": [target.entity_id]}
    )
    assert result["errors"] == {"targets": "target_already_controlled"}


async def test_radiant_options_cover_direct_globe_and_surface_validation(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-radiant", "targets": []},
        options={"comfort_strategy": "balanced", "profile": "comfort"},
    )
    entry.add_to_hass(hass)
    for model, fields, expected_key in (
        ("direct_mrt", {"mrt_entity": "sensor.mrt"}, "mrt_entity"),
        (
            "globe",
            {
                "globe_temperature_entity": "sensor.globe",
                "globe_diameter_m": 0.15,
                "globe_emissivity": 0.95,
            },
            "globe_temperature_entity",
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"comfort_strategy": "balanced", "profile": "comfort", "radiant_model": model},
        )
        result = await hass.config_entries.options.async_configure(result["flow_id"], fields)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][expected_key] == fields[expected_key]
        await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"comfort_strategy": "balanced", "profile": "comfort", "radiant_model": "surface"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"surface_modelled": False}
    )
    assert result["errors"] == {"surface_temperature_entity": "required"}
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_advanced_options_reject_bad_json_and_out_of_range_value(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-invalid-advanced", "targets": []},
        options={"comfort_strategy": "balanced", "profile": "comfort"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "comfort",
            "radiant_model": "uniform",
            "advanced_settings": True,
        },
    )
    assert result["step_id"] == "advanced"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"critical_locations_json": "{"}
    )
    assert result["errors"] == {"critical_locations_json": "invalid_option"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"critical_locations_json": "[]", "met": 10.0}
    )
    assert result["errors"]["met"] == "invalid_option"

"""Native config-flow and lifecycle integration tests."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb.config_flow import AthbOptionsFlow
from custom_components.athb.const import DOMAIN, PLATFORMS


def _schema_keys(result: config_entries.ConfigFlowResult) -> set[str]:
    return {str(marker.schema) for marker in result["data_schema"].schema}


def _suggested_value(result: config_entries.ConfigFlowResult, key: str) -> Any:
    marker = next(item for item in result["data_schema"].schema if str(item.schema) == key)
    return marker.description["suggested_value"]


async def _submit_everyday_controls(
    hass: HomeAssistant,
    result: config_entries.ConfigFlowResult,
    *,
    minimum: float = 18.0,
    maximum: float = 26.0,
    options: bool = False,
) -> config_entries.ConfigFlowResult:
    assert result["step_id"] == "control_limits"
    assert _schema_keys(result) == {
        "minimum_control_temperature",
        "maximum_control_temperature",
        "manual_override_minutes",
        "boost_delta_c",
        "boost_duration_minutes",
    }
    manager = hass.config_entries.options if options else hass.config_entries.flow
    return await manager.async_configure(
        result["flow_id"],
        {
            "minimum_control_temperature": minimum,
            "maximum_control_temperature": maximum,
            "manual_override_minutes": 120.0,
            "boost_delta_c": 1.0,
            "boost_duration_minutes": 60.0,
        },
    )


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
            "outdoor_source": "sensor.outdoor",
        },
    )
    assert result["step_id"] == "humidity"
    assert _schema_keys(result) == {"rh_declared"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rh_declared": 47.0}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": ["climate.target"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "comfort",
            "radiant_model": "uniform",
            "advanced_settings": False,
        },
    )
    result = await _submit_everyday_controls(hass, result)
    assert result["step_id"] == "review"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


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
    assert result["options"]["eco_intensity"] == "mild"
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
            "outdoor_source": "sensor.outdoor",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rh_declared": 50.0}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": ["climate.target"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "comfort",
            "radiant_model": "uniform",
            "advanced_settings": True,
        },
    )
    assert result["step_id"] == "control_limits"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "minimum_control_temperature": 26.0,
            "maximum_control_temperature": 18.0,
            "manual_override_minutes": 120.0,
            "boost_delta_c": 1.0,
            "boost_duration_minutes": 60.0,
        },
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
        {
            "comfort_strategy": "comfort",
            "profile": "eco",
            "radiant_model": "uniform",
            "advanced_settings": False,
        },
    )
    result = await _submit_everyday_controls(hass, result, options=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["radiant_model"] == "uniform"
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "comfort",
            "radiant_model": "mold_indicator",
            "advanced_settings": False,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "radiant"
    assert _schema_keys(result) == {"mold_indicator_entity"}
    mold_marker = next(
        marker
        for marker in result["data_schema"].schema
        if str(marker.schema) == "mold_indicator_entity"
    )
    assert result["data_schema"].schema[mold_marker].config["integration"] == "mold_indicator"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mold_indicator_entity": "sensor.mold_indicator"}
    )
    result = await _submit_everyday_controls(hass, result, options=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mold_indicator_entity"] == "sensor.mold_indicator"
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
            "rh_mode": "measured",
            "rh_entity": "sensor.old_humidity",
            "targets": [
                {
                    "target_uuid": "stable-target-uuid",
                    "entity_id": target.entity_id,
                    "registry_identity": target.id,
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "occupancy_entity": "binary_sensor.old_occupancy",
        },
        unique_id="zone-1",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_keys(result) == {"name"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living room updated"}
    )
    assert result["step_id"] == "environment"
    assert _schema_keys(result) == {
        "primary_temperature",
        "rh_mode",
        "outdoor_source",
    }
    assert _suggested_value(result, "primary_temperature") == "sensor.old"
    assert _suggested_value(result, "outdoor_source") == "sensor.outdoor"
    assert _suggested_value(result, "rh_mode") == "measured"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "primary_temperature": "sensor.new",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
        },
    )
    assert result["step_id"] == "humidity"
    assert _schema_keys(result) == {"rh_declared"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rh_declared": 45.0}
    )
    assert result["step_id"] == "targets"
    assert _suggested_value(result, "targets") == [target.entity_id]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": [target.entity_id]}
    )
    assert result["step_id"] == "preferences"
    assert {"comfort_strategy", "profile", "radiant_model", "advanced_settings"} <= _schema_keys(
        result
    )
    assert _suggested_value(result, "occupancy_entity") == "binary_sensor.old_occupancy"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "comfort",
            "profile": "eco",
            "radiant_model": "uniform",
            "advanced_settings": False,
        },
    )
    result = await _submit_everyday_controls(hass, result)
    assert result["step_id"] == "review"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert entry.title == "Living room updated"
    assert entry.data["targets"][0]["target_uuid"] == "stable-target-uuid"
    assert entry.data["primary_temperature"] == "sensor.new"
    assert entry.data["rh_declared"] == 45.0
    assert "rh_entity" not in entry.data
    assert entry.options["profile"] == "eco"
    assert "occupancy_entity" not in entry.options
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_advanced_options_store_mold_indicator_and_target_calibration(
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
            "eco_intensity": "custom",
            "radiant_model": "mold_indicator",
            "advanced_settings": True,
        },
    )
    assert result["step_id"] == "radiant"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"mold_indicator_entity": "sensor.mold_indicator"},
    )
    result = await _submit_everyday_controls(hass, result, options=True)
    assert result["step_id"] == "advanced_model"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"met": 1.1, "clothing_mode": "automatic", "air_speed_mode": "fixed"},
    )
    assert result["step_id"] == "air_speed"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"air_speed_m_s": 0.1}
    )
    assert result["step_id"] == "comfort_parameters"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "lower_comfort_vote": -0.5,
            "upper_comfort_vote": 0.5,
            "running_mean_alpha": 0.8,
            "reject_extrapolation": False,
        },
    )
    assert result["step_id"] == "profile_parameters"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "eco_heating_setback_c": 2.0,
            "eco_cooling_setback_c": 2.0,
        },
    )
    assert result["step_id"] == "command_behavior"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "minimum_range_gap": 1.0,
            "minimum_meaningful_change": 0.1,
            "feedback_resolution": 0.01,
            "fallback_mode": "fixed",
        },
    )
    assert result["step_id"] == "fallback_temperatures"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fallback_heating_c": 18.0, "fallback_cooling_c": 26.0}
    )
    assert result["step_id"] == "critical_locations"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"critical_location_count": 1}
    )
    assert result["step_id"] == "critical_location"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "location_id": "seat",
            "location_mode": "heating",
            "location_entity": "sensor.seat",
        },
    )
    assert result["step_id"] == "source_freshness"
    assert _schema_keys(result) == {
        "primary_temperature_freshness_minutes",
        "local_temperature_freshness_minutes",
        "radiant_freshness_minutes",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "primary_temperature_freshness_minutes": 120.0,
            "local_temperature_freshness_minutes": 60.0,
            "radiant_freshness_minutes": 90.0,
        },
    )
    assert result["step_id"] == "target_calibration"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"calibration_offset_c": 0.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mold_indicator_entity"] == "sensor.mold_indicator"
    assert result["data"]["critical_locations"][0]["location_id"] == "seat"
    assert result["data"]["primary_temperature_freshness_minutes"] == 120.0
    assert result["data"]["local_temperature_freshness_minutes"] == 60.0
    assert result["data"]["radiant_freshness_minutes"] == 90.0
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
    assert result["step_id"] == "humidity"
    assert _schema_keys(result) == {"rh_entity"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rh_entity": "sensor.rh"}
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
            "outdoor_source": "sensor.outdoor",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rh_declared": 50.0}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"targets": [target.entity_id]}
    )
    assert result["errors"] == {"targets": "target_already_controlled"}


async def test_room_model_only_offers_standard_and_mold_indicator(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-radiant", "targets": []},
        options={"comfort_strategy": "balanced", "profile": "comfort"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    model_marker = next(marker for marker in schema if str(marker.schema) == "radiant_model")
    model_selector = schema[model_marker]
    assert model_selector.config["options"] == ["uniform", "mold_indicator"]

    strategy_marker = next(marker for marker in schema if str(marker.schema) == "comfort_strategy")
    profile_marker = next(marker for marker in schema if str(marker.schema) == "profile")
    setback_marker = next(marker for marker in schema if str(marker.schema) == "eco_intensity")
    assert schema[strategy_marker].config["options"] == ["efficient", "balanced", "comfort"]
    assert schema[profile_marker].config["options"] == ["eco", "auto", "comfort", "boost"]
    assert schema[setback_marker].config["options"] == ["deep", "workday", "mild", "custom"]
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_advanced_options_only_show_fields_required_by_selected_modes(
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
    result = await _submit_everyday_controls(hass, result, options=True)
    assert result["step_id"] == "advanced_model"
    assert _schema_keys(result) == {"met", "clothing_mode", "air_speed_mode"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"met": 1.1, "clothing_mode": "fixed", "air_speed_mode": "measured"},
    )
    assert result["step_id"] == "clothing"
    assert _schema_keys(result) == {"fixed_clothing_clo"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fixed_clothing_clo": 0.8}
    )
    assert result["step_id"] == "air_speed"
    assert _schema_keys(result) == {"air_speed_entity"}


def test_freshness_page_includes_selected_mold_indicator() -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-freshness", "rh_mode": "measured", "targets": []},
        options={
            "radiant_model": "mold_indicator",
            "mold_indicator_entity": "sensor.mold_indicator",
            "air_speed_mode": "measured",
            "air_speed_entity": "sensor.air_speed",
        },
    )

    flow = AthbOptionsFlow(entry)
    keys = {str(marker.schema) for marker in flow._source_freshness_schema().schema}

    assert keys == {
        "primary_temperature_freshness_minutes",
        "relative_humidity_freshness_minutes",
        "radiant_freshness_minutes",
        "air_speed_freshness_minutes",
    }


async def test_max_setback_uses_command_limits_without_duplicate_fields(
    hass: HomeAssistant, enable_custom_integrations: Any
) -> None:
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"zone_uuid": "zone-deep", "targets": []},
        options={
            "comfort_strategy": "balanced",
            "profile": "eco",
            "eco_intensity": "deep",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "comfort_strategy": "balanced",
            "profile": "eco",
            "eco_intensity": "deep",
            "radiant_model": "uniform",
            "advanced_settings": True,
        },
    )
    assert result["step_id"] == "control_limits"
    assert _schema_keys(result) == {
        "minimum_control_temperature",
        "maximum_control_temperature",
        "manual_override_minutes",
        "boost_delta_c",
        "boost_duration_minutes",
    }
    result = await _submit_everyday_controls(hass, result, options=True)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"met": 1.1, "clothing_mode": "automatic", "air_speed_mode": "fixed"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"air_speed_m_s": 0.1}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "command_behavior"

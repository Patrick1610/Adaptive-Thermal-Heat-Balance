"""Target identities survive startup order, mode changes and reloads."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb.runtime import ZoneRuntime
from custom_components.athb.sensor import AthbSensor, Description, async_setup_entry
from custom_components.athb.target_sensor_layout import LAYOUT_KEY


def _entry(hass: Any, targets: list[dict[str, str]] | None = None) -> Any:
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-layout",
            "targets": targets or [{"target_uuid": "target-one", "entity_id": "climate.room"}],
        },
        options={"radiant_model": "uniform", "boost_delta_c": 1.0},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = ZoneRuntime(
        hass, cast(Any, entry), "zone-layout", "balanced", "off", False
    )
    return entry


def _state(hass: Any, *, ranged: bool = False, mode: str = "heat") -> None:
    hass.states.async_set(
        "climate.room",
        mode,
        {
            "supported_features": int(
                ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                if ranged
                else ClimateEntityFeature.TARGET_TEMPERATURE
            ),
            "hvac_modes": ["heat_cool", "off"] if ranged else ["heat", "off"],
        },
    )


@pytest.mark.parametrize("layout_kind", ["compact", "legacy", "ranged"])
@pytest.mark.parametrize("later_state", ["missing", "unavailable", "off", "cool"])
async def test_reload_preserves_unique_and_entity_ids(
    hass: Any, layout_kind: str, later_state: str
) -> None:
    entry = _entry(hass)
    _state(hass, ranged=layout_kind == "ranged")
    registry = er.async_get(hass)
    if layout_kind == "legacy":
        for suffix in ("target-one_temperature", "target_heating_deviation"):
            registry.async_get_or_create(
                "sensor", "athb", f"zone-layout_{suffix}", config_entry=entry
            )
    original_options = dict(entry.options)
    first: list[Any] = []
    await async_setup_entry(hass, entry, first.extend)
    layout = dict(entry.data[LAYOUT_KEY])
    for entity in first:
        registry.async_get_or_create("sensor", "athb", entity.unique_id, config_entry=entry)
    original_ids = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    if later_state == "missing":
        hass.states.async_remove("climate.room")
    elif later_state == "unavailable":
        hass.states.async_set("climate.room", "unavailable")
    else:
        _state(hass, mode=later_state)
    second: list[Any] = []
    await async_setup_entry(hass, entry, second.extend)

    assert {item.unique_id for item in second} == {item.unique_id for item in first}
    assert entry.data[LAYOUT_KEY] == layout
    assert dict(entry.options) == original_options
    assert {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == original_ids


@pytest.mark.parametrize("compact", [True, False])
async def test_first_upgrade_preserves_registered_layout_without_climate(
    hass: Any, compact: bool
) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)
    suffix = "target_current" if compact else "target-one_temperature"
    original = registry.async_get_or_create(
        "sensor", "athb", f"zone-layout_{suffix}", config_entry=entry
    )
    registry.async_get_or_create(
        "sensor", "athb", "zone-layout_target_heating_deviation", config_entry=entry
    )
    added: list[Any] = []

    await async_setup_entry(hass, entry, added.extend)

    assert entry.data[LAYOUT_KEY]["compact"] is compact
    assert original.unique_id in {item.unique_id for item in added}
    assert registry.async_get(original.entity_id) is not None


async def test_registered_endpoint_survives_missing_direction_and_climate(hass: Any) -> None:
    """An offline legacy target may never have had a deviation sensor."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "sensor", "athb", "zone-layout_target-one_temperature", config_entry=entry
    )
    added: list[Any] = []
    await async_setup_entry(hass, entry, added.extend)
    assert original.unique_id in {item.unique_id for item in added}
    assert entry.data[LAYOUT_KEY]["directions"] == []
    assert registry.async_get(original.entity_id) is not None


async def test_new_missing_target_waits_then_adds_once(hass: Any) -> None:
    entry = _entry(hass)
    added: list[Any] = []
    await async_setup_entry(hass, entry, added.extend)
    assert LAYOUT_KEY not in entry.data
    assert not any(item.unique_id.endswith("target_current") for item in added)

    _state(hass, ranged=True, mode="heat_cool")
    await hass.async_block_till_done()
    assert entry.data[LAYOUT_KEY]["endpoints"] == {"target-one": ["target_low", "target_high"]}
    assert any(item.unique_id.endswith("target-one_target_high") for item in added)
    count = len(added)
    hass.states.async_set("climate.room", "unavailable")
    await hass.async_block_till_done()
    assert len(added) == count


async def test_waiting_listener_is_cancelled_on_unload(hass: Any) -> None:
    entry = _entry(hass)
    added: list[Any] = []
    with patch.object(entry, "async_on_unload") as on_unload:
        await async_setup_entry(hass, entry, added.extend)
    count = len(added)
    cancel = on_unload.call_args.args[0]
    cancel()
    cancel()
    _state(hass)
    await hass.async_block_till_done()
    assert len(added) == count
    assert LAYOUT_KEY not in entry.data


async def test_invalid_metadata_preserves_registered_identities(hass: Any) -> None:
    entry = _entry(hass)
    registry = er.async_get(hass)
    for suffix in ("target-one_temperature", "target_heating_deviation"):
        registry.async_get_or_create("sensor", "athb", f"zone-layout_{suffix}", config_entry=entry)
    hass.config_entries.async_update_entry(entry, data={**entry.data, LAYOUT_KEY: "invalid"})
    _state(hass)
    added: list[Any] = []
    await async_setup_entry(hass, entry, added.extend)
    assert not entry.data[LAYOUT_KEY]["compact"]
    assert any(item.unique_id == "zone-layout_target-one_temperature" for item in added)


async def test_existing_input_status_explains_layout_readiness(hass: Any) -> None:
    entry = _entry(hass)
    sensor = AthbSensor(entry.runtime_data, Description("input_status"))
    assert (
        sensor.extra_state_attributes["target_sensor_layout_status"] == "waiting_for_capabilities"
    )
    _state(hass)
    await async_setup_entry(hass, entry, lambda _entities: None)
    assert sensor.extra_state_attributes["target_sensor_layout_status"] == "fixed"
    assert sensor.extra_state_attributes["target_sensor_layout"] == entry.data[LAYOUT_KEY]


async def test_changed_configured_targets_can_migrate_layout(hass: Any) -> None:
    entry = _entry(hass)
    _state(hass)
    await async_setup_entry(hass, entry, lambda _entities: None)
    registry = er.async_get(hass)
    obsolete = registry.async_get_or_create(
        "sensor", "athb", "zone-layout_target_current", config_entry=entry
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "targets": [{"target_uuid": "target-two", "entity_id": "climate.room"}],
        },
    )
    _state(hass, ranged=True, mode="heat_cool")
    added: list[Any] = []

    await async_setup_entry(hass, entry, added.extend)

    assert entry.data[LAYOUT_KEY]["targets"] == ["target-two"]
    assert not entry.data[LAYOUT_KEY]["compact"]
    assert any(item.unique_id.endswith("target-two_target_low") for item in added)
    assert registry.async_get(obsolete.entity_id) is None


async def test_bidirectional_scalar_layout_does_not_depend_on_current_mode(hass: Any) -> None:
    entry = _entry(hass)
    hass.states.async_set(
        "climate.room",
        "heat",
        {"supported_features": 1, "hvac_modes": ["off", "heat", "cool", "auto"]},
    )
    added: list[Any] = []
    await async_setup_entry(hass, entry, added.extend)
    assert entry.data[LAYOUT_KEY]["directions"] == ["heating", "cooling"]
    assert not entry.data[LAYOUT_KEY]["compact"]
    deviations = {item.direction: item for item in added if hasattr(item, "direction")}
    entry.runtime_data.values["comfort_range_current"] = 20.0
    entry.runtime_data.values["target_scenarios"] = {
        "target-one": {"direction": "heating_only", "current": {"room": {"temperature": 21.0}}}
    }
    assert deviations["heating"].native_value == -1.0
    assert deviations["cooling"].native_value is None
    entry.runtime_data.values["target_scenarios"] = {
        "target-one": {"direction": "cooling_only", "current": {"room": {"temperature": 19.0}}}
    }
    assert deviations["cooling"].native_value == 1.0
    assert deviations["heating"].native_value is None

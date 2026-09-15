"""Regression matrix derived from anonymized real Home Assistant diagnostics."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.athb.config_flow import OPTION_DEFAULTS
from custom_components.athb.config_schema import validate_options
from custom_components.athb.runtime import ZoneRuntime

FIXTURE = Path(__file__).parents[1] / "fixtures" / "home_assistant_lifecycle_configs.json"
SCENARIOS = json.loads(FIXTURE.read_text())


def test_realistic_diagnostic_matrix_is_valid_and_materially_diverse() -> None:
    scenarios = json.loads(FIXTURE.read_text())

    assert [item["name"] for item in scenarios] == [
        "living_room",
        "bathroom",
        "child_bedroom",
        "office",
        "master_bedroom",
        "hallway",
        "no_occupancy_zone",
    ]
    assert all(not validate_options({**OPTION_DEFAULTS, **item["options"]}) for item in scenarios)
    assert len({item["options"]["minimum_control_temperature"] for item in scenarios}) >= 4
    assert len({item["options"]["manual_override_minutes"] for item in scenarios}) >= 6
    assert {item["options"]["fallback_mode"] for item in scenarios} == {"fixed", "no_write"}
    assert {item["options"]["comfort_strategy"] for item in scenarios} == {
        "eco",
        "efficient",
        "balanced",
        "comfort",
        "near_neutral",
    }


def test_living_room_fixture_preserves_delivered_baseline_and_new_defaults() -> None:
    living = json.loads(FIXTURE.read_text())[0]

    assert living["template"] == "delivered_diagnostic"
    assert living["options"]["minimum_control_temperature"] == 15.0
    assert living["options"]["maximum_control_temperature"] == 30.0
    assert living["options"]["boost_delta_c"] == 2.0
    assert living["options"]["fallback_heating_c"] == 18.0
    assert living["options"]["fallback_cooling_c"] == 27.0
    assert living["options"]["primary_temperature_freshness_minutes"] == 30.0


def test_new_wizard_defaults_match_the_approved_safe_baseline() -> None:
    assert OPTION_DEFAULTS["minimum_control_temperature"] == 15.0
    assert OPTION_DEFAULTS["maximum_control_temperature"] == 30.0
    assert OPTION_DEFAULTS["boost_delta_c"] == 2.0
    assert OPTION_DEFAULTS["fallback_heating_c"] == 18.0
    assert OPTION_DEFAULTS["fallback_cooling_c"] == 27.0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item["name"])
@pytest.mark.freeze_time("2026-09-15 08:00:00+00:00")
async def test_each_diagnostic_scenario_completes_a_real_ha_lifecycle(
    hass: HomeAssistant, freezer: Any, scenario: dict[str, Any]
) -> None:
    """Exercise startup, failed input, recovery and the target-only command boundary."""

    name = str(scenario["name"])
    primary_id = f"sensor.{name}_temperature"
    humidity_id = f"sensor.{name}_humidity"
    outdoor_id = f"sensor.{name}_outdoor"
    target_id = f"climate.{name}"
    target_uuid = f"target-{name}"
    registry_identity = f"registry-{name}"
    options = {**OPTION_DEFAULTS, **scenario["options"]}
    starts_missing = name == "bathroom"
    humidity_starts_missing = name == "no_occupancy_zone"

    if not starts_missing:
        hass.states.async_set(primary_id, "20.0", {"unit_of_measurement": "°C"})
    if not humidity_starts_missing:
        hass.states.async_set(humidity_id, "50.0", {"unit_of_measurement": "%"})
    hass.states.async_set(outdoor_id, "8.0", {"unit_of_measurement": "°C"})
    if options.get("radiant_model") == "mold_indicator":
        hass.states.async_set(
            str(options["mold_indicator_entity"]),
            "70",
            {"estimated_critical_temp": 16.5},
        )
    target_attributes: dict[str, Any] = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "min_temp": 5.0,
        "max_temp": 35.0,
        "target_temp_step": 0.1,
        "temperature": 10.0,
        "unit_of_measurement": "°C",
    }
    hass.states.async_set(target_id, "heat", target_attributes)
    calls: list[ServiceCall] = []

    async def acknowledge(call: ServiceCall) -> None:
        calls.append(call)
        target_attributes["temperature"] = call.data["temperature"]
        hass.states.async_set(target_id, "heat", target_attributes, context=call.context)

    hass.services.async_register("climate", "set_temperature", acknowledge)
    data: dict[str, Any] = {
        "zone_uuid": f"zone-{name}",
        "primary_temperature": primary_id,
        "outdoor_source": outdoor_id,
        "rh_mode": "measured",
        "rh_entity": humidity_id,
        "targets": [
            {
                "target_uuid": target_uuid,
                "entity_id": target_id,
                "registry_identity": registry_identity,
            }
        ],
    }
    entry = MockConfigEntry(
        domain="athb", title=name.replace("_", " ").title(), data=data, options=options
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(
        hass,
        cast(Any, entry),
        str(data["zone_uuid"]),
        str(options["comfort_strategy"]),
        str(options["boost_mode"]),
        bool(options["control_enabled"]),
        str(options.get("eco_intensity", "custom")),
    )

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    if starts_missing or humidity_starts_missing:
        assert runtime.values["input_status"] != "ready"
        missing_id = humidity_id if humidity_starts_missing else primary_id
        missing_value = "50.0" if humidity_starts_missing else "20.0"
        missing_unit = "%" if humidity_starts_missing else "°C"
        hass.states.async_set(missing_id, missing_value, {"unit_of_measurement": missing_unit})
    else:
        assert runtime.values["input_status"] in {"ready", "running_mean_unavailable"}
        freshness = float(options["primary_temperature_freshness_minutes"])
        freezer.tick(delta=timedelta(minutes=freshness + 1.0))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await runtime.controller.async_wait_idle()
        assert runtime.values["input_status"] == "primary_temperature_stale"
        hass.states.async_set(primary_id, "20.1", {"unit_of_measurement": "°C"})
        # Refresh the other time-sensitive sources as a real HA update cycle would.
        hass.states.async_set(humidity_id, "50.0", {"unit_of_measurement": "%"})
        hass.states.async_set(outdoor_id, "8.0", {"unit_of_measurement": "°C"})

    freezer.tick(delta=timedelta(seconds=3))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await runtime.controller.async_wait_idle()
    assert runtime.values["input_status"] in {"ready", "running_mean_unavailable"}
    assert runtime.values["resume_required"] is False

    if not runtime.control_enabled:
        await runtime.async_set_control_enabled(True)
        await runtime.controller.async_wait_idle()
        await hass.async_block_till_done()

    if options["fallback_mode"] == "no_write":
        assert calls == []
        assert runtime.values["control_eligible"] is False
    else:
        assert calls
        effective = runtime.values["effective_targets"][target_uuid]["temperature"]
        assert float(effective) == pytest.approx(float(options["fallback_heating_c"]))
        assert float(calls[-1].data["temperature"]) == pytest.approx(float(effective))
        assert target_attributes["temperature"] == calls[-1].data["temperature"]
        assert all(set(call.data) == {"entity_id", "temperature"} for call in calls)

    await runtime.async_unload()

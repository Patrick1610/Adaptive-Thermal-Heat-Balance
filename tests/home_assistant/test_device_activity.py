"""Device-aware freshness and activity-source contract tests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb.config_flow import AthbConfigFlow, AthbOptionsFlow
from custom_components.athb.const import (
    CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY,
    CONF_RH_DEVICE_ACTIVITY_ENTITY,
    DOMAIN,
)
from custom_components.athb.device_activity import compatible_activity_entities
from custom_components.athb.runtime import ZoneRuntime


def _schema_keys(schema: Any) -> set[str]:
    return {str(marker.schema) for marker in schema.schema}


def _selector_entities(schema: Any, key: str) -> set[str]:
    marker = next(marker for marker in schema.schema if str(marker.schema) == key)
    return set(schema.schema[marker].config["include_entities"])


def _register_entity(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    device_id: str,
    domain: str,
    platform: str,
    unique_id: str,
    state: str,
    *,
    unit: str | None = None,
) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain,
        platform,
        unique_id,
        config_entry=cast(Any, config_entry),
        device_id=device_id,
        suggested_object_id=unique_id,
    )
    attributes = {"unit_of_measurement": unit} if unit is not None else {}
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


def _zha_device(hass: HomeAssistant, config_entry: MockConfigEntry, unique: str) -> str:
    return (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={("zha", unique)},
            name=unique,
        )
        .id
    )


def _runtime(
    hass: HomeAssistant,
    *,
    primary: str,
    rh: str,
    primary_activity: str | None = None,
    rh_activity: str | None = None,
) -> ZoneRuntime:
    data: dict[str, Any] = {
        "zone_uuid": "device-aware-zone",
        "primary_temperature": primary,
        "rh_mode": "measured",
        "rh_entity": rh,
        "targets": (),
    }
    if primary_activity is not None:
        data[CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY] = primary_activity
    if rh_activity is not None:
        data[CONF_RH_DEVICE_ACTIVITY_ENTITY] = rh_activity
    entry = SimpleNamespace(
        title="Room",
        entry_id="device-aware-entry",
        data=data,
        options={
            "primary_temperature_freshness_minutes": 30.0,
            "relative_humidity_freshness_minutes": 30.0,
        },
    )
    return ZoneRuntime(hass, cast(Any, entry), "device-aware-zone", "balanced", "off", False)


def _environment(
    hass: HomeAssistant, *, same_device: bool
) -> tuple[MockConfigEntry, str, str, str, str]:
    zha = MockConfigEntry(domain="zha", entry_id=f"zha-{'same' if same_device else 'split'}")
    zha.add_to_hass(hass)
    temperature_device = _zha_device(hass, zha, f"temperature-{'same' if same_device else 'split'}")
    humidity_device = (
        temperature_device if same_device else _zha_device(hass, zha, "humidity-split")
    )
    suffix = "same" if same_device else "split"
    temperature = _register_entity(
        hass,
        zha,
        temperature_device,
        "sensor",
        "zha",
        f"room_temperature_{suffix}",
        "20.0",
        unit="°C",
    )
    humidity = _register_entity(
        hass,
        zha,
        humidity_device,
        "sensor",
        "zha",
        f"room_humidity_{suffix}",
        "55.0",
        unit="%",
    )
    lqi = _register_entity(
        hass, zha, temperature_device, "sensor", "zha", f"room_lqi_{suffix}", "120"
    )
    humidity_rssi = _register_entity(
        hass,
        zha,
        humidity_device,
        "sensor",
        "zha",
        f"room_rssi_{suffix}",
        "-62",
        unit="dBm",
    )
    return zha, temperature, humidity, lqi, humidity_rssi


def test_activity_allowlist_keeps_direct_device_entities_and_excludes_helpers(
    hass: HomeAssistant,
) -> None:
    zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=True)
    device_id = er.async_get(hass).async_get(temperature).device_id
    assert device_id is not None
    mold = MockConfigEntry(domain="mold_indicator", entry_id="mold-helper")
    mold.add_to_hass(hass)
    template = MockConfigEntry(domain="template", entry_id="template-helper")
    template.add_to_hass(hass)
    mold_entity = _register_entity(
        hass, mold, device_id, "sensor", "mold_indicator", "mold_indicator", "72"
    )
    template_entity = _register_entity(
        hass, template, device_id, "sensor", "template", "derived_signal", "1"
    )
    athb = MockConfigEntry(domain=DOMAIN, entry_id="athb-helper")
    athb.add_to_hass(hass)
    athb_entity = _register_entity(
        hass, athb, device_id, "binary_sensor", DOMAIN, "athb_status", "on"
    )

    candidates = compatible_activity_entities(
        hass,
        (temperature, humidity),
        excluded_entity_ids={temperature, humidity},
    )

    assert lqi in candidates
    assert mold_entity not in candidates
    assert template_entity not in candidates
    assert athb_entity not in candidates
    assert temperature not in candidates
    assert humidity not in candidates
    assert er.async_get(hass).async_get(lqi).config_entry_id == zha.entry_id
    hass.states.async_set(lqi, "unavailable")
    assert lqi not in compatible_activity_entities(
        hass, (temperature, humidity), excluded_entity_ids={temperature, humidity}
    )


def test_activity_wizard_uses_one_or_two_exact_device_fields(hass: HomeAssistant) -> None:
    _zha, temperature, humidity, lqi, rssi = _environment(hass, same_device=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Room",
        data={
            "zone_uuid": "wizard-zone",
            "primary_temperature": temperature,
            "rh_mode": "measured",
            "rh_entity": humidity,
            "targets": (),
        },
        options={},
    )
    flow = AthbOptionsFlow(entry)
    flow.hass = hass

    shared = flow._device_activity_schema(flow._pending_data)
    assert _schema_keys(shared) == {"device_activity_entity"}
    assert _selector_entities(shared, "device_activity_entity") == {lqi, rssi}
    flow._apply_device_activity(flow._pending_data, {"device_activity_entity": lqi})
    assert flow._pending_data[CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY] == lqi
    assert flow._pending_data[CONF_RH_DEVICE_ACTIVITY_ENTITY] == lqi

    _zha_split, split_temperature, split_humidity, split_lqi, split_rssi = _environment(
        hass, same_device=False
    )
    split_data = {
        "primary_temperature": split_temperature,
        "rh_mode": "measured",
        "rh_entity": split_humidity,
        "targets": (),
    }
    separate = flow._device_activity_schema(split_data)
    assert _schema_keys(separate) == {
        CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY,
        CONF_RH_DEVICE_ACTIVITY_ENTITY,
    }
    assert _selector_entities(separate, CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY) == {split_lqi}
    assert _selector_entities(separate, CONF_RH_DEVICE_ACTIVITY_ENTITY) == {split_rssi}
    assert compatible_activity_entities(hass, (split_temperature, split_humidity)) == ()
    flow._apply_device_activity(flow._pending_data, {})
    assert CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY not in flow._pending_data
    assert CONF_RH_DEVICE_ACTIVITY_ENTITY not in flow._pending_data


async def test_setup_activity_step_persists_shared_selection(hass: HomeAssistant) -> None:
    _zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=True)
    flow = AthbConfigFlow()
    flow.hass = hass
    flow._data = {
        "name": "Room",
        "zone_uuid": "setup-activity",
        "primary_temperature": temperature,
        "rh_mode": "measured",
        "rh_entity": humidity,
        "outdoor_source": "sensor.outdoor",
    }

    form = await flow.async_step_device_activity()
    assert form["step_id"] == "device_activity"
    assert _schema_keys(form["data_schema"]) == {"device_activity_entity"}
    next_step = await flow.async_step_device_activity({"device_activity_entity": lqi})

    assert next_step["step_id"] == "targets"
    assert flow._data[CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY] == lqi
    assert flow._data[CONF_RH_DEVICE_ACTIVITY_ENTITY] == lqi


async def test_activity_step_skips_cleanly_without_registered_device(hass: HomeAssistant) -> None:
    flow = AthbConfigFlow()
    flow.hass = hass
    flow._data = {
        "name": "Room",
        "zone_uuid": "no-device",
        "primary_temperature": "sensor.unregistered",
        "rh_mode": "declared",
        "rh_declared": 50.0,
        "outdoor_source": "sensor.outdoor",
        CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY: "sensor.obsolete",
    }

    result = await flow.async_step_device_activity()

    assert result["step_id"] == "targets"
    assert CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY not in flow._data


async def test_options_activity_step_displays_saves_and_skips_when_no_candidates(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Room",
        data={
            "zone_uuid": "options-activity",
            "primary_temperature": temperature,
            "rh_mode": "measured",
            "rh_entity": humidity,
            "targets": (),
        },
        options={},
    )
    flow = AthbOptionsFlow(entry)
    flow.hass = hass
    save = MagicMock(return_value={"type": "create_entry"})
    cast(Any, flow)._save_data_settings = save

    form = await flow.async_step_source_device_activity()
    assert form["step_id"] == "source_device_activity"
    result = await flow.async_step_source_device_activity({"device_activity_entity": lqi})
    assert result == {"type": "create_entry"}
    assert flow._pending_data[CONF_PRIMARY_DEVICE_ACTIVITY_ENTITY] == lqi
    assert flow._pending_data[CONF_RH_DEVICE_ACTIVITY_ENTITY] == lqi
    save.assert_called_once_with()

    flow._pending_data = {
        "primary_temperature": "sensor.unregistered",
        "rh_mode": "declared",
        "rh_declared": 50.0,
        "targets": (),
    }
    skipped = await flow.async_step_source_device_activity()
    assert skipped == {"type": "create_entry"}
    assert save.call_count == 2


def test_shared_device_uses_latest_measurement_report_for_both_freshness_windows(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, _lqi, _rssi = _environment(hass, same_device=True)
    now = dt_util.utcnow()
    old = now - timedelta(hours=2)
    recent = now - timedelta(minutes=5)
    hass.states.async_set(
        temperature,
        "20.0",
        {"unit_of_measurement": "°C"},
        timestamp=old.timestamp(),
    )
    hass.states.async_set(
        humidity,
        "55.0",
        {"unit_of_measurement": "%"},
        timestamp=recent.timestamp(),
    )
    runtime = _runtime(hass, primary=temperature, rh=humidity)
    runtime.primary_feedback_at = old

    runtime._refresh_freshness_evidence(now)

    assert runtime.freshness_reported_at == {"primary": recent, "rh": recent}
    assert runtime.freshness_details["primary"]["freshness_basis"] == "shared_device"
    assert runtime.freshness_details["rh"]["freshness_basis"] == "own"
    assert runtime._snapshot_primary_temperature(hass.states.get(temperature)).observed_at == recent
    assert runtime.primary_feedback_at == old


def test_split_devices_keep_independent_freshness_and_activity_does_not_reset_heat_guard(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=False)
    now = dt_util.utcnow()
    old = now - timedelta(hours=2)
    recent = now - timedelta(minutes=2)
    hass.states.async_set(
        temperature,
        "20.0",
        {"unit_of_measurement": "°C"},
        timestamp=old.timestamp(),
    )
    hass.states.async_set(
        humidity,
        "55.0",
        {"unit_of_measurement": "%"},
        timestamp=recent.timestamp(),
    )
    hass.states.async_set(lqi, "121", timestamp=recent.timestamp())
    runtime = _runtime(
        hass,
        primary=temperature,
        rh=humidity,
        primary_activity=lqi,
    )
    runtime.primary_feedback_at = old

    runtime._refresh_freshness_evidence(now)

    assert runtime.freshness_reported_at["primary"] == recent
    assert runtime.freshness_reported_at["rh"] == recent
    assert runtime.freshness_details["primary"]["freshness_basis"] == "configured_activity"
    assert runtime.primary_feedback_at == old


def test_state_reported_timestamp_recovers_freshness_without_material_recalculation(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=True)
    now = dt_util.utcnow()
    old = now - timedelta(hours=2)
    hass.states.async_set(
        temperature,
        "20.0",
        {"unit_of_measurement": "°C"},
        timestamp=old.timestamp(),
    )
    hass.states.async_set(
        humidity,
        "55.0",
        {"unit_of_measurement": "%"},
        timestamp=old.timestamp(),
    )
    hass.states.async_set(lqi, "120", timestamp=old.timestamp())
    runtime = _runtime(
        hass,
        primary=temperature,
        rh=humidity,
        primary_activity=lqi,
        rh_activity=lqi,
    )
    runtime.primary_feedback_at = old
    runtime._refresh_freshness_evidence(now)
    generation = runtime.input_generation
    event = SimpleNamespace(
        data={
            "entity_id": lqi,
            "new_state": hass.states.get(lqi),
            "last_reported": now,
        }
    )

    runtime._handle_source_report_event(cast(Any, event))

    assert runtime.freshness_reported_at["primary"] == now
    assert runtime.freshness_reported_at["rh"] == now
    assert runtime.primary_feedback_at == old
    assert runtime.input_generation == generation + 1
    assert runtime.debounce_cancel is not None
    runtime.debounce_cancel()
    for cancel in runtime.timers.values():
        cancel()


def test_fresh_activity_report_only_moves_timers_and_unavailable_activity_is_ignored(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, lqi, _rssi = _environment(hass, same_device=True)
    now = dt_util.utcnow()
    recent = now - timedelta(minutes=2)
    for entity_id, state, unit in (
        (temperature, "20.0", "°C"),
        (humidity, "55.0", "%"),
        (lqi, "120", None),
    ):
        attributes = {"unit_of_measurement": unit} if unit is not None else {}
        hass.states.async_set(entity_id, state, attributes, timestamp=recent.timestamp())
    runtime = _runtime(
        hass,
        primary=temperature,
        rh=humidity,
        primary_activity=lqi,
        rh_activity=lqi,
    )
    runtime.primary_feedback_at = recent
    runtime._refresh_freshness_evidence(now)
    generation = runtime.input_generation

    runtime._handle_source_report_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "entity_id": lqi,
                    "new_state": hass.states.get(lqi),
                    "last_reported": now,
                }
            ),
        )
    )

    assert runtime.input_generation == generation
    assert runtime.debounce_cancel is None
    assert runtime.freshness_reported_at["primary"] == now
    assert "freshness:primary" in runtime.timers
    hass.states.async_set(lqi, "unavailable")
    runtime._refresh_freshness_evidence(now + timedelta(minutes=1))
    assert runtime.freshness_details["primary"]["freshness_basis"] == "own"
    assert runtime.primary_feedback_at == recent
    for cancel in runtime.timers.values():
        cancel()


def test_switching_to_declared_humidity_removes_obsolete_freshness_metadata(
    hass: HomeAssistant,
) -> None:
    _zha, temperature, humidity, _lqi, _rssi = _environment(hass, same_device=True)
    runtime = _runtime(hass, primary=temperature, rh=humidity)
    now = dt_util.utcnow()
    runtime.primary_feedback_at = now
    runtime.freshness_reported_at = {"primary": now, "rh": now}
    runtime.freshness_details = {"primary": {}, "rh": {}}
    runtime.entry.data["rh_mode"] = "declared"
    runtime.entry.data.pop("rh_entity")

    runtime._refresh_freshness_evidence(now)

    assert set(runtime.freshness_details) == {"primary"}
    assert set(runtime.freshness_reported_at) == {"primary"}

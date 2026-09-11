"""Runtime action, lease, callback, and cleanup tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.athb.runtime import ZoneRuntime


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

"""Adaptive Thermal Heat Balance native Home Assistant integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .adapters.storage import HomeAssistantControlStorageBackend
from .const import CONF_BOOST_MODE, CONF_COMFORT_STRATEGY, DOMAIN, PLATFORMS
from .repairs import RepairManager
from .runtime import AthbConfigEntry, runtime_from_entry


async def async_setup_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> bool:
    runtime = runtime_from_entry(hass, entry)
    entry.runtime_data = runtime
    await runtime.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> bool:
    await entry.runtime_data.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> bool:
    """Migrate legacy Profile choices to comfort level, occupancy setback and Boost."""

    if entry.version == 2:
        return True
    if entry.version != 1:
        return False
    options = dict(entry.options)
    legacy_profile = str(options.pop("profile", "comfort"))
    if legacy_profile == "eco":
        options[CONF_COMFORT_STRATEGY] = "eco"
    options[CONF_BOOST_MODE] = "adaptive" if legacy_profile == "boost" else "off"
    hass.config_entries.async_update_entry(entry, options=options, version=2)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> None:
    """Remove this zone's recovery journal and persistent Repair issues."""

    await HomeAssistantControlStorageBackend(hass, str(entry.data["zone_uuid"])).async_remove()
    RepairManager(hass, entry.entry_id).clear_all()


__all__ = [
    "DOMAIN",
    "async_migrate_entry",
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
]

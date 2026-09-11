"""Adaptive Thermal Heat Balance native Home Assistant integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .adapters.storage import HomeAssistantControlStorageBackend
from .const import DOMAIN, PLATFORMS
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
    del hass
    return entry.version == 1


async def async_remove_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> None:
    """Remove only this zone's control-critical recovery journal."""

    await HomeAssistantControlStorageBackend(hass, str(entry.data["zone_uuid"])).async_remove()


__all__ = [
    "DOMAIN",
    "async_migrate_entry",
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
]

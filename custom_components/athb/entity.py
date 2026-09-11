"""Shared native ATHB entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import ZoneRuntime


class AthbEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, runtime: ZoneRuntime, key: str) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.zone_uuid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.zone_uuid)},
            name=runtime.entry.title,
            manufacturer="ATHB",
            model="Adaptive Thermal Heat Balance",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.subscribe(self.async_write_ha_state))

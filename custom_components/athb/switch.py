"""Adaptive control intent switch."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class AdaptiveControlSwitch(AthbEntity, SwitchEntity):
    _attr_translation_key = "adaptive_control"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "adaptive_control")

    @property
    def is_on(self) -> bool:
        return self.runtime.control_enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        del kwargs
        await self.runtime.async_set_control_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        del kwargs
        await self.runtime.async_set_control_enabled(False)


class PreheatSwitch(AthbEntity, SwitchEntity):
    """Bounded temporary occupancy override."""

    _attr_translation_key = "preheat"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "preheat")

    @property
    def is_on(self) -> bool:
        return self.runtime.preheat_active

    @property
    def icon(self) -> str:
        return "mdi:radiator" if self.is_on else "mdi:radiator-off"

    async def async_turn_on(self, **kwargs: object) -> None:
        del kwargs
        await self.runtime.async_set_preheat_active(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        del kwargs
        await self.runtime.async_set_preheat_active(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    async_add_entities(
        [AdaptiveControlSwitch(entry.runtime_data), PreheatSwitch(entry.runtime_data)]
    )

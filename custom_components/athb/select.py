"""Lightweight strategy and profile selects."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class StrategySelect(AthbEntity, SelectEntity):
    _attr_name = "Comfort strategy"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "comfort_strategy")
        self._attr_options = ["efficient", "balanced", "comfort"]

    @property
    def current_option(self) -> str:
        return self.runtime.strategy

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set_strategy(option)


class ProfileSelect(AthbEntity, SelectEntity):
    _attr_name = "Profile"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "profile")
        self._attr_options = ["auto", "comfort", "eco", "boost"]

    @property
    def current_option(self) -> str:
        return self.runtime.profile

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set_profile(option)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    runtime = entry.runtime_data
    async_add_entities([StrategySelect(runtime), ProfileSelect(runtime)])

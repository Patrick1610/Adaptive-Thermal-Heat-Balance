"""Lightweight comfort-level, Boost, and occupancy-setback selects."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class StrategySelect(AthbEntity, SelectEntity):
    _attr_translation_key = "comfort_strategy"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "comfort_strategy")
        self._attr_options = ["eco", "efficient", "balanced", "comfort", "near_neutral"]

    @property
    def current_option(self) -> str:
        return self.runtime.strategy

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set_strategy(option)


class BoostModeSelect(AthbEntity, SelectEntity):
    _attr_translation_key = "boost_mode"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "boost_mode")
        self._attr_options = ["off", "adaptive", "rapid"]

    @property
    def current_option(self) -> str:
        return self.runtime.boost_mode

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set_boost_mode(option)


class EcoIntensitySelect(AthbEntity, SelectEntity):
    _attr_translation_key = "eco_intensity"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "eco_intensity")
        self._attr_options = ["deep", "workday", "mild", "custom"]

    @property
    def current_option(self) -> str:
        return self.runtime.eco_intensity

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set_eco_intensity(option)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    for obsolete_key in ("profile",):
        entity_id = registry.async_get_entity_id(
            "select", DOMAIN, f"{runtime.zone_uuid}_{obsolete_key}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)
    entities: list[SelectEntity] = [StrategySelect(runtime), BoostModeSelect(runtime)]
    if entry.options.get("occupancy_entity"):
        entities.append(EcoIntensitySelect(runtime))
    else:
        entity_id = registry.async_get_entity_id(
            "select", DOMAIN, f"{runtime.zone_uuid}_eco_intensity"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)
    async_add_entities(entities)

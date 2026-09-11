"""ATHB control eligibility binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class ControlEligibleBinarySensor(AthbEntity, BinarySensorEntity):
    _attr_name = "Control eligible"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "control_eligible")

    @property
    def is_on(self) -> bool:
        return bool(self.runtime.values.get("control_eligible", False))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    async_add_entities([ControlEligibleBinarySensor(entry.runtime_data)])

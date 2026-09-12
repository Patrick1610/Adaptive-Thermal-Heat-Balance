"""ATHB control eligibility binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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


class SurfaceSaturationBinarySensor(AthbEntity, BinarySensorEntity):
    _attr_name = "Predicted surface saturation"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "surface_saturation")

    @property
    def is_on(self) -> bool | None:
        value = self.runtime.values.get("surface_saturation")
        return value if isinstance(value, bool) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[BinarySensorEntity] = [ControlEligibleBinarySensor(entry.runtime_data)]
    surface_enabled = entry.options.get("radiant_model", "uniform") in {
        "surface",
        "mold_indicator",
    }
    if surface_enabled:
        entities.append(SurfaceSaturationBinarySensor(entry.runtime_data))
    else:
        registry = er.async_get(hass)
        obsolete_unique_id = f"{entry.runtime_data.zone_uuid}_surface_saturation"
        for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                registry_entry.domain == "binary_sensor"
                and registry_entry.unique_id == obsolete_unique_id
            ):
                registry.async_remove(registry_entry.entity_id)
    async_add_entities(entities)

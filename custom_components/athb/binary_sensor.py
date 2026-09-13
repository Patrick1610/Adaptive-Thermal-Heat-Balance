"""ATHB control eligibility binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class ControlEligibleBinarySensor(AthbEntity, BinarySensorEntity):
    _attr_translation_key = "control_eligible"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "control_eligible")

    @property
    def is_on(self) -> bool:
        return bool(self.runtime.values.get("control_eligible", False))


class SurfaceSaturationBinarySensor(AthbEntity, BinarySensorEntity, RestoreEntity):
    _attr_translation_key = "surface_saturation"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "surface_saturation")
        self._restored_is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        value = self.runtime.values.get("surface_saturation")
        return value if isinstance(value, bool) else self._restored_is_on

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data_quality = self.runtime.values.get("data_quality")
        if self._restored_is_on is not None and data_quality in {None, "unavailable"}:
            data_quality = "restored_stale"
        return {
            **super().extra_state_attributes,
            "data_quality": data_quality,
            "last_valid_at": self.runtime.values.get("last_valid_at"),
            "data_age_minutes": self.runtime.values.get("data_age_minutes"),
            "stale_safety_active": self.runtime.values.get("stale_safety_active", False),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not isinstance(self.runtime.values.get("surface_saturation"), bool):
            restored = await self.async_get_last_state()
            if restored is not None and restored.state in {"on", "off"}:
                self._restored_is_on = restored.state == "on"


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

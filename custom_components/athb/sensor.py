"""ATHB numerical, policy, and per-target sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


@dataclass(frozen=True, slots=True)
class Description:
    key: str
    temperature: bool = False
    humidity: bool = False
    enabled_default: bool = True
    entity_category: EntityCategory | None = None


DESCRIPTIONS = (
    Description("thermal_sensation"),
    Description(
        "heating_control_target",
        temperature=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "thermal_neutral",
        temperature=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "cooling_control_target",
        temperature=True,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description("comfort_status"),
    Description("input_status", entity_category=EntityCategory.DIAGNOSTIC),
    Description("control_status", entity_category=EntityCategory.DIAGNOSTIC),
    Description("outdoor_running_mean", temperature=True),
    Description("surface_temperature", temperature=True),
    Description(
        "surface_relative_humidity",
        humidity=True,
    ),
)

TARGET_ENDPOINTS = ("temperature", "target_low", "target_high")


class AthbSensor(AthbEntity, RestoreSensor):
    def __init__(self, runtime: ZoneRuntime, description: Description) -> None:
        super().__init__(runtime, description.key)
        self.description = description
        self._restored_native_value: Any = None
        self._attr_translation_key = description.key
        self._attr_entity_registry_enabled_default = description.enabled_default
        self._attr_entity_category = description.entity_category
        if description.temperature:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif description.humidity:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = "%"
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> Any:
        value = self.runtime.values.get(self.description.key)
        return value if value is not None else self._restored_native_value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.runtime.values.get(self.description.key) is None:
            restored = await self.async_get_last_sensor_data()
            if restored is not None:
                self._restored_native_value = restored.native_value

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data_quality = self.runtime.values.get("data_quality")
        if self._restored_native_value is not None and data_quality in {None, "unavailable"}:
            data_quality = "restored_stale"
        return {
            "comfort_level": self.runtime.strategy,
            "boost_mode": self.runtime.boost_mode,
            "occupancy_status": self.runtime.values.get("occupancy_status"),
            "setback_active": self.runtime.values.get("setback_active", False),
            "quality_reasons": self.runtime.values.get("quality_reasons", ()),
            "suppression_reason": self.runtime.values.get("suppression_reason"),
            "ownership": self.runtime.values.get("ownership", {}),
            "data_readiness": self.runtime.values.get("data_readiness", {}),
            "target_readiness": self.runtime.values.get("target_readiness", {}),
            "data_quality": data_quality,
            "last_valid_at": self.runtime.values.get("last_valid_at"),
            "data_age_minutes": self.runtime.values.get("data_age_minutes"),
            "stale_safety_active": self.runtime.values.get("stale_safety_active", False),
        }


class TargetSensor(AthbEntity, RestoreSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        runtime: ZoneRuntime,
        target: dict[str, str],
        endpoint: str,
        target_name: str | None = None,
    ) -> None:
        super().__init__(runtime, f"{target['target_uuid']}_{endpoint}")
        self.target = target
        self.endpoint = endpoint
        self._restored_native_value: Any = None
        self._attr_translation_key = {
            "temperature": "effective_target",
            "target_low": "effective_heating_target",
            "target_high": "effective_cooling_target",
        }[endpoint]
        self._attr_translation_placeholders = {
            "target": target_name or _fallback_target_name(target["entity_id"])
        }

    @property
    def native_value(self) -> Any:
        targets = self.runtime.values.get("effective_targets", {})
        value = targets.get(self.target["target_uuid"], {}).get(self.endpoint)
        return value if value is not None else self._restored_native_value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        targets = self.runtime.values.get("effective_targets", {})
        if targets.get(self.target["target_uuid"], {}).get(self.endpoint) is None:
            restored = await self.async_get_last_sensor_data()
            if restored is not None:
                self._restored_native_value = restored.native_value

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        targets = self.runtime.values.get("effective_target_details", {})
        detail = targets.get(self.target["target_uuid"], {})
        data_quality = self.runtime.values.get("data_quality")
        if self._restored_native_value is not None and data_quality in {None, "unavailable"}:
            data_quality = "restored_stale"
        return {
            "mode": detail.get("mode", "unavailable"),
            "reason": detail.get("reason"),
            "fallback": detail.get("fallback", False),
            "comfort_level": self.runtime.strategy,
            "boost_mode": self.runtime.boost_mode,
            "boost_phase": detail.get("boost_phase"),
            "occupancy_status": self.runtime.values.get("occupancy_status"),
            "setback_active": self.runtime.values.get("setback_active", False),
            "setback": self.runtime.eco_intensity,
            "stale": detail.get("stale", False),
            "safety_deescalation": detail.get("safety_deescalation", False),
            "data_quality": data_quality,
            "last_valid_at": self.runtime.values.get("last_valid_at"),
            "data_age_minutes": self.runtime.values.get("data_age_minutes"),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    surface_enabled = entry.options.get("radiant_model", "uniform") in {
        "surface",
        "mold_indicator",
    }
    descriptions = [
        item for item in DESCRIPTIONS if surface_enabled or not item.key.startswith("surface_")
    ]
    entities: list[SensorEntity] = [AthbSensor(runtime, item) for item in descriptions]
    desired_unique_ids = {entity.unique_id for entity in entities}
    for target in entry.data.get("targets", ()):
        for endpoint in _target_endpoints(hass, target["entity_id"]):
            entity = TargetSensor(
                runtime,
                target,
                endpoint,
                _target_display_name(hass, target["entity_id"]),
            )
            entities.append(entity)
            desired_unique_ids.add(entity.unique_id)
    _remove_stale_sensor_entities(hass, entry, desired_unique_ids)
    async_add_entities(entities)


def _target_display_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return a human name without exposing a raw entity id in the UI."""

    state = hass.states.get(entity_id)
    if state is not None:
        friendly_name = state.attributes.get("friendly_name")
        if isinstance(friendly_name, str) and friendly_name.strip():
            return friendly_name.strip()
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is not None:
        for candidate in (registry_entry.name, registry_entry.original_name):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return _fallback_target_name(entity_id)


def _fallback_target_name(entity_id: str) -> str:
    object_id = entity_id.partition(".")[2] or entity_id
    return object_id.replace("_", " ").strip().title()


def _target_endpoints(hass: HomeAssistant, entity_id: str) -> tuple[str, ...]:
    state = hass.states.get(entity_id)
    if state is None:
        return ("temperature",)
    try:
        features = ClimateEntityFeature(int(state.attributes.get("supported_features", 0)))
    except TypeError, ValueError:
        return ("temperature",)
    endpoints: list[str] = []
    if features & ClimateEntityFeature.TARGET_TEMPERATURE:
        endpoints.append("temperature")
    if features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE:
        endpoints.extend(("target_low", "target_high"))
    return tuple(endpoints) or ("temperature",)


def _remove_stale_sensor_entities(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    desired_unique_ids: set[str | None],
) -> None:
    registry = er.async_get(hass)
    zone_prefix = f"{entry.runtime_data.zone_uuid}_"
    managed_suffixes = tuple(f"_{endpoint}" for endpoint in TARGET_ENDPOINTS)
    core_ids = {f"{entry.runtime_data.zone_uuid}_{item.key}" for item in DESCRIPTIONS}
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registry_entry.unique_id
        managed = unique_id in core_ids or (
            unique_id.startswith(zone_prefix) and unique_id.endswith(managed_suffixes)
        )
        if registry_entry.domain == "sensor" and managed and unique_id not in desired_unique_ids:
            registry.async_remove(registry_entry.entity_id)

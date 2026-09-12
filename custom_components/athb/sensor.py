"""ATHB numerical, policy, and per-target sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


@dataclass(frozen=True, slots=True)
class Description:
    key: str
    name: str
    temperature: bool = False
    humidity: bool = False
    enabled_default: bool = True


DESCRIPTIONS = (
    Description("thermal_sensation", "Thermal sensation"),
    Description("heating_control_target", "Heating control target", True),
    Description("thermal_neutral", "Thermal neutral", True),
    Description("cooling_control_target", "Cooling control target", True),
    Description("comfort_status", "Comfort status"),
    Description("input_status", "Input status"),
    Description("control_status", "Control status"),
    Description("outdoor_running_mean", "Outdoor running mean", True),
    Description("surface_temperature", "Surface temperature", True),
    Description(
        "surface_relative_humidity",
        "Surface relative humidity",
        humidity=True,
    ),
)

TARGET_ENDPOINTS = ("temperature", "target_low", "target_high")


class AthbSensor(AthbEntity, SensorEntity):
    def __init__(self, runtime: ZoneRuntime, description: Description) -> None:
        super().__init__(runtime, description.key)
        self.description = description
        self._attr_name = description.name
        self._attr_entity_registry_enabled_default = description.enabled_default
        if description.temperature:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif description.humidity:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = "%"
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> Any:
        return self.runtime.values.get(self.description.key)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
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
        }


class TargetSensor(AthbEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, runtime: ZoneRuntime, target: dict[str, str], endpoint: str) -> None:
        super().__init__(runtime, f"{target['target_uuid']}_{endpoint}")
        self.target = target
        self.endpoint = endpoint
        self._attr_name = f"{target['entity_id']} effective {endpoint.replace('_', ' ')}"

    @property
    def native_value(self) -> Any:
        targets = self.runtime.values.get("effective_targets", {})
        return targets.get(self.target["target_uuid"], {}).get(self.endpoint)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        targets = self.runtime.values.get("effective_target_details", {})
        detail = targets.get(self.target["target_uuid"], {})
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
            entity = TargetSensor(runtime, target, endpoint)
            entities.append(entity)
            desired_unique_ids.add(entity.unique_id)
    _remove_stale_sensor_entities(hass, entry, desired_unique_ids)
    async_add_entities(entities)


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

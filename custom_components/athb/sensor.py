"""ATHB numerical, policy, and per-target sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


@dataclass(frozen=True, slots=True)
class Description:
    key: str
    name: str
    temperature: bool = False
    humidity: bool = False


DESCRIPTIONS = (
    Description("thermal_sensation", "Thermal sensation"),
    Description("heating_control_target", "Heating control target", True),
    Description("thermal_neutral", "Thermal neutral", True),
    Description("cooling_control_target", "Cooling control target", True),
    Description("comfort_status", "Comfort status"),
    Description("control_status", "Control status"),
    Description("outdoor_running_mean", "Outdoor running mean", True),
    Description("surface_temperature", "Surface temperature", True),
    Description("surface_relative_humidity", "Surface relative humidity", humidity=True),
)


class AthbSensor(AthbEntity, SensorEntity):
    def __init__(self, runtime: ZoneRuntime, description: Description) -> None:
        super().__init__(runtime, description.key)
        self.description = description
        self._attr_name = description.name
        if description.temperature:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif description.humidity:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> Any:
        return self.runtime.values.get(self.description.key)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "comfort_strategy": self.runtime.strategy,
            "profile": self.runtime.profile,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [AthbSensor(runtime, item) for item in DESCRIPTIONS]
    for target in entry.data.get("targets", ()):
        entities.extend(
            (
                TargetSensor(runtime, target, "temperature"),
                TargetSensor(runtime, target, "target_low"),
                TargetSensor(runtime, target, "target_high"),
            )
        )
    async_add_entities(entities)

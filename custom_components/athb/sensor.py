"""ATHB numerical, policy, and per-target sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate.const import ATTR_CURRENT_TEMPERATURE, ClimateEntityFeature
from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .adapters.climate import capability_from_state
from .core.climate import (
    TARGET_TEMPERATURE,
    TARGET_TEMPERATURE_RANGE,
    AutoMapping,
    infer_auto_mapping,
)
from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


@dataclass(frozen=True, slots=True)
class Description:
    key: str
    temperature: bool = False
    humidity: bool = False
    suggested_display_precision: int | None = None
    enabled_default: bool = True
    entity_category: EntityCategory | None = None


DESCRIPTIONS = (
    Description("thermal_sensation"),
    Description(
        "lower_comfort_boundary",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "comfort_range_current",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "comfort_range_neutral_delta",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "heating_control_target",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "thermal_neutral",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "cooling_control_target",
        temperature=True,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    Description(
        "upper_comfort_boundary",
        temperature=True,
        suggested_display_precision=2,
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
TARGET_SCENARIOS = ("current", "occupied", "unoccupied")
ROOT_SENSOR_CONTEXT = {
    "lower_comfort_boundary": ("lower_comfort", "comfort_range"),
    "heating_control_target": ("heating_control", "control_range"),
    "thermal_neutral": ("thermal_neutral", "comfort_range_reference"),
    "cooling_control_target": ("cooling_control", "control_range"),
    "upper_comfort_boundary": ("upper_comfort", "comfort_range"),
}


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
        if description.suggested_display_precision is not None:
            self._attr_suggested_display_precision = description.suggested_display_precision

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
        attributes = {
            **super().extra_state_attributes,
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
        if context := ROOT_SENSOR_CONTEXT.get(self.description.key):
            root_name, range_role = context
            votes = self.runtime.values.get("root_sensation_votes", {})
            attributes["sensation_vote"] = votes.get(root_name) if isinstance(votes, dict) else None
            attributes["range_role"] = range_role
        elif self.description.key == "comfort_range_current":
            attributes["range_role"] = "current_observation"
            attributes["source_entity"] = self.runtime.entry.data.get("primary_temperature")
        elif self.description.key == "comfort_range_neutral_delta":
            attributes["range_role"] = "neutral_delta"
            attributes["calculation"] = "current_minus_neutral"
            attributes["current_temperature"] = self.runtime.values.get("comfort_range_current")
            attributes["neutral_reference"] = self.runtime.values.get("thermal_neutral")
        return attributes


class ZoneTargetSensor(AthbEntity, RestoreSensor):
    """One room-coordinate target with exact per-climate context on Current."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2

    def __init__(self, runtime: ZoneRuntime, scenario: str) -> None:
        super().__init__(runtime, f"target_{scenario}")
        self.scenario = scenario
        self._restored_native_value: Any = None
        self._attr_translation_key = f"target_{scenario}"

    def _live_value(self) -> float | None:
        values: list[float] = []
        for target in self.runtime.values.get("target_scenarios", {}).values():
            room = target.get(self.scenario, {}).get("room", {})
            value = room.get("temperature")
            if isinstance(value, int | float) and not isinstance(value, bool):
                values.append(float(value))
        if values and max(values) - min(values) <= 1e-6:
            return values[0]
        return None

    @property
    def native_value(self) -> Any:
        live_value = self._live_value()
        return live_value if live_value is not None else self._restored_native_value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.native_value is None:
            restored = await self.async_get_last_sensor_data()
            if restored is not None:
                self._restored_native_value = restored.native_value

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live_value = self._live_value()
        data_quality = self.runtime.values.get("data_quality")
        if live_value is None and self._restored_native_value is not None:
            data_quality = "restored_stale"
        attributes = {
            **super().extra_state_attributes,
            "target_basis": "room_policy_before_actuator_adjustments",
            "scenario": self.scenario,
            "data_quality": data_quality,
            "last_valid_at": self.runtime.values.get("last_valid_at"),
            "suppression_reason": self.runtime.values.get("suppression_reason"),
        }
        if self.scenario == "current":
            attributes["per_climate"] = _per_climate_context(self.runtime)
            attributes["decision"] = _target_decision_context(self.runtime)
        return attributes


def _target_deviation_context(runtime: ZoneRuntime, direction: str) -> dict[str, Any] | None:
    current = runtime.values.get("comfort_range_current")
    if not isinstance(current, int | float) or isinstance(current, bool):
        return None
    references: list[float] = []
    for target in runtime.values.get("target_scenarios", {}).values():
        room = target.get("current", {}).get("room", {})
        target_direction = target.get("direction")
        if target_direction == "ranged":
            endpoint = "target_low" if direction == "heating" else "target_high"
            value = room.get(endpoint)
            if isinstance(value, int | float) and not isinstance(value, bool):
                references.append(float(value))
        elif (
            isinstance(value := room.get("temperature"), int | float)
            and not isinstance(value, bool)
            and target_direction == f"{direction}_only"
        ):
            references.append(float(value))
    if not references:
        return None
    reference = max(references) if direction == "heating" else min(references)
    return {
        "deviation": reference - float(current),
        "current_temperature": float(current),
        "direction": direction,
        "reference_temperature": reference,
        "position": (
            "below_reference"
            if float(current) < reference
            else "above_reference"
            if float(current) > reference
            else "at_reference"
        ),
    }


class TargetDeviationSensor(AthbEntity, RestoreSensor):
    """Signed directional target position in room-temperature coordinates."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2

    def __init__(self, runtime: ZoneRuntime, direction: str) -> None:
        super().__init__(runtime, f"target_{direction}_deviation")
        self.direction = direction
        self._attr_translation_key = f"target_{direction}_deviation"
        self._restored_native_value: Any = None

    @property
    def native_value(self) -> Any:
        context = _target_deviation_context(self.runtime, self.direction)
        return context["deviation"] if context is not None else self._restored_native_value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if _target_deviation_context(self.runtime, self.direction) is None:
            restored = await self.async_get_last_sensor_data()
            if restored is not None:
                self._restored_native_value = restored.native_value

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        context = _target_deviation_context(self.runtime, self.direction)
        data_quality = self.runtime.values.get("data_quality")
        if context is None and self._restored_native_value is not None:
            data_quality = "restored_stale"
        return {
            **super().extra_state_attributes,
            "calculation": "directional_target_minus_current",
            **(context or {}),
            "data_quality": data_quality,
            "last_valid_at": self.runtime.values.get("last_valid_at"),
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
            **super().extra_state_attributes,
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
    targets = list(entry.data.get("targets", ()))
    for direction in _target_control_directions(hass, targets):
        deviation = TargetDeviationSensor(runtime, direction)
        entities.append(deviation)
        desired_unique_ids.add(deviation.unique_id)
    if _supports_zone_target_sensors(hass, targets):
        for scenario in TARGET_SCENARIOS:
            zone_entity = ZoneTargetSensor(runtime, scenario)
            entities.append(zone_entity)
            desired_unique_ids.add(zone_entity.unique_id)
    else:
        for target in targets:
            for endpoint in _target_endpoints(hass, target["entity_id"]):
                target_entity = TargetSensor(
                    runtime,
                    target,
                    endpoint,
                    _target_display_name(hass, target["entity_id"]),
                )
                entities.append(target_entity)
                desired_unique_ids.add(target_entity.unique_id)
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


def _supports_zone_target_sensors(hass: HomeAssistant, targets: list[dict[str, str]]) -> bool:
    """Use the compact three-sensor view only for one scalar control direction."""

    directions: set[str] = set()
    if not targets:
        return False
    for target in targets:
        capability = capability_from_state(hass.states.get(target["entity_id"]))
        scalar = bool(capability.supported_features & TARGET_TEMPERATURE)
        ranged = bool(capability.supported_features & TARGET_TEMPERATURE_RANGE)
        if not scalar or ranged:
            return False
        if capability.hvac_mode in {"heat", "cool"}:
            directions.add("heating" if capability.hvac_mode == "heat" else "cooling")
            continue
        inferred = infer_auto_mapping(capability)
        if inferred in {AutoMapping.HEATING, AutoMapping.COOLING}:
            directions.add(inferred.value)
            continue
        advertised = set(capability.advertised_hvac_modes)
        if "heat" in advertised and "cool" not in advertised:
            directions.add("heating")
        elif "cool" in advertised and "heat" not in advertised:
            directions.add("cooling")
        else:
            return False
    return len(directions) == 1


def _target_control_directions(
    hass: HomeAssistant, targets: list[dict[str, str]]
) -> tuple[str, ...]:
    """Return observable heating/cooling target directions from public capabilities."""

    directions: set[str] = set()
    for target in targets:
        capability = capability_from_state(hass.states.get(target["entity_id"]))
        if capability.supported_features & TARGET_TEMPERATURE_RANGE:
            directions.update(("heating", "cooling"))
            continue
        if not capability.supported_features & TARGET_TEMPERATURE:
            continue
        if capability.hvac_mode in {"heat", "cool"}:
            directions.add("heating" if capability.hvac_mode == "heat" else "cooling")
            continue
        inferred = infer_auto_mapping(capability)
        if inferred in {AutoMapping.HEATING, AutoMapping.COOLING}:
            directions.add(inferred.value)
            continue
        advertised = set(capability.advertised_hvac_modes)
        if "heat" in advertised and "cool" not in advertised:
            directions.add("heating")
        elif "cool" in advertised and "heat" not in advertised:
            directions.add("cooling")
    return tuple(direction for direction in ("heating", "cooling") if direction in directions)


def _per_climate_context(runtime: ZoneRuntime) -> dict[str, dict[str, Any]]:
    """Expose current observations and all three normalized scenario requests."""

    scenarios = runtime.values.get("target_scenarios", {})
    context: dict[str, dict[str, Any]] = {}
    for target in runtime.entry.data.get("targets", ()):
        entity_id = str(target["entity_id"])
        details = scenarios.get(str(target["target_uuid"]), {})
        state = runtime.hass.states.get(entity_id)
        capability = capability_from_state(state)
        current_temperature = (
            None if state is None else state.attributes.get(ATTR_CURRENT_TEMPERATURE)
        )
        reported_target: dict[str, float] = {}
        if capability.scalar_target_ha is not None:
            reported_target["temperature"] = capability.scalar_target_ha
        if capability.target_temp_low_ha is not None:
            reported_target["target_low"] = capability.target_temp_low_ha
        if capability.target_temp_high_ha is not None:
            reported_target["target_high"] = capability.target_temp_high_ha
        context[entity_id] = {
            "hvac_mode": capability.hvac_mode,
            "hvac_action": None if state is None else state.attributes.get("hvac_action"),
            "available": capability.available,
            "unit": capability.temperature_unit.value,
            "current_temperature": current_temperature,
            "reported_target": reported_target,
            "calibration_offset_c": runtime.entry.options.get(
                f"calibration_{target['target_uuid']}", 0.0
            ),
            "minimum_temperature": capability.min_temp_ha,
            "maximum_temperature": capability.max_temp_ha,
            "temperature_step": capability.target_temp_step_ha,
            "athb_current_target": details.get("current", {}).get("actuator", {}),
            "athb_occupied_target": details.get("occupied", {}).get("actuator", {}),
            "athb_unoccupied_target": details.get("unoccupied", {}).get("actuator", {}),
            "command_outcome": runtime.values.get("command_outcomes", {}).get(
                str(target["target_uuid"])
            ),
            "ownership": runtime.values.get("ownership", {}).get(
                str(target.get("registry_identity", ""))
            ),
            "target_readiness": runtime.values.get("target_readiness", {}).get(
                str(target.get("registry_identity", ""))
            ),
        }
    return context


def _target_decision_context(runtime: ZoneRuntime) -> dict[str, Any]:
    """Return a compact structured explanation of the current room target."""

    calculation = runtime.values.get("calculation")
    targets = getattr(calculation, "targets", ())
    numerical = next(
        (item.result for item in targets if getattr(item, "result", None) is not None), None
    )
    policy = getattr(numerical, "policy", None)
    return {
        "scenario": runtime.values.get("occupancy_status"),
        "comfort_level": runtime.strategy,
        "occupancy_source_state": runtime.values.get("occupancy_source_state"),
        "occupancy_held": runtime.values.get("occupancy_held", False),
        "setback": runtime.eco_intensity if runtime.entry.options.get("occupancy_entity") else None,
        "setback_active": runtime.values.get("setback_active", False),
        "boost_mode": runtime.boost_mode,
        "boost_phase": getattr(policy, "boost_phase", None),
        "governing_heating": getattr(policy, "governing_heating", None),
        "governing_cooling": getattr(policy, "governing_cooling", None),
        "pre_slew_heating_c": getattr(policy, "pre_slew_heating_c", None),
        "pre_slew_cooling_c": getattr(policy, "pre_slew_cooling_c", None),
        "requested_heating_c": getattr(policy, "heating_c", None),
        "requested_cooling_c": getattr(policy, "cooling_c", None),
        "explicit_transition": getattr(calculation, "explicit_transition", False),
        "transition_reasons": runtime.values.get("transition_reasons", ()),
        "data_quality": runtime.values.get("data_quality"),
        "quality_reasons": runtime.values.get("quality_reasons", ()),
        "suppression_reason": runtime.values.get("suppression_reason"),
        "recovery_reason": runtime.values.get("recovery_reason"),
        "recovery_reasons": runtime.values.get("recovery_reasons", ()),
        "resume_required": runtime.values.get("resume_required", False),
    }


def _remove_stale_sensor_entities(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    desired_unique_ids: set[str | None],
) -> None:
    registry = er.async_get(hass)
    zone_prefix = f"{entry.runtime_data.zone_uuid}_"
    managed_suffixes = tuple(f"_{endpoint}" for endpoint in TARGET_ENDPOINTS)
    core_ids = {
        *(f"{entry.runtime_data.zone_uuid}_{item.key}" for item in DESCRIPTIONS),
        *(f"{entry.runtime_data.zone_uuid}_target_{scenario}" for scenario in TARGET_SCENARIOS),
        f"{entry.runtime_data.zone_uuid}_target_heating_deviation",
        f"{entry.runtime_data.zone_uuid}_target_cooling_deviation",
    }
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registry_entry.unique_id
        managed = unique_id in core_ids or (
            unique_id.startswith(zone_prefix) and unique_id.endswith(managed_suffixes)
        )
        if registry_entry.domain == "sensor" and managed and unique_id not in desired_unique_ids:
            registry.async_remove(registry_entry.entity_id)

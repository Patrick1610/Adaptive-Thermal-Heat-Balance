"""Shared native ATHB entity base."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import ZoneRuntime


class AthbEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, runtime: ZoneRuntime, key: str) -> None:
        self.runtime = runtime
        self._athb_key = key
        self._attr_unique_id = f"{runtime.zone_uuid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.zone_uuid)},
            name=runtime.entry.title,
            manufacturer="ATHB",
            model="Adaptive Thermal Heat Balance",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.subscribe(self.async_write_ha_state))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose compact, provenance-aware context on every ATHB entity."""

        return self._context_attributes()

    def _context_attributes(self) -> dict[str, Any]:
        values = self.runtime.values
        calculation = values.get("calculation")
        data = self.runtime.entry.data
        options = self.runtime.entry.options
        raw_source_states = values.get("source_states", {})
        source_states = raw_source_states if isinstance(raw_source_states, dict) else {}

        def source(
            entity_id: str | None,
            value: object,
            *,
            provenance: str,
            unit: str,
        ) -> dict[str, Any]:
            observed = source_states.get(entity_id, {}) if entity_id else {}
            return {
                "entity_id": entity_id,
                "value": value,
                "unit": unit,
                "provenance": provenance,
                "last_reported": observed.get("last_reported"),
            }

        primary_id = str(data.get("primary_temperature", "")) or None
        outdoor_id = str(data.get("outdoor_source", "")) or None
        rh_mode = str(data.get("rh_mode", "declared"))
        rh_id = str(data.get("rh_entity", "")) or None if rh_mode == "measured" else None
        primary_value = getattr(calculation, "primary_value_c", None)
        humidity_value = getattr(calculation, "relative_humidity_pct", None)
        outdoor_value = getattr(calculation, "outdoor_value_c", None)
        running_mean = getattr(calculation, "running_mean_c", None)
        history_quality = getattr(calculation, "history_quality", None)
        provenance = values.get("provenance", {})
        settings = _settings_for_entity(self._athb_key, options)
        attributes: dict[str, Any] = {
            "sources": {
                "primary_temperature": source(
                    primary_id,
                    primary_value,
                    provenance=str(provenance.get("primary", "measured")),
                    unit="°C",
                ),
                "relative_humidity": source(
                    rh_id,
                    humidity_value,
                    provenance=str(values.get("rh_provenance", rh_mode)),
                    unit="%",
                ),
                "outdoor_temperature": source(
                    outdoor_id,
                    outdoor_value,
                    provenance=str(provenance.get("outdoor", "measured")),
                    unit="°C",
                ),
            },
            "control_context": {
                "comfort_level": self.runtime.strategy,
                "boost_mode": self.runtime.boost_mode,
                "boost_expiry": (
                    expiry.isoformat()
                    if isinstance((expiry := values.get("boost_expiry")), datetime)
                    else expiry
                ),
                "occupancy_entity": options.get("occupancy_entity"),
                "occupancy_status": values.get("occupancy_status"),
                "setback": self.runtime.eco_intensity if options.get("occupancy_entity") else None,
                "setback_active": values.get("setback_active", False),
            },
            "related_values": {
                "thermal_sensation": values.get("thermal_sensation"),
                "comfort_status": values.get("comfort_status"),
                "heating_target": values.get("heating_control_target"),
                "neutral_reference": values.get("thermal_neutral"),
                "cooling_target": values.get("cooling_control_target"),
                "adaptive_outdoor_temperature": running_mean,
            },
            "data_quality": values.get("data_quality"),
            "last_valid_at": values.get("last_valid_at"),
            "data_age_minutes": values.get("data_age_minutes"),
            "history_quality": history_quality,
            "status_context": {
                "input_status": values.get("input_status"),
                "control_status": values.get("control_status"),
                "control_eligible": values.get("control_eligible", False),
                "suppression_reason": values.get("suppression_reason"),
                "quality_reasons": values.get("quality_reasons", ()),
                "ownership": values.get("ownership", {}),
                "target_readiness": values.get("target_readiness", {}),
                "data_readiness": values.get("data_readiness", {}),
                "command_outcomes": values.get("command_outcomes", {}),
            },
        }
        if settings:
            attributes["settings"] = settings
        return attributes


def _settings_for_entity(key: str, options: Any) -> dict[str, Any]:
    """Return only settings that help explain this entity's state."""

    shared_model = {
        "met": options.get("met", 1.1),
        "air_speed_mode": options.get("air_speed_mode", "fixed"),
        "air_speed_m_s": options.get("air_speed_m_s", 0.1),
        "air_speed_entity": options.get("air_speed_entity"),
        "clothing_mode": options.get("clothing_mode", "automatic"),
        "fixed_clothing_clo": options.get("fixed_clothing_clo"),
        "radiant_model": options.get("radiant_model", "uniform"),
    }
    if key in {
        "thermal_sensation",
        "comfort_status",
        "heating_control_target",
        "thermal_neutral",
        "cooling_control_target",
        "target_current",
        "target_occupied",
        "target_unoccupied",
        "comfort_strategy",
    }:
        return {
            **shared_model,
            "lower_comfort_vote": options.get("lower_comfort_vote", -0.5),
            "upper_comfort_vote": options.get("upper_comfort_vote", 0.5),
        }
    if key in {
        "surface_temperature",
        "surface_relative_humidity",
        "surface_saturation",
    }:
        return {
            "radiant_model": options.get("radiant_model", "uniform"),
            "mold_indicator_entity": options.get("mold_indicator_entity"),
            "surface_rh_threshold_pct": options.get("surface_rh_threshold_pct", 80.0),
        }
    if key == "outdoor_running_mean":
        return {"running_mean_alpha": options.get("running_mean_alpha", 0.8)}
    if key in {"adaptive_control", "control_status", "control_eligible", "resume_control"}:
        return {
            "manual_override_minutes": options.get("manual_override_minutes", 120.0),
            "minimum_meaningful_change": options.get("minimum_meaningful_change", 0.1),
            "feedback_resolution": options.get("feedback_resolution", 0.01),
        }
    if key == "eco_intensity":
        return {
            "eco_heating_setback_c": options.get("eco_heating_setback_c", 2.0),
            "eco_cooling_setback_c": options.get("eco_cooling_setback_c", 2.0),
            "minimum_control_temperature": options.get("minimum_control_temperature", 18.0),
            "maximum_control_temperature": options.get("maximum_control_temperature", 26.0),
        }
    if key == "boost_mode":
        return {
            "boost_delta_c": options.get("boost_delta_c", 1.0),
            "boost_duration_minutes": options.get("boost_duration_minutes", 60.0),
        }
    if key == "input_status":
        return {
            "primary_temperature_freshness_minutes": options.get(
                "primary_temperature_freshness_minutes", 30.0
            ),
            "relative_humidity_freshness_minutes": options.get(
                "relative_humidity_freshness_minutes", 30.0
            ),
            "running_mean_alpha": options.get("running_mean_alpha", 0.8),
            "fallback_mode": options.get("fallback_mode", "fixed"),
        }
    return {}

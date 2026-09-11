"""Validation shared by ATHB config and options flows."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_OUTDOOR_SOURCE,
    CONF_PRIMARY_TEMPERATURE,
    CONF_RH_DECLARED,
    CONF_RH_ENTITY,
    CONF_RH_MODE,
)


def validate_environment(data: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not data.get(CONF_PRIMARY_TEMPERATURE):
        errors[CONF_PRIMARY_TEMPERATURE] = "required"
    if not data.get(CONF_OUTDOOR_SOURCE):
        errors[CONF_OUTDOOR_SOURCE] = "required"
    mode = data.get(CONF_RH_MODE)
    if mode == "measured":
        if not data.get(CONF_RH_ENTITY) or CONF_RH_DECLARED in data:
            errors[CONF_RH_ENTITY] = "invalid_rh_source"
    elif mode == "declared":
        value = data.get(CONF_RH_DECLARED)
        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 100.0
            or CONF_RH_ENTITY in data
        ):
            errors[CONF_RH_DECLARED] = "invalid_rh_source"
    else:
        errors[CONF_RH_MODE] = "invalid_rh_source"
    return errors


def validate_targets(targets: object) -> tuple[str, ...]:
    if not isinstance(targets, list | tuple):
        raise ValueError("targets must be a sequence")
    normalized = tuple(str(target) for target in targets if str(target))
    if not 1 <= len(normalized) <= 8:
        raise ValueError("one through eight targets are required")
    if len(normalized) != len(set(normalized)):
        raise ValueError("targets must be distinct")
    if any(
        target.startswith("sensor.athb_") or target.startswith("climate.athb_")
        for target in normalized
    ):
        raise ValueError("ATHB self references are not permitted")
    return normalized


def _finite_in_range(value: object, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def validate_options(data: Mapping[str, Any]) -> dict[str, str]:
    """Validate complete runtime tuning without exposing numerical-kernel constants."""

    errors: dict[str, str] = {}
    ranges = {
        "met": (0.8, 2.0),
        "air_speed_m_s": (0.0, 2.0),
        "fixed_clothing_clo": (0.1, 2.0),
        "lower_comfort_vote": (-1.0, -0.05),
        "upper_comfort_vote": (0.05, 1.0),
        "eco_heating_setback_c": (0.0, 5.0),
        "eco_cooling_setback_c": (0.0, 5.0),
        "inactive_heating_temperature": (5.0, 35.0),
        "inactive_cooling_temperature": (5.0, 35.0),
        "boost_delta_c": (0.0, 3.0),
        "boost_duration_minutes": (5.0, 180.0),
        "manual_override_minutes": (15.0, 1440.0),
        "running_mean_alpha": (0.6, 0.9),
        "minimum_range_gap": (1.0, 10.0),
        "minimum_meaningful_change": (0.0, 5.0),
        "feedback_resolution": (0.0, 5.0),
        "surface_view_factor": (0.0, 1.0),
        "surface_f_rsi": (0.000001, 1.0),
        "surface_rh_threshold_pct": (0.000001, 100.0),
        "globe_diameter_m": (0.01, 1.0),
        "globe_emissivity": (0.01, 1.0),
    }
    for name, (minimum, maximum) in ranges.items():
        if name in data and not _finite_in_range(data[name], minimum, maximum):
            errors[name] = "invalid_option"
    for name, value in data.items():
        if name.startswith("calibration_") and not _finite_in_range(value, -3.0, 3.0):
            errors[name] = "invalid_option"
    if data.get("eco_intensity", "custom") not in {"mild", "workday", "deep", "custom"}:
        errors["eco_intensity"] = "invalid_option"
    air_speed_mode = data.get("air_speed_mode", "fixed")
    if air_speed_mode == "measured":
        if not data.get("air_speed_entity"):
            errors["air_speed_entity"] = "required"
    elif air_speed_mode != "fixed":
        errors["air_speed_mode"] = "invalid_option"
    clothing_mode = data.get("clothing_mode", "automatic")
    if clothing_mode not in {"automatic", "fixed"}:
        errors["clothing_mode"] = "invalid_option"
    elif clothing_mode == "fixed" and not _finite_in_range(
        data.get("fixed_clothing_clo", 0.7), 0.1, 2.0
    ):
        errors["fixed_clothing_clo"] = "invalid_option"
    if (
        data.get("radiant_model") == "surface"
        and data.get("surface_modelled", False)
        and "surface_f_rsi" not in data
    ):
        errors["surface_f_rsi"] = "required"
    minimum = data.get("minimum_control_temperature", 18.0)
    maximum = data.get("maximum_control_temperature", 26.0)
    if (
        not _finite_in_range(minimum, 5.0, 35.0)
        or not _finite_in_range(maximum, 5.0, 35.0)
        or float(minimum) >= float(maximum)
    ):
        errors["control_bounds"] = "invalid_control_bounds"
    inactive_heating = data.get("inactive_heating_temperature", 18.0)
    inactive_cooling = data.get("inactive_cooling_temperature", 26.0)
    if (
        "inactive_heating_temperature" not in errors
        and "inactive_cooling_temperature" not in errors
        and float(inactive_heating) >= float(inactive_cooling)
    ):
        errors["inactive_cooling_temperature"] = "invalid_option"
    if data.get("fallback_mode", "fixed") not in {"fixed", "no_write"}:
        errors["fallback_mode"] = "invalid_option"
    if data.get("fallback_mode", "fixed") == "fixed" and "control_bounds" not in errors:
        heating = data.get("fallback_heating_c", minimum)
        cooling = data.get("fallback_cooling_c", maximum)
        if not _finite_in_range(heating, float(minimum), float(maximum)):
            errors["fallback_heating_c"] = "invalid_option"
        if not _finite_in_range(cooling, float(minimum), float(maximum)):
            errors["fallback_cooling_c"] = "invalid_option"
        if (
            "fallback_heating_c" not in errors
            and "fallback_cooling_c" not in errors
            and float(heating) >= float(cooling)
        ):
            errors["fallback_cooling_c"] = "invalid_option"
    if data.get("auto_mapping", "unmapped") not in {
        "unmapped",
        "heating",
        "cooling",
        "range",
        "bidirectional_scalar",
    }:
        errors["auto_mapping"] = "invalid_option"
    critical = data.get("critical_locations", ())
    if not isinstance(critical, list | tuple) or len(critical) > 8:
        errors["critical_locations"] = "invalid_option"
    else:
        identities: list[str] = []
        for item in critical:
            if (
                not isinstance(item, dict)
                or not item.get("location_id")
                or not item.get("entity_id")
                or item.get("mode") not in {"monitoring", "heating", "cooling", "both"}
            ):
                errors["critical_locations"] = "invalid_option"
                break
            identities.append(str(item["location_id"]))
        if len(identities) != len(set(identities)):
            errors["critical_locations"] = "invalid_option"
    return errors

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

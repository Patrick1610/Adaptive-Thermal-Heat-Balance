"""Constants for Adaptive Thermal Heat Balance."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "athb"
PLATFORMS = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
)
CONF_ZONE_UUID = "zone_uuid"
CONF_PRIMARY_TEMPERATURE = "primary_temperature"
CONF_RH_MODE = "rh_mode"
CONF_RH_ENTITY = "rh_entity"
CONF_RH_DECLARED = "rh_declared"
CONF_OUTDOOR_SOURCE = "outdoor_source"
CONF_TARGETS = "targets"
CONF_COMFORT_STRATEGY = "comfort_strategy"
CONF_BOOST_MODE = "boost_mode"
CONF_ECO_INTENSITY = "eco_intensity"
CONF_MOLD_INDICATOR_ENTITY = "mold_indicator_entity"
MOLD_INDICATOR_CRITICAL_TEMP_ATTRIBUTE = "estimated_critical_temp"
CONF_CONTROL_ENABLED = "control_enabled"
DEFAULT_STRATEGY = "balanced"
DEFAULT_BOOST_MODE = "off"
DEFAULT_ECO_INTENSITY = "mild"

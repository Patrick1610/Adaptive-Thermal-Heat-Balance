"""Home Assistant climate.set_temperature service boundary used only by CommandBroker."""

from __future__ import annotations

from homeassistant.components.climate.const import (
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import Context, HomeAssistant, State

from ..core.climate import ClimateCapabilitySnapshot, TemperatureUnit
from .broker import ContextToken


def capability_from_state(state: State | None) -> ClimateCapabilitySnapshot:
    """Build a frozen capability snapshot using only public HA state attributes."""

    if state is None:
        return ClimateCapabilitySnapshot(
            None, (), 0, None, None, None, TemperatureUnit.CELSIUS, available=False
        )
    attributes = state.attributes
    unit = (
        TemperatureUnit.FAHRENHEIT
        if str(attributes.get("unit_of_measurement")) in {"°F", "F"}
        else TemperatureUnit.CELSIUS
    )

    def numeric(name: str) -> float | None:
        value = attributes.get(name)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except TypeError, ValueError, OverflowError:
            return None

    modes = attributes.get(ATTR_HVAC_MODES, ())
    return ClimateCapabilitySnapshot(
        state.state,
        tuple(str(mode) for mode in modes) if isinstance(modes, list | tuple) else (),
        int(attributes.get(ATTR_SUPPORTED_FEATURES, 0)),
        numeric(ATTR_MIN_TEMP),
        numeric(ATTR_MAX_TEMP),
        numeric(ATTR_TARGET_TEMP_STEP),
        unit,
        numeric(ATTR_TEMPERATURE),
        numeric(ATTR_TARGET_TEMP_LOW),
        numeric(ATTR_TARGET_TEMP_HIGH),
        str(attributes[ATTR_PRESET_MODE]) if attributes.get(ATTR_PRESET_MODE) is not None else None,
        state.state != STATE_UNAVAILABLE,
        bool(attributes.get("restored", False)),
        any(
            attributes.get(name) is not None
            for name in (ATTR_TEMPERATURE, ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH)
        ),
    )


class HomeAssistantClimateService:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_set_temperature(
        self, payload: dict[str, object], context: ContextToken
    ) -> None:
        if not isinstance(context.native_context, Context):
            raise TypeError("native Home Assistant Context required")
        await self._hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            payload,
            blocking=True,
            context=context.native_context,
        )

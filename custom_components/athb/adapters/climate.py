"""Home Assistant climate.set_temperature service boundary used only by CommandBroker."""

from __future__ import annotations

from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.climate.const import SERVICE_SET_TEMPERATURE
from homeassistant.core import Context, HomeAssistant

from .broker import ContextToken


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

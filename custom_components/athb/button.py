"""Explicit resume action."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AthbEntity
from .runtime import AthbConfigEntry, ZoneRuntime


class ResumeButton(AthbEntity, ButtonEntity):
    _attr_translation_key = "resume_control"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(runtime, "resume_control")

    async def async_press(self) -> None:
        await self.runtime.async_resume()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    async_add_entities([ResumeButton(entry.runtime_data)])

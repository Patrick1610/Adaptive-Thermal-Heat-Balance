"""Typed per-zone Home Assistant runtime state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .adapters.broker import TargetLeaseRegistry
from .const import (
    CONF_COMFORT_STRATEGY,
    CONF_CONTROL_ENABLED,
    CONF_PROFILE,
    DEFAULT_PROFILE,
    DEFAULT_STRATEGY,
)
from .controller import ZoneController


@dataclass(slots=True)
class ZoneRuntime:
    hass: HomeAssistant
    entry: ConfigEntry[ZoneRuntime]
    zone_uuid: str
    strategy: str
    profile: str
    control_enabled: bool
    configuration_generation: int = 1
    runtime_generation: int = 1
    values: dict[str, Any] = field(default_factory=dict)
    listeners: list[Callable[[], None]] = field(default_factory=list)
    update_callbacks: set[Callable[[], None]] = field(default_factory=set)
    controller: ZoneController[dict[str, Any], dict[str, Any]] | None = None
    debounce_cancel: Callable[[], None] | None = None
    debounce_started: float | None = None

    async def async_start(self) -> None:
        semaphore = get_global_semaphore(self.hass)
        self.controller = ZoneController(
            executor=self.hass.async_add_executor_job,
            calculate=lambda snapshot: snapshot,
            publish=lambda _generation, result: self.publish(result),
            global_semaphore=semaphore,
        )
        entity_ids = {
            str(self.entry.data.get("primary_temperature", "")),
            str(self.entry.data.get("outdoor_source", "")),
        }
        if self.entry.data.get("rh_mode") == "measured":
            entity_ids.add(str(self.entry.data.get("rh_entity", "")))
        entity_ids.update(str(target["entity_id"]) for target in self.entry.data.get("targets", ()))
        entity_ids.discard("")

        @callback
        def state_event(_event: Any) -> None:
            self.schedule_environmental_snapshot()

        if entity_ids:
            self.listeners.append(
                async_track_state_change_event(self.hass, entity_ids, state_event)
            )
        self.async_request_snapshot()

    @callback
    def schedule_environmental_snapshot(self) -> None:
        now = self.hass.loop.time()
        if self.debounce_started is None:
            self.debounce_started = now
        if self.debounce_cancel is not None:
            self.debounce_cancel()
        delay = max(0.0, min(2.0, self.debounce_started + 10.0 - now))

        @callback
        def fire(_now: Any) -> None:
            self.debounce_cancel = None
            self.debounce_started = None
            self.async_request_snapshot()

        self.debounce_cancel = async_call_later(self.hass, delay, fire)

    @callback
    def async_request_snapshot(self) -> None:
        if self.controller is None:
            return
        source_ids = (
            str(self.entry.data.get("primary_temperature", "")),
            str(self.entry.data.get("outdoor_source", "")),
            str(self.entry.data.get("rh_entity", "")),
        )
        source_states = {
            entity_id: (
                None
                if (state := self.hass.states.get(entity_id)) is None
                else {
                    "state": state.state,
                    "unit": state.attributes.get("unit_of_measurement"),
                    "last_reported": state.last_reported.isoformat(),
                }
            )
            for entity_id in source_ids
            if entity_id
        }
        snapshot = {
            **self.values,
            "source_states": source_states,
            "strategy": self.strategy,
            "profile": self.profile,
            "configuration_generation": self.configuration_generation,
        }
        self.controller.request(snapshot)

    @callback
    def subscribe(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self.update_callbacks.add(update_callback)

        @callback
        def remove() -> None:
            self.update_callbacks.discard(update_callback)

        return remove

    @callback
    def publish(self, values: dict[str, Any]) -> None:
        self.values = values
        for update_callback in tuple(self.update_callbacks):
            update_callback()

    async def async_set_strategy(self, strategy: str) -> None:
        if strategy == self.strategy:
            return
        options = {**self.entry.options, CONF_COMFORT_STRATEGY: strategy}
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.strategy = strategy
        self.configuration_generation += 1
        if self.controller is not None:
            self.controller.invalidate()
            if self.debounce_cancel is not None:
                self.debounce_cancel()
                self.debounce_cancel = None
                self.debounce_started = None
            self.async_request_snapshot()
        self.publish({**self.values, "strategy": strategy, "reason": "strategy_changed"})

    async def async_set_profile(self, profile: str) -> None:
        self.profile = profile
        self.publish({**self.values, "profile": profile})

    async def async_set_control_enabled(self, enabled: bool) -> None:
        leases = get_lease_registry(self.hass)
        targets = self.entry.data.get("targets", ())
        if enabled:
            acquired: list[str] = []
            for target in targets:
                identity = str(target["registry_identity"])
                if not leases.acquire(identity, self.entry.entry_id):
                    for acquired_identity in acquired:
                        leases.release(acquired_identity, self.entry.entry_id)
                    raise ValueError("target_already_controlled")
                acquired.append(identity)
        else:
            for target in targets:
                leases.release(str(target["registry_identity"]), self.entry.entry_id)
        self.control_enabled = enabled
        options = {**self.entry.options, CONF_CONTROL_ENABLED: enabled}
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.publish({**self.values, "control_enabled": enabled})

    async def async_resume(self) -> None:
        self.publish({**self.values, "resume_requested": True})

    async def async_unload(self) -> None:
        self.runtime_generation += 1
        if self.debounce_cancel is not None:
            self.debounce_cancel()
            self.debounce_cancel = None
        if self.controller is not None:
            await self.controller.async_stop()
        for unsubscribe in self.listeners:
            unsubscribe()
        self.listeners.clear()
        self.update_callbacks.clear()
        leases = get_lease_registry(self.hass)
        for target in self.entry.data.get("targets", ()):
            leases.release(str(target["registry_identity"]), self.entry.entry_id)


type AthbConfigEntry = ConfigEntry[ZoneRuntime]


def get_global_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    domain_data = hass.data.setdefault("athb", {})
    semaphore = domain_data.get("calculation_semaphore")
    if semaphore is None:
        semaphore = asyncio.Semaphore(2)
        domain_data["calculation_semaphore"] = semaphore
    return cast(asyncio.Semaphore, semaphore)


def get_lease_registry(hass: HomeAssistant) -> TargetLeaseRegistry:
    domain_data = hass.data.setdefault("athb", {})
    leases = domain_data.get("target_leases")
    if leases is None:
        leases = TargetLeaseRegistry()
        domain_data["target_leases"] = leases
    return cast(TargetLeaseRegistry, leases)


def runtime_from_entry(hass: HomeAssistant, entry: AthbConfigEntry) -> ZoneRuntime:
    return ZoneRuntime(
        hass,
        entry,
        str(entry.data["zone_uuid"]),
        str(entry.options.get(CONF_COMFORT_STRATEGY, DEFAULT_STRATEGY)),
        str(entry.options.get(CONF_PROFILE, DEFAULT_PROFILE)),
        bool(entry.options.get(CONF_CONTROL_ENABLED, False)),
    )

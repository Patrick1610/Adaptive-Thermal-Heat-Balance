"""Persist presentation identities independently of transient climate state."""

from __future__ import annotations

from typing import Any, TypeGuard

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .runtime import AthbConfigEntry

LAYOUT_KEY = "target_sensor_layout"
ENDPOINTS = ("temperature", "target_low", "target_high")
DIRECTIONS = ("heating", "cooling")


def resolve_target_sensor_layout(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    candidate: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reuse a saved layout, adopting existing identities on the first upgrade.

    Only a changed configured target UUID set permits a new capability-derived
    layout. A new installation with missing capabilities waits for a state event
    instead of saving a guessed scalar layout.
    """
    targets = sorted(str(target["target_uuid"]) for target in entry.data.get("targets", ()))
    saved = entry.data.get(LAYOUT_KEY)
    if _valid_layout(saved, targets):
        return dict(saved)

    retargeted = (
        isinstance(saved, dict)
        and isinstance(saved.get("targets"), list)
        and saved["targets"] != targets
    )
    layout = None if retargeted else _registered_layout(hass, entry, targets, candidate)
    if layout is None:
        layout = candidate
    if layout is None:
        return None
    layout = {"version": 1, "targets": targets, **layout}
    hass.config_entries.async_update_entry(entry, data={**entry.data, LAYOUT_KEY: layout})
    return layout


def _valid_layout(value: Any, targets: list[str]) -> TypeGuard[dict[str, Any]]:
    if not isinstance(value, dict):
        return False
    if value.get("version") != 1 or value.get("targets") != targets:
        return False
    if type(value.get("compact")) is not bool:
        return False
    directions = value.get("directions")
    endpoints = value.get("endpoints")
    return (
        isinstance(directions, list)
        and all(item in DIRECTIONS for item in directions)
        and len(set(directions)) == len(directions)
        and (not value["compact"] or len(directions) == 1)
        and isinstance(endpoints, dict)
        and set(endpoints) == set(targets)
        and all(
            isinstance(items, list) and bool(items) and all(item in ENDPOINTS for item in items)
            for items in endpoints.values()
        )
    )


def _registered_layout(
    hass: HomeAssistant,
    entry: AthbConfigEntry,
    targets: list[str],
    candidate: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep registered target/deviation unique IDs on the first upgrade."""
    prefix = f"{entry.runtime_data.zone_uuid}_"
    ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == "sensor"
    }
    directions = [item for item in DIRECTIONS if f"{prefix}target_{item}_deviation" in ids]
    if not directions and candidate is not None:
        directions = list(candidate["directions"])
    compact = any(
        f"{prefix}target_{scenario}" in ids for scenario in ("current", "occupied", "unoccupied")
    )
    endpoints = {
        target: [item for item in ENDPOINTS if f"{prefix}{target}_{item}" in ids]
        for target in targets
    }
    if compact and len(directions) != 1:
        if candidate is None or not candidate["compact"]:
            return None
        directions = list(candidate["directions"])
    if compact:
        return {
            "compact": True,
            "directions": directions,
            "endpoints": {target: ["temperature"] for target in targets},
        }
    if targets and all(endpoints.values()):
        return {"compact": False, "directions": directions, "endpoints": endpoints}
    return None

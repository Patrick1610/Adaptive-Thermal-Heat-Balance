"""Device-aware activity-source selection and validation."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_ACTIVITY_DOMAINS = frozenset({"sensor", "binary_sensor"})
_DERIVED_PLATFORMS = frozenset(
    {
        "derivative",
        "filter",
        "group",
        "integration",
        "min_max",
        "mold_indicator",
        "statistics",
        "template",
    }
)


def registry_entry(hass: HomeAssistant, entity_id: str) -> er.RegistryEntry | None:
    """Return the current registry entry for an entity."""

    return er.async_get(hass).async_get(entity_id)


def source_device_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve a source's physical Home Assistant device identity."""

    entry = registry_entry(hass, entity_id)
    return entry.device_id if entry is not None else None


def sources_share_device(hass: HomeAssistant, first: str, second: str) -> bool:
    """Return whether two registered sources share one non-empty device identity."""

    first_device = source_device_id(hass, first)
    return first_device is not None and first_device == source_device_id(hass, second)


def compatible_activity_entities(
    hass: HomeAssistant,
    source_entity_ids: Iterable[str],
    *,
    excluded_entity_ids: Iterable[str] = (),
    require_available: bool = True,
) -> tuple[str, ...]:
    """Return loaded, direct entities that can prove activity for every source device."""

    sources = tuple(
        entry
        for entity_id in source_entity_ids
        if (entry := registry_entry(hass, entity_id)) is not None
    )
    if not sources or any(source.device_id is None for source in sources):
        return ()
    device_id = sources[0].device_id
    if any(source.device_id != device_id for source in sources):
        return ()
    excluded = set(excluded_entity_ids) | {source.entity_id for source in sources}
    registry = er.async_get(hass)
    candidates: list[str] = []
    for candidate in registry.entities.values():
        if (
            candidate.entity_id in excluded
            or candidate.domain not in _ACTIVITY_DOMAINS
            or candidate.device_id != device_id
            or candidate.platform == DOMAIN
            or candidate.platform in _DERIVED_PLATFORMS
            or any(
                candidate.platform != source.platform
                or candidate.config_entry_id != source.config_entry_id
                for source in sources
            )
        ):
            continue
        state = hass.states.get(candidate.entity_id)
        if require_available and (state is None or state.state in {"unknown", "unavailable"}):
            continue
        candidates.append(candidate.entity_id)
    return tuple(sorted(candidates))


def activity_entity_is_compatible(
    hass: HomeAssistant,
    activity_entity_id: str,
    source_entity_ids: Iterable[str],
    *,
    excluded_entity_ids: Iterable[str] = (),
) -> bool:
    """Validate a configured activity entity against current registry topology."""

    return activity_entity_id in compatible_activity_entities(
        hass,
        source_entity_ids,
        excluded_entity_ids=excluded_entity_ids,
        require_available=False,
    )

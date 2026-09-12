"""Privacy-preserving config-entry diagnostics for ATHB."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.core import HomeAssistant

from .runtime import AthbConfigEntry

_DROP_KEYS = {
    "user_id",
    "context_id",
    "parent_context_id",
    "latitude",
    "longitude",
    "external_url",
    "internal_url",
    "occupancy_history",
    "raw_attributes",
}
_IDENTITY_KEYS = {
    "entity_id",
    "source_identity",
    "registry_identity",
    "target_identity",
    "location_id",
    "area_id",
    "device_id",
    "primary_temperature",
    "outdoor_source",
    "rh_entity",
    "mrt_entity",
    "globe_temperature_entity",
    "surface_temperature_entity",
    "mold_indicator_entity",
}


def _pseudonym(value: str, salt: str) -> str:
    return f"athb-{sha256(f'{salt}|{value}'.encode()).hexdigest()[:12]}"


def _privacy_filter(value: Any, *, salt: str, key: str | None = None) -> Any:
    if key in _DROP_KEYS:
        return None
    if key in _IDENTITY_KEYS and isinstance(value, str):
        return _pseudonym(value, salt)
    if isinstance(value, dict):
        return {
            (
                _pseudonym(item_key, salt)
                if "." in item_key or item_key.startswith("registry-")
                else item_key
            ): filtered
            for item_key, item_value in value.items()
            if item_key not in _DROP_KEYS
            and (filtered := _privacy_filter(item_value, salt=salt, key=item_key)) is not None
        }
    if isinstance(value, list | tuple):
        return [_privacy_filter(item, salt=salt) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AthbConfigEntry
) -> dict[str, Any]:
    """Return bounded current evidence without exposing household identifiers."""

    del hass
    runtime = entry.runtime_data
    trace_payloads = [
        {"decision_id": item.decision_id, "generation": item.generation, **item.payload}
        for item in runtime.trace_ring.items
    ]
    raw = {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "current": {
            key: value
            for key, value in runtime.values.items()
            if key not in {"source_states", "calculation"}
        },
        "decision_traces": trace_payloads,
        "ownership": {key: asdict(value) for key, value in runtime.ownership.items()},
    }
    serializable = json.loads(json.dumps(raw, default=str, allow_nan=False))
    # HA's standard helper removes known sensitive keys before ATHB applies
    # consistent pseudonyms to identifiers that remain useful for correlation.
    redacted = async_redact_data(serializable, _DROP_KEYS)
    filtered = _privacy_filter(redacted, salt=runtime.zone_uuid)
    assert isinstance(filtered, dict)
    return filtered

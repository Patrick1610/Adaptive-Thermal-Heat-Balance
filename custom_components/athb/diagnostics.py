"""Privacy-preserving config-entry diagnostics for ATHB."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MOLD_INDICATOR_ENTITY,
    CONF_OUTDOOR_SOURCE,
    CONF_PRIMARY_TEMPERATURE,
    CONF_RH_ENTITY,
)
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
    "primary_device_activity_entity",
    "rh_device_activity_entity",
    "activity_entity",
}


def _pseudonym(value: str, salt: str) -> str:
    return f"athb-{sha256(f'{salt}|{value}'.encode()).hexdigest()[:12]}"


def _pseudonym_entity(value: str, salt: str) -> str:
    """Keep the non-private HA domain useful while hiding the object id."""

    domain, separator, _object_id = value.partition(".")
    return f"{domain}.{_pseudonym(value, salt)}" if separator else _pseudonym(value, salt)


def _privacy_filter(value: Any, *, salt: str, key: str | None = None) -> Any:
    if key in _DROP_KEYS:
        return None
    if key in _IDENTITY_KEYS and isinstance(value, str):
        return _pseudonym_entity(value, salt)
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

    runtime = entry.runtime_data
    trace_payloads = [
        {"decision_id": item.decision_id, "generation": item.generation, **item.payload}
        for item in runtime.trace_ring.items
    ]
    now = dt_util.utcnow()
    source_specs = (
        (
            "primary",
            entry.data.get(CONF_PRIMARY_TEMPERATURE),
            "primary_temperature_freshness_minutes",
        ),
        ("humidity", entry.data.get(CONF_RH_ENTITY), "relative_humidity_freshness_minutes"),
        ("outdoor", entry.data.get(CONF_OUTDOOR_SOURCE), None),
        (
            "mold_indicator",
            entry.options.get(CONF_MOLD_INDICATOR_ENTITY),
            "radiant_freshness_minutes",
        ),
    )
    sources: list[dict[str, Any]] = []
    for kind, raw_entity_id, freshness_key in source_specs:
        entity_id = str(raw_entity_id or "")
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        reported = (
            (getattr(state, "last_reported", None) or state.last_updated)
            if state is not None
            else None
        )
        source_key = "rh" if kind == "humidity" else kind
        freshness_detail = runtime.freshness_details.get(source_key, {})
        measurement_reported = freshness_detail.get("measurement_reported_at", reported)
        effective_reported = freshness_detail.get("freshness_reported_at", reported)
        age_minutes = (
            max(0.0, (now - measurement_reported).total_seconds() / 60.0)
            if measurement_reported is not None
            else None
        )
        effective_age_minutes = (
            max(0.0, (now - effective_reported).total_seconds() / 60.0)
            if effective_reported is not None
            else None
        )
        accepted = runtime.source_states.get(source_key)
        sources.append(
            {
                "kind": kind,
                "entity_id": entity_id,
                "available": state is not None and state.state not in {"unknown", "unavailable"},
                "reported_age_minutes": age_minutes,
                "effective_freshness_age_minutes": effective_age_minutes,
                "freshness_basis": freshness_detail.get("freshness_basis", "own"),
                "device_id": freshness_detail.get("device_id"),
                "activity_entity": freshness_detail.get("activity_entity"),
                "activity_entity_valid": freshness_detail.get("activity_entity_valid", True),
                "activity_warning": (
                    None
                    if freshness_detail.get("activity_entity_valid", True)
                    else "configured_activity_entity_no_longer_matches_source_device"
                ),
                "configured_freshness_minutes": (
                    entry.options.get(freshness_key) if freshness_key is not None else 120.0
                ),
                "validated": accepted is not None and accepted.last_accepted is not None,
                "recovering": bool(accepted.recovering) if accepted is not None else False,
            }
        )
    raw = {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "current": {
            key: value
            for key, value in runtime.values.items()
            if key not in {"source_states", "calculation"}
        },
        "decision_traces": trace_payloads,
        "ownership": {key: asdict(value) for key, value in runtime.ownership.items()},
        "sources": sources,
        "active_repairs": sorted(
            runtime.repair_manager.active if runtime.repair_manager is not None else ()
        ),
    }
    serializable = json.loads(json.dumps(raw, default=str, allow_nan=False))
    # HA's standard helper removes known sensitive keys before ATHB applies
    # consistent pseudonyms to identifiers that remain useful for correlation.
    redacted = async_redact_data(serializable, _DROP_KEYS)
    filtered = _privacy_filter(redacted, salt=runtime.zone_uuid)
    assert isinstance(filtered, dict)
    return filtered

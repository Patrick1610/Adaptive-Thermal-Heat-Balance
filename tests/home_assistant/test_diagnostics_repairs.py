"""Diagnostics privacy, repair, and transition logging tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.athb.diagnostics import async_get_config_entry_diagnostics
from custom_components.athb.repairs import RepairManager, TransitionLogger
from custom_components.athb.runtime import ZoneRuntime


async def test_diagnostics_pseudonymize_identifiers_and_drop_private_data(
    hass: HomeAssistant,
) -> None:
    entry = SimpleNamespace(
        title="Private room",
        entry_id="entry-private",
        data={
            "zone_uuid": "zone-private",
            "primary_temperature": "sensor.bedroom_private",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.private_name",
                    "registry_identity": "registry-secret",
                }
            ],
            "user_id": "user-secret",
        },
        options={},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-private", "balanced", "comfort", False)
    runtime.values = {
        "thermal_sensation": -0.1,
        "source_states": {"sensor.bedroom_private": {"state": "20"}},
        "external_url": "https://private.example",
    }
    runtime.trace_ring.add(
        generation=1,
        payload={
            "source_identity": "sensor.bedroom_private",
            "location_id": "cold-corner-private",
            "context_id": "context-secret",
            "occupancy_history": ["home", "away"],
            "requested_room_target": 19.5,
        },
    )
    entry.runtime_data = runtime

    diagnostics = await async_get_config_entry_diagnostics(hass, cast(Any, entry))
    serialized = str(diagnostics)

    assert "bedroom_private" not in serialized
    assert "private_name" not in serialized
    assert "registry-secret" not in serialized
    assert "cold-corner-private" not in serialized
    assert "user-secret" not in serialized
    assert "context-secret" not in serialized
    assert "private.example" not in serialized
    assert "requested_room_target" in serialized
    assert "athb-" in serialized
    assert diagnostics["sources"][0]["kind"] == "primary"
    assert diagnostics["sources"][0]["entity_id"].startswith("sensor.athb-")
    assert diagnostics["active_repairs"] == []


def test_repair_manager_creates_once_and_clears_issue(hass: HomeAssistant) -> None:
    manager = RepairManager(hass, "entry-1")
    registry = ir.async_get(hass)

    assert manager.update("incompatible_auto_mapping", True)
    assert not manager.update("incompatible_auto_mapping", True)
    assert registry.async_get_issue("athb", "entry-1_incompatible_auto_mapping") is not None
    assert manager.update("incompatible_auto_mapping", False)
    assert registry.async_get_issue("athb", "entry-1_incompatible_auto_mapping") is None


def test_transition_logger_emits_only_on_failure_and_recovery(caplog) -> None:
    logger = TransitionLogger(logging.getLogger("athb-test-transition"))
    with caplog.at_level(logging.INFO):
        assert not logger.update("source", False, active_message="failed", recovery_message="ok")
        assert logger.update("source", True, active_message="failed", recovery_message="ok")
        assert not logger.update("source", True, active_message="failed", recovery_message="ok")
        assert logger.update("source", False, active_message="failed", recovery_message="ok")

    assert [record.message for record in caplog.records] == ["failed", "ok"]

"""Diagnostics privacy, repair, and transition logging tests."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

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
    reported = dt_util.utcnow() - timedelta(minutes=20)
    effective = dt_util.utcnow() - timedelta(minutes=2)
    runtime.freshness_details["primary"] = {
        "measurement_reported_at": reported,
        "freshness_reported_at": effective,
        "freshness_basis": "configured_activity",
        "device_id": "private-device-id",
        "activity_entity": "sensor.private_lqi",
        "activity_entity_valid": True,
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
    assert diagnostics["sources"][0]["reported_age_minutes"] >= 20.0
    assert diagnostics["sources"][0]["effective_freshness_age_minutes"] >= 2.0
    assert diagnostics["sources"][0]["freshness_basis"] == "configured_activity"
    assert diagnostics["sources"][0]["activity_entity"].startswith("sensor.athb-")
    assert diagnostics["sources"][0].get("activity_warning") is None
    assert diagnostics["active_repairs"] == []


def test_repair_manager_creates_once_and_clears_issue(hass: HomeAssistant) -> None:
    manager = RepairManager(hass, "entry-1")
    registry = ir.async_get(hass)

    assert manager.update("incompatible_auto_mapping", True)
    assert not manager.update("incompatible_auto_mapping", True)
    assert registry.async_get_issue("athb", "entry-1_incompatible_auto_mapping") is not None
    assert manager.update("incompatible_auto_mapping", False)
    assert registry.async_get_issue("athb", "entry-1_incompatible_auto_mapping") is None


def test_reloaded_repair_manager_adopts_and_reconciles_persistent_issue(
    hass: HomeAssistant,
) -> None:
    first_runtime = RepairManager(hass, "entry-reload")
    assert first_runtime.update("mandatory_input_unavailable_1h", True)

    reloaded_runtime = RepairManager(hass, "entry-reload")
    assert reloaded_runtime.active == {"mandatory_input_unavailable_1h"}
    assert not reloaded_runtime.update("mandatory_input_unavailable_1h", True)
    assert reloaded_runtime.update("mandatory_input_unavailable_1h", False)
    assert (
        ir.async_get(hass).async_get_issue("athb", "entry-reload_mandatory_input_unavailable_1h")
        is None
    )


def test_reloaded_runtime_clears_recovered_resume_issue(hass: HomeAssistant) -> None:
    assert RepairManager(hass, "entry-resume").update("resume_required", True)
    entry = SimpleNamespace(
        title="Recovered room",
        entry_id="entry-resume",
        data={"zone_uuid": "zone-resume", "targets": ()},
        options={},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-resume", "balanced", "off", False)
    runtime.repair_manager = RepairManager(hass, entry.entry_id)

    runtime._update_delayed_repair("resume_required", False, timedelta(hours=1))

    assert ir.async_get(hass).async_get_issue("athb", "entry-resume_resume_required") is None


def test_transition_logger_emits_only_on_failure_and_recovery(caplog) -> None:
    logger = TransitionLogger(logging.getLogger("athb-test-transition"))
    with caplog.at_level(logging.INFO):
        assert not logger.update("source", False, active_message="failed", recovery_message="ok")
        assert logger.update("source", True, active_message="failed", recovery_message="ok")
        assert not logger.update("source", True, active_message="failed", recovery_message="ok")
        assert logger.update("source", False, active_message="failed", recovery_message="ok")

    assert [record.message for record in caplog.records] == ["failed", "ok"]

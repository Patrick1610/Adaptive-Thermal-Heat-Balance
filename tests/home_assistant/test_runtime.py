"""Runtime action, lease, callback, and cleanup tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.athb import runtime as runtime_module
from custom_components.athb.adapters.broker import CommandBroker, CommandOutcome
from custom_components.athb.adapters.climate import (
    HomeAssistantClimateService,
    capability_from_state,
)
from custom_components.athb.adapters.storage import (
    ControlStoreState,
    HomeAssistantControlStorageBackend,
    StoredActuator,
    serialize_control_state,
)
from custom_components.athb.calculation import CapturedTarget, calculate_runtime_snapshot
from custom_components.athb.core.climate import (
    ClimateCapabilitySnapshot,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    TemperatureUnit,
)
from custom_components.athb.core.contracts import (
    AcknowledgementStatus,
    DispatchStatus,
    Observation,
    ObservationValidity,
    Provenance,
)
from custom_components.athb.core.ownership import (
    DataReadiness,
    Ownership,
    OwnershipEvent,
    OwnershipState,
    TargetReadiness,
)
from custom_components.athb.core.sources import SourceState
from custom_components.athb.runtime import ZoneRuntime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios


def _runtime(*, entry_id: str = "entry-1", target_identity: str = "registry-1") -> ZoneRuntime:
    entry = SimpleNamespace(
        title="Zone",
        entry_id=entry_id,
        data={
            "zone_uuid": "zone-1",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": target_identity,
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_update_entry=MagicMock()))
    return ZoneRuntime(cast(Any, hass), cast(Any, entry), "zone-1", "balanced", "off", False)


def test_runtime_callbacks_boost_resume_and_lightweight_strategy() -> None:
    runtime = _runtime()
    updates: list[dict[str, Any]] = []
    remove = runtime.subscribe(lambda: updates.append(dict(runtime.values)))
    runtime.publish({"thermal_sensation": 0.1})
    assert updates[-1]["thermal_sensation"] == 0.1
    asyncio.run(runtime.async_set_boost_mode("adaptive"))
    assert runtime.boost_mode == "adaptive"
    asyncio.run(runtime.async_resume())
    assert runtime.values["resume_requested"] is True
    asyncio.run(runtime.async_set_strategy("comfort"))
    assert runtime.strategy == "comfort"
    calls = runtime.hass.config_entries.async_update_entry.call_count
    asyncio.run(runtime.async_set_strategy("comfort"))
    assert runtime.hass.config_entries.async_update_entry.call_count == calls
    remove()
    runtime.publish({"after_remove": True})
    assert updates[-1].get("after_remove") is None


def test_runtime_resume_resolves_broker_pending_before_reconciliation() -> None:
    runtime = _runtime()
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.COMMAND_FAULT,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
        resume_required=True,
    )

    class Broker:
        def __init__(self) -> None:
            self.resumed: list[str] = []

        async def async_resume_target(self, identity: str) -> None:
            self.resumed.append(identity)

        def invalidate(self, _identity: str) -> None:
            return None

    broker = Broker()
    runtime.broker = cast(Any, broker)
    asyncio.run(runtime.async_resume())

    assert broker.resumed == ["registry-1"]
    assert runtime.ownership["registry-1"].ownership is Ownership.RECONCILING
    assert runtime.ownership["registry-1"].resume_required is False


def test_control_status_keeps_ownership_target_and_data_health_separate() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.HOLD_LAST_GOOD,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    assert runtime._control_status() == "hold_last_good"
    runtime.ownership["registry-1"] = replace(
        runtime.ownership["registry-1"],
        data_readiness=DataReadiness.READY,
        target_readiness=TargetReadiness.SUSPENDED_MODE,
    )
    assert runtime._control_status() == "suspended_mode"
    runtime.ownership["registry-1"] = replace(
        runtime.ownership["registry-1"],
        ownership=Ownership.MANUAL_OVERRIDE,
    )
    assert runtime._control_status() == "manual_override"


@pytest.mark.parametrize(
    ("ownership", "data_readiness", "target_readiness", "expected"),
    [
        (
            Ownership.COMMAND_FAULT,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "command_fault",
        ),
        (
            Ownership.RECONCILING,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "reconciling",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.UNAVAILABLE,
            "unavailable",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.INCOMPATIBLE,
            "incompatible",
        ),
        (
            Ownership.OWNED,
            DataReadiness.INVALID,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "invalid",
        ),
        (
            Ownership.OWNED,
            DataReadiness.FALLBACK_READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "fallback_ready",
        ),
        (
            Ownership.OWNED,
            DataReadiness.DEGRADED_READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "degraded_ready",
        ),
        (
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            "ready",
        ),
    ],
)
def test_control_status_exposes_each_safety_state(
    ownership: Ownership,
    data_readiness: DataReadiness,
    target_readiness: TargetReadiness,
    expected: str,
) -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", ownership, data_readiness, target_readiness
    )
    assert runtime._control_status() == expected


def test_control_status_is_unknown_without_a_registered_target() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    assert runtime._control_status() == "unknown"
    runtime.stale_safety_applied.add("registry-1")
    assert runtime._control_status() == "stale_safety"


def test_ownership_transition_is_published_without_waiting_for_calculation() -> None:
    runtime = _runtime()
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        target_identity="registry-1",
        ownership=Ownership.OWNED,
        data_readiness=DataReadiness.READY,
        target_readiness=TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.values = {
        "control_status": "owned",
        "control_eligible": True,
        "resume_required": False,
    }
    runtime._transition("registry-1", OwnershipEvent.DISABLE)

    assert runtime.values["control_status"] == "ready"
    assert runtime.values["control_eligible"] is False
    assert runtime.values["resume_required"] is False
    assert runtime.values["ownership"] == {"registry-1": "disabled"}


def test_stale_primary_keeps_last_valid_outputs_visible_and_labelled() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")

    current = runtime._observable_values(valid)
    runtime.values = current
    assert current["thermal_sensation"] is not None
    assert current["data_quality"] == "current"

    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    held = runtime._observable_values(stale)

    assert stale.hold_condition == "primary_temperature_stale"
    assert held["thermal_sensation"] == current["thermal_sensation"]
    assert held["lower_comfort_boundary"] == current["lower_comfort_boundary"]
    assert held["heating_control_target"] == current["heating_control_target"]
    assert held["upper_comfort_boundary"] == current["upper_comfort_boundary"]
    assert held["root_sensation_votes"] == current["root_sensation_votes"]
    assert held["effective_targets"] == current["effective_targets"]
    assert held["target_scenarios"] == current["target_scenarios"]
    assert held["input_status"] == "primary_temperature_stale"
    assert held["data_quality"] == "stale"
    assert all(
        detail["stale"] is True and detail["mode"] == "stale_hold"
        for detail in held["effective_target_details"].values()
    )

    runtime.stale_safety_applied.add("registry-climate-living-room")
    runtime.values["effective_targets"] = {"target-living-room": {"temperature": 18.0}}
    runtime.values["effective_target_details"] = {"target-living-room": {"mode": "stale_safety"}}
    guarded = runtime._observable_values(stale)
    assert guarded["effective_targets"] == {"target-living-room": {"temperature": 18.0}}
    assert guarded["effective_target_details"] == {"target-living-room": {"mode": "stale_safety"}}


def test_stale_secondary_source_cannot_replace_fully_valid_snapshot() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")
    current = runtime._observable_values(valid)
    saved_values = dict(runtime.last_valid_values)
    saved_at = runtime.last_valid_at
    assert snapshot.primary is not None
    assert snapshot.relative_humidity is not None

    stale_now = snapshot.now + timedelta(hours=1)
    stale_rh = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=stale_now,
            primary=replace(snapshot.primary, observed_at=stale_now),
            source_states=valid.source_states,
        )
    )
    held = runtime._observable_values(stale_rh)

    assert stale_rh.hold_condition == "primary_rh_stale"
    assert runtime.last_valid_values == saved_values
    assert runtime.last_valid_at == saved_at
    assert held["target_scenarios"] == current["target_scenarios"]
    assert held["data_quality"] == "stale"


def test_persisted_last_valid_outputs_are_used_after_runtime_reload() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    prior = _runtime(target_identity="registry-climate-living-room")
    prior_values = prior._observable_values(valid)

    reloaded = _runtime(target_identity="registry-climate-living-room")
    reloaded.last_valid_values = dict(prior.last_valid_values)
    reloaded.last_valid_at = prior.last_valid_at
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )

    restored = reloaded._observable_values(stale)

    assert restored["thermal_sensation"] == prior_values["thermal_sensation"]
    assert restored["effective_targets"] == prior_values["effective_targets"]
    assert restored["data_quality"] == "stale"
    assert restored["input_status"] == "primary_temperature_stale"


async def test_changed_last_valid_output_is_persisted_once() -> None:
    class Persistence:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def async_update_last_valid_output(
            self, *, values_json: str, observed_at: str
        ) -> bool:
            self.calls.append((values_json, observed_at))
            return True

    runtime = _runtime()
    runtime.hass.async_create_task = asyncio.create_task
    persistence = Persistence()
    runtime.persistence = cast(Any, persistence)
    runtime.last_valid_values = {"thermal_sensation": -0.2}
    runtime.last_valid_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    runtime._schedule_last_valid_persistence()
    await asyncio.gather(*tuple(runtime.background_tasks))
    runtime._schedule_last_valid_persistence()

    assert persistence.calls == [('{"thermal_sensation":-0.2}', "2026-09-13T12:00:00+00:00")]


async def test_failed_last_valid_output_persistence_can_retry() -> None:
    class Persistence:
        async def async_update_last_valid_output(self, **_kwargs: Any) -> bool:
            return False

    runtime = _runtime()
    runtime.persistence = cast(Any, Persistence())
    runtime.last_valid_persistence_payload = '{"thermal_sensation":-0.2}'

    await runtime._async_persist_last_valid_output(
        runtime.last_valid_persistence_payload,
        datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )

    assert runtime.last_valid_persistence_payload is None


async def test_stale_safety_uses_configured_fallback_without_hvac_command(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "control_enabled": True,
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 17.5,
            "fallback_cooling_c": 26.0,
        }
    )
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.HOLD_LAST_GOOD,
        TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.capability_generations["registry-1"] = 1
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=1, minutes=1),
            now - timedelta(hours=1, minutes=1),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        ),
        recovering=True,
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Broker:
        def __init__(self) -> None:
            self.intents: list[Any] = []

        async def async_submit(self, intent: Any, *, now: datetime) -> CommandOutcome:
            self.intents.append(intent)
            return CommandOutcome(
                "safety-command",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.NOT_APPLICABLE,
                "dispatched",
            )

    broker = Broker()
    runtime.broker = cast(Any, broker)

    await runtime._async_apply_stale_safety()

    assert len(broker.intents) == 1
    intent = broker.intents[0]
    assert intent.temperature_ha == 17.5
    assert intent.safety_deescalation is True
    assert intent.explicit_transition is True
    assert runtime.values["effective_targets"] == {"target-1": {"temperature": 17.5}}
    assert runtime.values["effective_target_details"]["target-1"] == {
        "mode": "stale_safety",
        "reason": "primary_temperature_stale",
        "fallback": True,
        "stale": True,
        "safety_deescalation": True,
    }
    assert runtime.values["control_status"] == "stale_safety"
    assert runtime.values["control_eligible"] is False

    await runtime._async_apply_stale_safety()
    assert len(broker.intents) == 1


async def test_stale_safety_runs_through_real_broker_to_exact_service_payload(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def capture(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture)
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 26.0,
        }
    )
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.INVALID,
        TargetReadiness.AVAILABLE_SUPPORTED,
        revision=1,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime_module.get_lease_registry(hass).acquire("registry-1", runtime.entry.entry_id)
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 22.0,
            "unit_of_measurement": "°C",
        },
    )

    class Persistence:
        async def async_persist_pending(self, _command: Any) -> bool:
            return True

        async def async_mark_dispatched(self, _command: Any) -> bool:
            return True

        async def async_resolve(self, _command: Any, _reason: str) -> bool:
            return True

    runtime.broker = CommandBroker(
        service=HomeAssistantClimateService(hass),
        persistence=Persistence(),
        preflight=runtime._broker_preflight,
        command_id_factory=lambda: "stale-safety-command",
        context_factory=runtime._context_token,
    )

    await runtime._async_apply_stale_safety()

    assert [dict(call.data) for call in calls] == [
        {"entity_id": "climate.target", "temperature": 18.0}
    ]
    assert all("hvac_mode" not in call.data for call in calls)
    assert runtime.values["control_status"] == "stale_safety"
    for cancel in runtime.timers.values():
        cancel()


async def test_stale_safety_reports_unready_and_unneeded_targets(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.data["targets"] = [
        {
            "target_uuid": "unready",
            "entity_id": "climate.unready",
            "registry_identity": "registry-unready",
        },
        {
            "target_uuid": "settled",
            "entity_id": "climate.settled",
            "registry_identity": "registry-settled",
        },
    ]
    now = dt_util.utcnow()
    runtime.source_states["primary"] = SourceState(
        last_accepted=Observation(
            "registry:room",
            19.0,
            "°C",
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            Provenance.MEASURED,
            ObservationValidity.VALID,
        )
    )
    hass.states.async_set("climate.unready", "unavailable")
    hass.states.async_set(
        "climate.settled",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 18.0,
            "unit_of_measurement": "°C",
        },
    )

    class UnusedBroker:
        async def async_submit(self, _intent: Any, *, now: datetime) -> CommandOutcome:
            raise AssertionError("no safety command expected")

    runtime.broker = cast(Any, UnusedBroker())
    await runtime._async_apply_stale_safety()

    assert runtime.values["command_outcomes"] == {
        "unready": "stale_safety_target_not_ready",
        "settled": "stale_safety_not_required",
    }
    assert runtime.values["stale_safety_active"] is False


async def test_stale_safety_requires_control_broker_and_old_accepted_value(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    await runtime._async_apply_stale_safety()

    runtime.control_enabled = True
    runtime.broker = cast(Any, SimpleNamespace())
    await runtime._async_apply_stale_safety()

    assert runtime.values == {}


def test_stale_safety_can_recover_timestamped_sensor_evidence_after_reload() -> None:
    runtime = _runtime()
    observed_at = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
    state = State(
        "sensor.room",
        "19.25",
        {"unit_of_measurement": "°C"},
        last_changed=observed_at,
        last_reported=observed_at,
        last_updated=observed_at,
    )
    runtime.entry.data["primary_temperature"] = "sensor.room"
    runtime.hass = cast(
        Any,
        SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state)),
    )

    assert runtime._primary_safety_reference() == (19.25, observed_at)


def test_stale_safety_never_increases_heating_demand(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 26.0,
            "fallback_heating_c": 18.0,
        }
    )
    capability = ClimateCapabilitySnapshot(
        "heat",
        ("off", "heat"),
        1,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        18.0,
    )
    mapping = runtime_module.resolve_capability(capability)
    assert not isinstance(mapping, runtime_module.ClimateFailure)

    assert runtime._stale_safety_target("target-1", capability, mapping, 19.0) is None


def test_stale_safety_normalizes_cooling_and_range_away_from_demand(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update(
        {
            "minimum_control_temperature": 16.0,
            "maximum_control_temperature": 28.0,
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 27.0,
            "minimum_range_gap": 1.0,
        }
    )
    cooling = ClimateCapabilitySnapshot(
        "cool",
        ("off", "cool"),
        1,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        21.0,
    )
    cooling_mapping = runtime_module.resolve_capability(cooling)
    assert not isinstance(cooling_mapping, runtime_module.ClimateFailure)
    cooling_result = runtime._stale_safety_target("target-1", cooling, cooling_mapping, 24.0)
    assert isinstance(cooling_result, NormalizedScalarTarget)
    assert cooling_result.normalized_actuator_c == 27.0

    ranged = ClimateCapabilitySnapshot(
        "heat_cool",
        ("off", "heat_cool"),
        2,
        5.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        None,
        21.0,
        24.0,
    )
    ranged_mapping = runtime_module.resolve_capability(ranged)
    assert not isinstance(ranged_mapping, runtime_module.ClimateFailure)
    ranged_result = runtime._stale_safety_target("target-1", ranged, ranged_mapping, 19.0)
    assert isinstance(ranged_result, NormalizedRangeTarget)
    assert ranged_result.heating.normalized_actuator_c == 18.0
    assert ranged_result.cooling.normalized_actuator_c == 27.0

    runtime._publish_stale_safety_target("target-1", ranged_result)
    assert runtime.values["effective_targets"] == {
        "target-1": {"target_low": 18.0, "target_high": 27.0}
    }
    assert (
        runtime._stale_safety_target(
            "target-1", replace(ranged, target_temp_low_ha=None), ranged_mapping, 19.0
        )
        is None
    )
    assert (
        runtime._stale_safety_target(
            "target-1", replace(cooling, scalar_target_ha=None), cooling_mapping, 24.0
        )
        is None
    )


def test_stale_safety_timer_arms_cancels_and_clears_after_valid_recovery(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    valid = calculate_runtime_snapshot(snapshot)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True

    runtime._schedule_stale_safety(stale)
    assert "stale_safety" in runtime.timers

    runtime.stale_safety_applied.add("registry-1")
    runtime._schedule_stale_safety(valid)
    assert "stale_safety" not in runtime.timers
    assert runtime.stale_safety_applied == set()

    runtime._schedule_stale_safety(replace(stale, source_states=()))
    assert "stale_safety" not in runtime.timers

    runtime._schedule_stale_safety(stale)
    assert "stale_safety" in runtime.timers
    runtime.stale_safety_applied.add("registry-1")
    runtime.control_enabled = False
    runtime._schedule_stale_safety(stale)
    assert "stale_safety" not in runtime.timers
    assert runtime.stale_safety_applied == set()


def test_control_enable_uses_domain_lease_and_rolls_back_conflict() -> None:
    first = _runtime(entry_id="first")
    second = _runtime(entry_id="second")
    second.hass = first.hass
    asyncio.run(first.async_set_control_enabled(True))
    assert first.control_enabled
    with pytest.raises(ValueError, match="already_controlled"):
        asyncio.run(second.async_set_control_enabled(True))
    assert not second.control_enabled
    asyncio.run(first.async_set_control_enabled(False))
    asyncio.run(second.async_set_control_enabled(True))
    assert second.control_enabled


def test_multi_target_enable_is_atomic_when_a_later_lease_conflicts() -> None:
    owner = _runtime(entry_id="owner", target_identity="registry-2")
    contender = _runtime(entry_id="contender", target_identity="registry-1")
    contender.hass = owner.hass
    contender.entry.data["targets"].append(
        {
            "target_uuid": "target-2",
            "entity_id": "climate.second",
            "registry_identity": "registry-2",
        }
    )
    asyncio.run(owner.async_set_control_enabled(True))

    with pytest.raises(ValueError, match="already_controlled"):
        asyncio.run(contender.async_set_control_enabled(True))

    leases = owner.hass.data["athb"]["target_leases"]
    assert leases.owner("registry-1") is None
    assert leases.owner("registry-2") == "owner"
    assert "registry-1" not in contender.ownership


def test_unload_cancels_listeners_releases_lease_and_clears_callbacks() -> None:
    runtime = _runtime()
    asyncio.run(runtime.async_set_control_enabled(True))
    removed: list[bool] = []
    runtime.listeners.append(lambda: removed.append(True))
    runtime.subscribe(lambda: None)
    asyncio.run(runtime.async_unload())
    assert removed == [True]
    assert runtime.listeners == []
    assert runtime.update_callbacks == set()
    assert runtime.hass.data["athb"]["target_leases"].owner("registry-1") is None


async def test_runtime_tracks_reports_debounces_and_captures_coherent_snapshot(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-1",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "measured",
            "rh_entity": "sensor.rh",
            "targets": [],
        },
        options={"comfort_strategy": "balanced"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-1", "balanced", "off", False)
    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    hass.states.async_set("sensor.room", "21.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "12.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.rh", "48", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert runtime.debounce_cancel is not None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    await runtime.controller.async_wait_idle()
    assert runtime.values["source_states"]["sensor.room"]["state"] == "21.5"
    assert runtime.values["source_states"]["sensor.rh"]["unit"] == "%"
    assert runtime.values["strategy"] == "balanced"
    await runtime.async_unload()


async def test_unchanged_source_report_renews_freshness_without_target_feedback(
    hass: HomeAssistant,
) -> None:
    attributes = {"unit_of_measurement": "°C"}
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "temperature": 18.0,
        "unit_of_measurement": "°C",
    }
    hass.states.async_set("sensor.room", "21.5", attributes)
    hass.states.async_set("climate.target", "heat", target_attributes)
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-reported",
            "primary_temperature": "sensor.room",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-reported", "balanced", "off", False)
    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    initial_generation = runtime.input_generation
    initial_state = hass.states.get("sensor.room")
    assert initial_state is not None
    initial_reported = initial_state.last_reported

    hass.states.async_set("sensor.room", "21.5", attributes)
    await hass.async_block_till_done()

    reported_state = hass.states.get("sensor.room")
    assert reported_state is not None
    assert reported_state.last_reported > initial_reported
    assert runtime.input_generation == initial_generation + 1
    assert runtime.debounce_cancel is not None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    await runtime.controller.async_wait_idle()

    assert (
        runtime.values["source_states"]["sensor.room"]["last_reported"]
        == reported_state.last_reported.isoformat()
    )
    accepted_primary = runtime.source_states["primary"].last_accepted
    assert accepted_primary is not None
    assert accepted_primary.observed_at == reported_state.last_reported
    assert not runtime.source_states["primary"].recovering
    assert runtime.debounce_cancel is None

    target_generation = runtime.input_generation
    hass.states.async_set("climate.target", "heat", target_attributes)
    await hass.async_block_till_done()

    assert runtime.input_generation == target_generation
    assert runtime.debounce_cancel is None
    await runtime.async_unload()


async def test_runtime_uses_pipeline_and_broker_for_exact_target_only_service(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def capture(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "min_temp": 16.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "temperature": 17.0,
            "unit_of_measurement": "°C",
        },
    )
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-command",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-command", "balanced", "off", True)

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert [dict(call.data) for call in calls] == [
        {"entity_id": "climate.target", "temperature": 18.0}
    ]
    assert all("hvac_mode" not in call.data for call in calls)
    assert runtime.values["rh_provenance"] == "declared"
    assert runtime.values["calculation"].targets[0].result.policy.fallback
    await runtime.async_unload()


@pytest.mark.parametrize(
    ("stored_fingerprint", "expected_reason", "expected_reasons"),
    [
        (None, "unclean_shutdown", ("unclean_shutdown",)),
        (
            "stale-configuration",
            "configuration_changed",
            ("configuration_changed", "unclean_shutdown"),
        ),
    ],
)
async def test_restart_without_pending_command_reasserts_without_resume(
    hass: HomeAssistant,
    stored_fingerprint: str | None,
    expected_reason: str,
    expected_reasons: tuple[str, ...],
) -> None:
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "temperature": 17.0,
        "unit_of_measurement": "°C",
    }
    acknowledged_attributes = {**target_attributes, "temperature": 18.0}
    calls: list[ServiceCall] = []

    async def acknowledge(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set(
            "climate.target", "heat", acknowledged_attributes, context=call.context
        )

    hass.services.async_register("climate", "set_temperature", acknowledge)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.target", "heat", target_attributes)
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-unclean-reassert",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(
        hass,
        cast(Any, entry),
        "zone-unclean-reassert",
        "balanced",
        "off",
        True,
    )
    stored = ControlStoreState(
        storage_generation=4,
        run_id="prior-run",
        clean_shutdown=False,
        configuration_fingerprint=stored_fingerprint or runtime._configuration_fingerprint(),
        strategy="balanced",
        actuators=(
            StoredActuator(
                target_identity="registry-1",
                ownership="owned",
                ownership_revision=3,
                external_revision=0,
                override_reason=None,
                override_expiry=None,
                resume_required=False,
                last_observed_target_fingerprint=None,
                last_command_id=None,
                last_command_payload_fingerprint=None,
                last_command_context_id=None,
                pending_command=None,
            ),
        ),
        control_enabled_intent=True,
    )
    await HomeAssistantControlStorageBackend(hass, "zone-unclean-reassert").async_save(
        serialize_control_state(stored)
    )

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert [dict(call.data) for call in calls] == [
        {"entity_id": "climate.target", "temperature": 18.0}
    ]
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    assert not runtime.ownership["registry-1"].resume_required
    assert runtime.values["recovery_reason"] == expected_reason
    assert runtime.values["recovery_reasons"] == expected_reasons
    assert runtime.values["command_outcomes"]["target-1"] == "own_context_match"
    await runtime.async_unload()


async def test_same_value_climate_report_acknowledges_command_end_to_end(
    hass: HomeAssistant,
) -> None:
    target_attributes = {
        "hvac_modes": ["off", "heat"],
        "supported_features": 1,
        "min_temp": 16.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        "temperature": 18.0,
        "unit_of_measurement": "°C",
    }
    calls: list[ServiceCall] = []

    async def report_unchanged(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set("climate.target", "heat", target_attributes, context=call.context)

    hass.services.async_register("climate", "set_temperature", report_unchanged)
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.target", "heat", {**target_attributes, "temperature": 17.0})
    entry = MockConfigEntry(
        domain="athb",
        title="Zone",
        data={
            "zone_uuid": "zone-same-value",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_mode": "declared",
            "rh_declared": 50.0,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "comfort_strategy": "balanced",
            "control_enabled": True,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-same-value", "balanced", "off", True)

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert runtime.broker is not None
    assert runtime.broker.state_counts("registry-1") == (0, 0)
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    await runtime.async_unload()


async def test_manual_override_and_boost_deadlines_expire_without_polling(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-timers",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={"manual_override_minutes": 15.0, "boost_duration_minutes": 5.0},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-timers", "balanced", "off", True)
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime._transition("registry-1", OwnershipEvent.EXTERNAL_TARGET)
    expiry = runtime.ownership["registry-1"].override_expiry
    assert expiry is not None
    assert "override:registry-1" in runtime.timers
    runtime.timers.pop("override:registry-1")()
    runtime._transition(
        "registry-1", OwnershipEvent.OVERRIDE_EXPIRED, now=expiry + timedelta(seconds=1)
    )
    assert runtime.ownership["registry-1"].ownership is Ownership.RECONCILING

    await runtime.async_set_boost_mode("adaptive")
    boost_expiry = cast(datetime, runtime.values["boost_expiry"])
    assert runtime.boost_mode == "adaptive"
    assert boost_expiry > expiry - timedelta(minutes=15)
    runtime.timers.pop("boost")()
    await runtime.async_set_boost_mode("off")
    assert runtime.boost_mode == "off"


async def test_failure_hold_uses_event_timer_and_elapses_after_fifteen_minutes(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass

    runtime._update_failure_hold("extrapolation_rejected")

    assert runtime.failure_hold_reason == "extrapolation_rejected"
    assert runtime.failure_hold_elapsed is False
    assert "failure_hold" in runtime.timers
    original_timer = runtime.timers["failure_hold"]
    runtime._update_failure_hold("extrapolation_rejected")
    assert runtime.timers["failure_hold"] is original_timer

    runtime._update_failure_hold(None)
    assert runtime.failure_hold_reason is None
    assert runtime.failure_hold_elapsed is False
    assert "failure_hold" not in runtime.timers

    runtime._update_failure_hold("extrapolation_rejected")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done()

    assert runtime.failure_hold_elapsed is True
    assert runtime.input_generation == 2
    assert "failure_hold" not in runtime.timers

    runtime._update_failure_hold(None)
    assert runtime.failure_hold_reason is None
    assert runtime.failure_hold_elapsed is False
    assert runtime.timers == {}


def test_occupancy_setback_holds_last_known_state_then_reports_unknown(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-auto", "targets": []},
        options={"occupancy_entity": "binary_sensor.occupied"},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-auto", "balanced", "off", False)
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    hass.states.async_set("binary_sensor.occupied", "off")
    assert runtime._resolve_profile(now).resolved.value == "eco"
    hass.states.async_set("binary_sensor.occupied", "unknown")
    assert runtime._resolve_profile(now + timedelta(minutes=20)).resolved.value == "eco"
    expired = runtime._resolve_profile(now + timedelta(minutes=31))
    assert expired.resolved.value == "comfort"
    assert expired.reasons == ("occupancy_unknown",)


def test_outdoor_history_sample_converts_fahrenheit_exactly_once(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={"zone_uuid": "zone-unit", "outdoor_source": "sensor.outdoor", "targets": []},
        options={},
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-unit", "balanced", "off", False)
    hass.states.async_set("sensor.outdoor", "50", {"unit_of_measurement": "°F"})
    sample = runtime._outdoor_sample(datetime(2026, 9, 10, 10, tzinfo=UTC))
    assert sample is not None
    assert sample.value_c == pytest.approx(10.0)


def test_runtime_coordination_uses_manual_opposing_target_observation() -> None:
    scenario = load_scenarios()[0]
    snapshot = _captured(scenario)
    cooling_capability = ClimateCapabilitySnapshot(
        "cool",
        ("off", "cool"),
        1,
        16.0,
        30.0,
        0.5,
        TemperatureUnit.CELSIUS,
        20.0,
        None,
        None,
    )
    snapshot = replace(
        snapshot,
        targets=(
            snapshot.targets[0],
            CapturedTarget(
                "cool-target",
                "registry-cool",
                "climate.cool",
                cooling_capability,
            ),
        ),
    )
    calculation = calculate_runtime_snapshot(snapshot)
    runtime = _runtime(target_identity="registry-climate-living-room")
    runtime.entry.data["targets"].append(
        {
            "target_uuid": "cool-target",
            "entity_id": "climate.cool",
            "registry_identity": "registry-cool",
        }
    )
    runtime.ownership = {
        "registry-climate-living-room": OwnershipState(
            "registry-climate-living-room", Ownership.OWNED
        ),
        "registry-cool": OwnershipState("registry-cool", Ownership.MANUAL_OVERRIDE),
    }
    assert (
        runtime._runtime_coordination_reason(calculation.targets[0], calculation)
        == "cross_actuator_conflict"
    )


def test_runtime_tracks_all_progressive_sources_and_critical_freshness(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        entry_id="entry-sources",
        data={
            "zone_uuid": "zone-sources",
            "primary_temperature": "sensor.room",
            "outdoor_source": "sensor.outdoor",
            "rh_entity": "sensor.rh",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "occupancy_entity": "binary_sensor.occupied",
            "air_speed_entity": "sensor.air_speed",
            "radiant_model": "surface",
            "surface_temperature_entity": "sensor.surface",
            "critical_locations": [
                {
                    "location_id": "seat",
                    "entity_id": "sensor.seat",
                    "mode": "heating_guard",
                },
                "invalid-entry",
            ],
        },
    )
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-sources", "balanced", "off", False)
    for entity_id, value, unit in (
        ("sensor.room", "20", "°C"),
        ("sensor.outdoor", "5", "°C"),
        ("sensor.rh", "50", "%"),
        ("sensor.air_speed", "0.1", "m/s"),
        ("sensor.surface", "16", "°C"),
        ("sensor.seat", "18.5", "°C"),
    ):
        hass.states.async_set(entity_id, value, {"unit_of_measurement": unit})
    tracked = runtime._tracked_entity_ids()
    assert {
        "sensor.room",
        "sensor.rh",
        "sensor.air_speed",
        "sensor.surface",
        "sensor.seat",
        "binary_sensor.occupied",
        "climate.target",
    } <= tracked
    assert "sensor.outdoor" not in tracked
    runtime._update_critical_delta("sensor.seat", hass.states.get("sensor.seat"))
    assert runtime.critical_delta_states["seat"].report_count == 1
    runtime._schedule_freshness_expiries(dt_util.utcnow(), "sensor.surface")
    assert "freshness:critical:seat" in runtime.timers
    assert "freshness:radiant" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


def test_runtime_reads_mold_indicator_critical_point_attribute_as_temperature(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    hass.states.async_set(
        "sensor.mold_indicator",
        "86.2",
        {
            "unit_of_measurement": "%",
            "estimated_critical_temp": 16.7,
            "dewpoint": 13.2,
        },
    )

    captured = runtime._snapshot_mold_indicator(hass.states.get("sensor.mold_indicator"))

    assert captured is not None
    assert captured.raw_state == 16.7
    assert captured.unit == str(hass.config.units.temperature_unit)
    assert captured.attributes == {"estimated_critical_temp": 16.7}


def test_service_and_registry_events_distinguish_own_context_and_removal(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    stored = SimpleNamespace(target_identity="registry-1", last_command_context_id="own-context")
    runtime.persistence = cast(Any, SimpleNamespace(state=SimpleNamespace(actuators=(stored,))))

    ignored = SimpleNamespace(
        data={"domain": "light", "service": "turn_on"}, context=Context(id="external")
    )
    runtime._service_event(cast(Any, ignored))
    own = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": "climate.target"},
        },
        context=Context(id="own-context"),
    )
    runtime._service_event(cast(Any, own))
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED
    runtime.persistence = None
    external = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": ["climate.target"]},
        },
        context=Context(id="external"),
    )
    runtime._service_event(cast(Any, external))
    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE

    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "update", "entity_id": "climate.target"}))
    )
    runtime._entity_registry_event(
        cast(Any, SimpleNamespace(data={"action": "remove", "entity_id": "climate.target"}))
    )
    assert repair_calls[-1] == ("removed_source_or_target", True)
    for cancel in runtime.timers.values():
        cancel()


def test_registry_rename_preserves_target_identity_and_updates_live_tracking(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-rename",
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": "climate.target",
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={"comfort_strategy": "balanced", "control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename", "balanced", "off", False)
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": "climate.target",
                    "entity_id": "climate.renamed_target",
                }
            ),
        )
    )

    assert entry.data["targets"][0] == {
        "target_uuid": "target-1",
        "entity_id": "climate.renamed_target",
        "registry_identity": "registry-1",
    }
    assert runtime.input_generation == 2
    assert runtime.listeners
    assert repair_calls == []
    for unsubscribe in runtime.listeners:
        unsubscribe()
    if runtime.debounce_cancel is not None:
        runtime.debounce_cancel()


def test_registry_rename_updates_every_configured_source_kind(hass: HomeAssistant) -> None:
    old_entity_id = "sensor.shared"
    new_entity_id = "sensor.renamed_shared"
    entry = MockConfigEntry(
        domain="athb",
        data={
            "zone_uuid": "zone-rename-all",
            "primary_temperature": old_entity_id,
            "outdoor_source": old_entity_id,
            "rh_entity": old_entity_id,
            "targets": [
                {
                    "target_uuid": "target-1",
                    "entity_id": old_entity_id,
                    "registry_identity": "registry-1",
                }
            ],
        },
        options={
            "occupancy_entity": old_entity_id,
            "air_speed_entity": old_entity_id,
            "mrt_entity": old_entity_id,
            "globe_temperature_entity": old_entity_id,
            "surface_temperature_entity": old_entity_id,
            "mold_indicator_entity": old_entity_id,
            "critical_locations": [
                {"location_id": "window", "entity_id": old_entity_id},
                "preserved-extension-value",
            ],
        },
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "zone-rename-all", "balanced", "off", False)
    renamed_history: list[str] = []
    runtime.history_collector = cast(Any, SimpleNamespace(update_entity_id=renamed_history.append))
    runtime.repair_manager = cast(Any, SimpleNamespace(update=lambda *_args: None))

    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": old_entity_id,
                    "entity_id": new_entity_id,
                }
            ),
        )
    )

    assert entry.data["primary_temperature"] == new_entity_id
    assert entry.data["outdoor_source"] == new_entity_id
    assert entry.data["rh_entity"] == new_entity_id
    assert entry.data["targets"][0]["entity_id"] == new_entity_id
    for key in (
        "occupancy_entity",
        "air_speed_entity",
        "mrt_entity",
        "globe_temperature_entity",
        "surface_temperature_entity",
        "mold_indicator_entity",
    ):
        assert entry.options[key] == new_entity_id
    assert entry.options["critical_locations"] == [
        {"location_id": "window", "entity_id": new_entity_id},
        "preserved-extension-value",
    ]
    assert renamed_history == [new_entity_id]

    generation = runtime.input_generation
    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": "sensor.unconfigured",
                    "entity_id": "sensor.still_unconfigured",
                }
            ),
        )
    )
    assert runtime.input_generation == generation
    for unsubscribe in runtime.listeners:
        unsubscribe()
    if runtime.debounce_cancel is not None:
        runtime.debounce_cancel()


def test_external_temperature_target_uses_home_assistant_area_selection(
    hass: HomeAssistant,
) -> None:
    area = ar.async_get(hass).async_create("Living room")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("climate", "test", "target", suggested_object_id="target")
    registry.async_update_entity(entry.entity_id, area_id=area.id)
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )

    event = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"area_id": area.id, "temperature": 21.0},
        },
        context=Context(id="external-area-call"),
    )
    runtime._service_event(cast(Any, event))

    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    for cancel in runtime.timers.values():
        cancel()


def test_external_temperature_target_all_selector_is_not_missed(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    event = SimpleNamespace(
        data={
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"entity_id": "all", "temperature": 21.0},
        },
        context=Context(id="external-all-call"),
    )

    runtime._service_event(cast(Any, event))

    assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    for cancel in runtime.timers.values():
        cancel()


class _FeedbackBroker:
    def __init__(self, outcomes: list[CommandOutcome], *, pending: int = 0) -> None:
        self.outcomes = outcomes
        self.pending = pending
        self.feedbacks: list[Any] = []
        self.invalidated: list[str] = []

    def state_counts(self, _identity: str) -> tuple[int, int]:
        return (self.pending, 0)

    async def async_feedback(self, feedback: Any, *, now: datetime) -> CommandOutcome:
        self.feedbacks.append(feedback)
        return self.outcomes.pop(0)

    def invalidate(self, identity: str) -> None:
        self.invalidated.append(identity)

    async def async_drain_queued(self, _identity: str, *, now: datetime) -> CommandOutcome | None:
        return None


async def test_target_return_after_startup_is_reconciled_not_manual_override(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.RECONCILING,
        DataReadiness.READY,
        TargetReadiness.UNAVAILABLE,
    )
    runtime.capability_generations["registry-1"] = 1
    unavailable = State("climate.target", "unavailable")
    runtime.last_target_fingerprints["registry-1"] = runtime._fingerprint(
        capability_from_state(unavailable)
    )
    broker = _FeedbackBroker([])
    runtime.broker = cast(Any, broker)
    available = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 19.5,
            "unit_of_measurement": "°C",
        },
    )

    await runtime._async_handle_target_state(
        runtime.entry.data["targets"][0], unavailable, available
    )

    state = runtime.ownership["registry-1"]
    assert state.ownership is Ownership.RECONCILING
    assert state.target_readiness is TargetReadiness.AVAILABLE_SUPPORTED
    assert state.external_revision == 0
    assert runtime.capability_generations["registry-1"] == 2
    assert broker.feedbacks == []


async def test_unchanged_target_report_acknowledges_pending_command_without_recalculation(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    acknowledged = CommandOutcome(
        "command",
        DispatchStatus.DISPATCHED,
        AcknowledgementStatus.ACKNOWLEDGED,
        "own_context_match",
    )
    broker = _FeedbackBroker([acknowledged], pending=1)
    runtime.broker = cast(Any, broker)
    generation = runtime.input_generation
    current = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 19.5,
            "unit_of_measurement": "°C",
        },
    )

    await runtime._async_handle_target_report(
        runtime.entry.data["targets"][0], current, "athb-context", None
    )
    await hass.async_block_till_done()

    assert len(broker.feedbacks) == 1
    assert broker.feedbacks[0].context_id == "athb-context"
    assert broker.feedbacks[0].observed.temperature_ha == 19.5
    assert runtime.input_generation == generation
    assert runtime.ownership["registry-1"].ownership is Ownership.OWNED


async def test_target_feedback_rejection_repair_and_hvac_mode_reconciliation(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime.last_target_fingerprints["registry-1"] = runtime._fingerprint(
        ClimateCapabilitySnapshot(
            "heat", ("off", "heat"), 1, 5.0, 35.0, 0.5, TemperatureUnit.CELSIUS, 18.0
        )
    )
    rejected = CommandOutcome(
        "command",
        DispatchStatus.DISPATCHED,
        AcknowledgementStatus.REJECTED,
        "target_rejected",
    )
    acknowledged = replace(rejected, acknowledgement_status=AcknowledgementStatus.ACKNOWLEDGED)
    broker = _FeedbackBroker([rejected, rejected, rejected, acknowledged])
    runtime.broker = cast(Any, broker)
    repair_calls: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: repair_calls.append((name, active)))
    )
    target = runtime.entry.data["targets"][0]
    old = State(
        "climate.target",
        "heat",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 18.0,
            "unit_of_measurement": "°C",
        },
    )
    for temperature in (18.5, 19.0, 19.5):
        new = State(
            "climate.target",
            "heat",
            {
                "hvac_modes": ["off", "heat"],
                "supported_features": 1,
                "temperature": temperature,
                "unit_of_measurement": "°C",
            },
        )
        await runtime._async_handle_target_state(target, old, new)
        runtime.ownership["registry-1"] = replace(
            runtime.ownership["registry-1"], ownership=Ownership.OWNED
        )
        old = new
    assert runtime.rejection_counts["registry-1"] == 3
    assert ("persistent_target_rejection", True) in repair_calls

    new = State(
        "climate.target",
        "off",
        {
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
            "temperature": 20.0,
            "unit_of_measurement": "°C",
        },
    )
    await runtime._async_handle_target_state(target, old, new)
    await hass.async_block_till_done()
    assert runtime.capability_generations["registry-1"] == 2
    assert runtime.rejection_counts["registry-1"] == 0
    assert ("persistent_target_rejection", False) in repair_calls


async def test_runtime_apply_schedules_deadlines_and_marks_unknown(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    calculation = calculate_runtime_snapshot(_captured(scenario))
    runtime = _runtime(target_identity="registry-climate-living-room")
    runtime.hass = hass
    runtime.control_enabled = True
    runtime.entry.entry_id = "zone"
    runtime.ownership = {
        "registry-climate-living-room": OwnershipState(
            "registry-climate-living-room",
            Ownership.OWNED,
            DataReadiness.READY,
            TargetReadiness.AVAILABLE_SUPPORTED,
            revision=1,
        )
    }
    runtime.capability_generations = {"registry-climate-living-room": 1}

    class Broker:
        async def async_submit(self, _intent: Any, *, now: datetime) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.UNKNOWN,
                "command_outcome_unknown",
            )

        def invalidate(self, _identity: str) -> None:
            return None

    runtime.broker = cast(Any, Broker())
    await runtime._async_apply_calculation(calculation)
    assert runtime.values["command_outcomes"] == {"target-living-room": "command_outcome_unknown"}
    assert runtime.ownership["registry-climate-living-room"].ownership is Ownership.COMMAND_FAULT
    assert "ack:registry-climate-living-room" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


def test_runtime_marks_startup_and_data_recovery_for_live_target_reassertion() -> None:
    scenario = load_scenarios()[0]
    calculation = calculate_runtime_snapshot(_captured(scenario))
    identity = "registry-climate-living-room"
    runtime = _runtime(target_identity=identity)
    runtime.control_enabled = True
    runtime.ownership[identity] = OwnershipState(
        identity,
        Ownership.RECONCILING,
        DataReadiness.INVALID,
        TargetReadiness.UNAVAILABLE,
    )

    startup = runtime._reconcile_targets(calculation)

    assert startup == frozenset({identity})
    assert runtime.ownership[identity].ownership is Ownership.OWNED
    assert runtime._reconcile_targets(calculation) == frozenset()

    runtime.ownership[identity] = replace(
        runtime.ownership[identity], data_readiness=DataReadiness.HOLD_LAST_GOOD
    )
    recovered = runtime._reconcile_targets(calculation)

    assert recovered == frozenset({identity})


def test_explicit_transition_survives_invalid_input_and_is_consumed_by_valid_result(
    hass: HomeAssistant,
) -> None:
    scenario = load_scenarios()[0]
    snapshot = replace(_captured(scenario), explicit_transition=False)
    valid = calculate_runtime_snapshot(snapshot)
    stale = calculate_runtime_snapshot(
        replace(
            snapshot,
            now=snapshot.now + timedelta(hours=1),
            source_states=valid.source_states,
        )
    )
    identity = "registry-climate-living-room"
    runtime = _runtime(target_identity=identity)
    runtime.hass = hass
    runtime.pending_transition_reasons.clear()
    runtime.explicit_transition = False
    runtime.ownership[identity] = OwnershipState(
        identity,
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )

    runtime._publish_calculation(1, stale)

    assert runtime.explicit_transition
    assert runtime.pending_transition_reasons == {"input_recovery"}

    recovered = calculate_runtime_snapshot(replace(snapshot, explicit_transition=True))
    runtime._publish_calculation(2, recovered)

    assert not runtime.explicit_transition
    assert runtime.pending_transition_reasons == set()


def test_occupancy_change_is_an_explicit_transition(hass: HomeAssistant) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options["occupancy_entity"] = "binary_sensor.room_occupied"
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    hass.states.async_set("binary_sensor.room_occupied", "on")
    assert runtime._resolve_profile(now).resolved.value == "comfort"
    runtime.pending_transition_reasons.clear()
    runtime.explicit_transition = False

    hass.states.async_set("binary_sensor.room_occupied", "off")
    assert runtime._resolve_profile(now + timedelta(seconds=1)).resolved.value == "eco"

    assert runtime.explicit_transition
    assert runtime.pending_transition_reasons == {"occupancy"}


async def test_climate_can_be_primary_source_and_target_on_the_same_event(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.data["primary_temperature"] = "climate.target"
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1",
        Ownership.OWNED,
        DataReadiness.READY,
        TargetReadiness.AVAILABLE_SUPPORTED,
    )
    runtime.capability_generations["registry-1"] = 1
    runtime.broker = None
    old = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.0, "temperature": 19.0},
    )
    new = State(
        "climate.target",
        "heat",
        {"supported_features": 1, "current_temperature": 20.5, "temperature": 19.0},
    )
    generation = runtime.input_generation
    event = SimpleNamespace(
        data={"entity_id": "climate.target", "old_state": old, "new_state": new}
    )

    runtime._handle_state_event(cast(Any, event))
    await hass.async_block_till_done()

    assert runtime.input_generation == generation + 1
    assert runtime.debounce_cancel is not None
    runtime.debounce_cancel()


async def test_broker_timer_callbacks_cover_timeout_and_queue_paths(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.ownership["registry-1"] = OwnershipState(
        "registry-1", Ownership.OWNED, DataReadiness.READY, TargetReadiness.AVAILABLE_SUPPORTED
    )

    class Broker:
        async def async_acknowledgement_timeout(
            self, _identity: str, *, now: datetime
        ) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.DISPATCHED,
                AcknowledgementStatus.UNKNOWN,
                "command_outcome_unknown",
            )

        async def async_drain_queued(self, _identity: str, *, now: datetime) -> CommandOutcome:
            return CommandOutcome(
                "command",
                DispatchStatus.NOT_DISPATCHED,
                AcknowledgementStatus.PENDING,
                "command_interval",
            )

        def invalidate(self, _identity: str) -> None:
            return None

    runtime.broker = cast(Any, Broker())
    await runtime._async_acknowledgement_timeout("registry-1")
    assert runtime.values["last_command_outcome"] == "command_outcome_unknown"
    await runtime._async_drain_queued("registry-1")
    assert "ack:registry-1" in runtime.timers
    assert "queue:registry-1" in runtime.timers
    for cancel in runtime.timers.values():
        cancel()


async def test_startup_storage_fault_and_boost_recovery_paths(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Persistence:
        start_ok = False
        expiry = "2000-01-01T00:00:00+00:00"

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=self.expiry,
                rapid_boost_reached=False,
                actuators=(),
            )
            self.requires_resume = False

        async def async_start(self) -> bool:
            return self.start_ok

        async def async_mark_clean(self, *, now: datetime) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "ZoneCommandPersistence", Persistence)
    entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-paths",
        data={"zone_uuid": "startup-paths", "targets": []},
        options={"boost_mode": "adaptive", "control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "startup-paths", "balanced", "adaptive", False)
    await runtime.async_start()
    assert runtime.boost_mode == "off"
    assert runtime.values["control_status"] == "storage_fault"
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()
    await runtime.async_unload()

    Persistence.start_ok = True
    Persistence.expiry = "2099-01-01T00:00:00+00:00"
    future_entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-future-boost",
        data={"zone_uuid": "startup-future-boost", "targets": []},
        options={"boost_mode": "adaptive", "control_enabled": False},
    )
    future_entry.add_to_hass(hass)
    future = ZoneRuntime(
        hass, cast(Any, future_entry), "startup-future-boost", "balanced", "adaptive", False
    )
    await future.async_start()
    assert future.boost_mode == "adaptive"
    assert "boost" in future.timers
    assert future.controller is not None
    await future.controller.async_wait_idle()
    await future.async_unload()


async def test_startup_restores_persisted_display_values_before_stale_calculation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Persistence:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.state = SimpleNamespace(
                boost_expiry_utc=None,
                rapid_boost_reached=False,
                actuators=(),
                last_valid_values_json='{"thermal_sensation":-0.2}',
                last_valid_at="2026-09-13T12:00:00+00:00",
            )
            self.requires_resume = False

        async def async_start(self) -> bool:
            return True

        async def async_mark_clean(self, *, now: datetime) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "ZoneCommandPersistence", Persistence)
    entry = MockConfigEntry(
        domain="athb",
        entry_id="startup-restored-output",
        data={"zone_uuid": "startup-restored-output", "targets": []},
        options={"control_enabled": False},
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(
        hass,
        cast(Any, entry),
        "startup-restored-output",
        "balanced",
        "off",
        False,
    )

    await runtime.async_start()
    assert runtime.controller is not None
    await runtime.controller.async_wait_idle()

    assert runtime.values["thermal_sensation"] == -0.2
    assert runtime.values["data_quality"] == "stale"
    assert runtime.last_valid_at == datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    await runtime.async_unload()


def test_readiness_radiant_modes_and_noop_timer_paths() -> None:
    runtime = _runtime()
    unavailable = ClimateCapabilitySnapshot(
        "heat", ("heat",), 1, 5.0, 35.0, 0.5, TemperatureUnit.CELSIUS, 20.0, available=False
    )
    off = replace(unavailable, available=True, hvac_mode="off")
    incompatible = replace(unavailable, available=True, hvac_mode="fan_only")
    assert runtime._target_readiness(unavailable) is TargetReadiness.UNAVAILABLE
    assert runtime._target_readiness(off) is TargetReadiness.SUSPENDED_MODE
    assert runtime._target_readiness(incompatible) is TargetReadiness.INCOMPATIBLE
    for mode, expected in (
        ("direct_mrt", "direct_mrt"),
        ("globe", "globe"),
        ("surface", "surface"),
        ("mold_indicator", "surface"),
        ("uniform", "direct_mrt"),
    ):
        runtime.entry.options["radiant_model"] = mode
        assert runtime._radiant_source_kind().value == expected
    runtime._replace_timer("ignored", 1.0, lambda: None)
    assert runtime.timers == {}
    assert runtime._timer_expiry("missing") is None


async def test_delayed_repair_activates_once_and_recovers(
    hass: HomeAssistant,
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    updates: list[tuple[str, bool]] = []
    runtime.repair_manager = cast(
        Any, SimpleNamespace(update=lambda name, active: updates.append((name, active)))
    )
    runtime._update_delayed_repair("missing_history_24h", True, timedelta(seconds=1))
    runtime._update_delayed_repair("missing_history_24h", True, timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert updates == [("missing_history_24h", True)]
    runtime._update_delayed_repair("missing_history_24h", False, timedelta(seconds=1))
    assert updates[-1] == ("missing_history_24h", False)


async def test_strategy_and_boost_changes_invalidate_active_runtime_without_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="athb",
        entry_id="runtime-changes",
        data={"zone_uuid": "runtime-changes", "targets": []},
        options={"comfort_strategy": "balanced", "boost_mode": "off"},
    )
    entry.add_to_hass(hass)
    requested: list[Any] = []

    class Controller:
        def invalidate(self) -> None:
            requested.append("invalidate")

        def request(self, snapshot: Any) -> None:
            requested.append(snapshot)

    runtime = ZoneRuntime(hass, cast(Any, entry), "runtime-changes", "balanced", "off", False)
    runtime.controller = cast(Any, Controller())
    persisted: list[dict[str, Any]] = []

    class Persistence:
        async def async_update_runtime(self, **values: Any) -> bool:
            persisted.append(values)
            return True

    runtime.persistence = cast(Any, Persistence())
    cancelled: list[bool] = []
    runtime.debounce_cancel = lambda: cancelled.append(True)
    runtime.debounce_started = hass.loop.time()
    await runtime.async_set_strategy("comfort")
    assert requested[0] == "invalidate"
    assert cancelled == [True]
    assert runtime.configuration_generation == 2
    assert runtime.strategy == "comfort"
    assert entry.options["comfort_strategy"] == "comfort"

    await runtime.async_set_boost_mode("rapid")
    assert runtime.boost_mode == "rapid"
    assert "boost" in runtime.timers
    await runtime.async_set_boost_mode("off")
    assert runtime.boost_mode == "off"
    assert "boost" not in runtime.timers

    broker = MagicMock()
    runtime.broker = broker
    runtime.ownership = {"registry-1": cast(Any, object())}
    runtime.debounce_cancel = lambda: cancelled.append(True)
    runtime.debounce_started = hass.loop.time()
    await runtime.async_set_eco_intensity("workday")
    assert runtime.eco_intensity == "workday"
    assert runtime.configuration_generation == 3
    assert cancelled == [True, True]
    broker.invalidate.assert_called_once_with("registry-1")
    unchanged_generation = runtime.configuration_generation
    await runtime.async_set_eco_intensity("workday")
    assert runtime.configuration_generation == unchanged_generation
    await hass.async_block_till_done()
    assert len(persisted) == 4
    assert persisted[0]["strategy"] == "comfort"
    assert persisted[-1]["strategy"] == "comfort"
    assert persisted[-1]["configuration_fingerprint"] == runtime._configuration_fingerprint()

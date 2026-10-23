"""Optional central-demand permission without new latches or control paths."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.athb.calculation import calculate_runtime_snapshot
from custom_components.athb.config_flow import AthbOptionsFlow
from custom_components.athb.config_schema import validate_options
from custom_components.athb.const import (
    CONF_DEMAND_ASSIST_ENTITY as SOURCE,
)
from custom_components.athb.const import (
    CONF_DEMAND_ASSIST_THRESHOLD as THRESHOLD,
)
from custom_components.athb.const import (
    DOMAIN,
)
from custom_components.athb.core.climate import (
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    normalize_scalar_target,
)
from custom_components.athb.core.contracts import ActuationDirection, RootSuccess
from custom_components.athb.runtime import ZoneRuntime
from tests.home_assistant.test_runtime import _runtime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios


@pytest.mark.parametrize(
    "threshold", [None, True, False, "0.5", 0, -1, 0.15, float("nan"), float("inf")]
)
def test_invalid_assist_threshold_is_rejected(threshold: Any) -> None:
    assert validate_options({SOURCE: "sensor.central_demand", THRESHOLD: threshold}) == {
        THRESHOLD: "invalid_assist_threshold"
    }


def test_source_requires_explicit_threshold_and_numeric_domain() -> None:
    assert validate_options({SOURCE: "sensor.central_demand"}) == {THRESHOLD: "required"}
    assert validate_options({SOURCE: "binary_sensor.central_demand", THRESHOLD: 0.5}) == {
        SOURCE: "invalid_option"
    }
    for source in ("sensor.demand", "number.demand", "input_number.demand"):
        assert validate_options({SOURCE: source, THRESHOLD: 0.5}) == {}
    assert validate_options({}) == {}
    assert validate_options({THRESHOLD: 0.5}) == {SOURCE: "required"}
    assert validate_options({SOURCE: "sensor.athb_target", THRESHOLD: 0.5}) == {
        SOURCE: "invalid_option"
    }


@pytest.mark.parametrize("threshold", [None, 0, -1, True, "0.5", float("nan"), float("inf")])
def test_runtime_fails_closed_for_invalid_assist_configuration(
    hass: HomeAssistant, threshold: Any
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update({SOURCE: "sensor.central_demand", THRESHOLD: threshold})
    hass.states.async_set("sensor.central_demand", "10")
    assert runtime._central_demand_assist()["eligible"] is False


async def test_assist_form_requires_threshold_and_can_clear_existing_pair(
    hass: HomeAssistant,
) -> None:
    flow = AthbOptionsFlow(MockConfigEntry(domain=DOMAIN, data={"targets": []}))
    flow.hass = hass
    flow._pending_options = {SOURCE: "sensor.old_demand", THRESHOLD: 10.0}
    flow._advanced = True
    form = await flow.async_step_demand_hysteresis()
    assert (
        next(k for k in form["data_schema"].schema if str(k) == THRESHOLD).default is vol.UNDEFINED
    )
    invalid = await flow.async_step_demand_hysteresis({SOURCE: "sensor.new_demand"})
    assert invalid["errors"] == {THRESHOLD: "required"}
    valid = await flow.async_step_demand_hysteresis({SOURCE: "sensor.new_demand", THRESHOLD: 0.5})
    assert valid["step_id"] == "init"
    assert flow._pending_options[THRESHOLD] == 0.5
    cleared = await flow.async_step_demand_hysteresis({})
    assert cleared["step_id"] == "init"
    assert SOURCE not in flow._pending_options
    assert THRESHOLD not in flow._pending_options


async def test_assist_form_preserves_both_error_messages(hass: HomeAssistant) -> None:
    flow = AthbOptionsFlow(MockConfigEntry(domain=DOMAIN, data={"targets": []}))
    flow.hass = hass
    flow._pending_options = {}
    result = await flow.async_step_demand_hysteresis(
        {
            SOURCE: "sensor.central_demand",
            THRESHOLD: 0,
            "heating_demand_activation_delta_c": 0.5,
            "heating_demand_deactivation_delta_c": 0.5,
        }
    )
    assert result["errors"] == {
        THRESHOLD: "invalid_assist_threshold",
        "base": "invalid_demand_hysteresis",
    }


async def test_native_athb_source_is_rejected_even_with_custom_entity_name(
    hass: HomeAssistant,
) -> None:
    own = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "own", suggested_object_id="own_target"
    )
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update({SOURCE: own.entity_id, THRESHOLD: 0.5})
    hass.states.async_set(own.entity_id, "20")
    assert runtime._central_demand_assist()["eligible"] is False
    flow = AthbOptionsFlow(MockConfigEntry(domain=DOMAIN, data={"targets": []}))
    flow.hass = hass
    flow._pending_options = {}
    result = await flow.async_step_demand_hysteresis({SOURCE: own.entity_id, THRESHOLD: 0.5})
    assert result["errors"] == {SOURCE: "invalid_option"}


@pytest.mark.parametrize("domain", ["sensor", "number", "input_number"])
def test_source_freshness_uses_reports_not_value_changes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    entity_id = f"{domain}.central_demand"
    runtime.entry.options.update({SOURCE: entity_id, THRESHOLD: 0.5})
    hass.states.async_set(entity_id, "0.5")
    state = hass.states.get(entity_id)
    now = dt_util.utcnow()
    monkeypatch.setattr(state, "last_reported", now - timedelta(minutes=31))
    assert runtime._central_demand_assist()["eligible"] is False
    monkeypatch.setattr(state, "last_reported", now)
    monkeypatch.setattr(state, "last_updated", now - timedelta(days=1))
    assert runtime._central_demand_assist()["eligible"] is True


def test_unchanged_permission_report_only_recalculates_after_expiry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update({SOURCE: "sensor.central_demand", THRESHOLD: 0.5})
    assert "sensor.central_demand" in runtime._reported_source_entity_ids()
    scheduler = MagicMock()
    monkeypatch.setattr(ZoneRuntime, "schedule_environmental_snapshot", scheduler)
    now = dt_util.utcnow()
    for minutes in (1, 31):
        runtime._handle_source_report_event(
            cast(
                Any,
                SimpleNamespace(
                    data={
                        "entity_id": "sensor.central_demand",
                        "last_reported": now,
                        "old_last_reported": now - timedelta(minutes=minutes),
                        "new_state": State("sensor.central_demand", "0.5"),
                    }
                ),
            )
        )
    scheduler.assert_called_once_with()
    runtime._handle_source_report_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "entity_id": "sensor.central_demand",
                    "last_reported": now,
                    "new_state": State("sensor.central_demand", "0.5"),
                }
            ),
        )
    )
    scheduler.assert_called_once_with()


async def test_registry_rename_preserves_the_assist_pair(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"targets": []}, options={SOURCE: "sensor.old_demand", THRESHOLD: 0.5}
    )
    entry.add_to_hass(hass)
    runtime = ZoneRuntime(hass, cast(Any, entry), "rename-assist", "balanced", "off", False)
    runtime.repair_manager = cast(Any, SimpleNamespace(update=lambda *_args: None))
    runtime._entity_registry_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "action": "update",
                    "old_entity_id": "sensor.old_demand",
                    "entity_id": "sensor.new_demand",
                }
            ),
        )
    )
    assert entry.options[SOURCE] == "sensor.new_demand"
    assert entry.options[THRESHOLD] == 0.5
    assert "sensor.new_demand" in runtime._tracked_entity_ids()
    assert "sensor.old_demand" not in runtime._tracked_entity_ids()
    await runtime.async_unload()


@pytest.mark.parametrize(
    "source_value", [None, "unknown", "unavailable", "nan", "inf", "on", "0", "0.499999", "-1"]
)
def test_invalid_or_below_threshold_source_cannot_grant_permission(
    hass: HomeAssistant, source_value: str | None
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update({SOURCE: "sensor.central_demand", THRESHOLD: 0.5})
    if source_value is not None:
        hass.states.async_set("sensor.central_demand", source_value)
    assert runtime._central_demand_assist()["eligible"] is False


@pytest.mark.parametrize(("value", "threshold"), [("0.5", 0.5), ("0.500001", 0.5), ("10", 10.0)])
def test_source_threshold_is_inclusive_without_rounding(
    hass: HomeAssistant, value: str, threshold: float
) -> None:
    runtime = _runtime()
    runtime.hass = hass
    runtime.entry.options.update({SOURCE: "sensor.central_demand", THRESHOLD: threshold})
    hass.states.async_set("sensor.central_demand", value)
    assert runtime._central_demand_assist()["eligible"] is True


@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("occupied", [False, None, True])
def test_assist_only_starts_occupied_positive_executable_heat_and_keeps_normal_stop(
    hass: HomeAssistant, ranged: bool, occupied: bool | None
) -> None:
    scenario = next(
        s for s in load_scenarios() if s["scenario_id"] == ("VI-018" if ranged else "VI-001")
    )
    calculation = calculate_runtime_snapshot(_captured(scenario))
    target = calculation.targets[0]
    assert target.result is not None
    assert target.result.roots is not None
    heating = normalize_scalar_target(
        requested_room_c=20.0,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=target.capability,
        options=GridOptions(16.0, 30.0),
    )
    assert isinstance(heating, NormalizedScalarTarget)
    original = target.result.normalized
    normalized = replace(original, heating=heating) if ranged else heating
    # Isolate the optional permission from the independent comfort protection.
    lower = target.result.roots.lower_comfort
    assert isinstance(lower, RootSuccess)
    result = replace(
        target.result,
        normalized=normalized,
        roots=replace(
            target.result.roots, lower_comfort=replace(lower, mapped_room_temperature_c=18.0)
        ),
    )
    calculation = replace(
        calculation,
        primary_value_c=19.7,
        effective_occupancy_on=occupied,
        targets=(replace(target, result=result),),
    )
    runtime = _runtime(target_identity=target.registry_identity)
    runtime.hass = hass
    runtime._apply_heating_demand_hysteresis(calculation)
    assert "central_demand_assist" not in runtime.heating_demand_details[target.target_uuid]
    runtime.entry.options.update(
        {
            SOURCE: "sensor.central_demand",
            THRESHOLD: 0.5,
            "minimum_control_temperature": 16.0,
            "heating_demand_deactivation_delta_c": 0.0,
        }
    )
    hass.states.async_set("sensor.central_demand", "0.49")
    runtime._apply_heating_demand_hysteresis(calculation)
    assert runtime.heating_demand_active[target.registry_identity] is False
    hass.states.async_set("sensor.central_demand", "0.5")
    transformed, _ = runtime._apply_heating_demand_hysteresis(calculation)
    assert runtime.heating_demand_active[target.registry_identity] is (occupied is True)
    if occupied:
        assert transformed.targets[0].result.normalized == normalized
        assert (
            runtime.heating_demand_details[target.target_uuid]["reason"] == "central_demand_assist"
        )
        hass.states.async_set("sensor.central_demand", "unavailable")
        runtime._apply_heating_demand_hysteresis(calculation)
        assert runtime.heating_demand_active[target.registry_identity] is True
        assert (
            runtime.heating_demand_details[target.target_uuid]["reason"] == "active_hysteresis_hold"
        )
    stopped, _ = runtime._apply_heating_demand_hysteresis(
        replace(calculation, primary_value_c=20.0)
    )
    assert runtime.heating_demand_active[target.registry_identity] is False
    if ranged:
        assert isinstance(original, NormalizedRangeTarget)
        assert stopped.targets[0].result.normalized.cooling == original.cooling
    runtime.heating_demand_active.clear()
    for guarded in (
        replace(calculation, primary_temperature_stale=True),
        replace(calculation, targets=(replace(target, suppression_reason="manual_override"),)),
    ):
        runtime._apply_heating_demand_hysteresis(guarded)
        assert not runtime.heating_demand_active


def test_assist_source_changes_schedule_but_unchanged_attributes_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.entry.options[SOURCE] = "sensor.central_demand"
    scheduler = MagicMock()
    monkeypatch.setattr(ZoneRuntime, "schedule_environmental_snapshot", scheduler)
    assert "sensor.central_demand" in runtime._tracked_entity_ids()
    assert "sensor.central_demand" not in runtime._measurement_source_entity_ids()
    for old, new in (("0.49", "0.5"), ("0.5", "unavailable"), ("unavailable", "0.5")):
        runtime._handle_state_event(
            cast(
                Any,
                SimpleNamespace(
                    data={
                        "entity_id": "sensor.central_demand",
                        "old_state": State("sensor.central_demand", old),
                        "new_state": State("sensor.central_demand", new),
                    }
                ),
            )
        )
    assert scheduler.call_count == 3
    runtime._handle_state_event(
        cast(
            Any,
            SimpleNamespace(
                data={
                    "entity_id": "sensor.central_demand",
                    "old_state": State("sensor.central_demand", "0.5"),
                    "new_state": State(
                        "sensor.central_demand", "0.5", {"unit_of_measurement": "°C"}
                    ),
                }
            ),
        )
    )
    assert scheduler.call_count == 3

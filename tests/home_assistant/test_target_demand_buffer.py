"""Independent destination buffers and truthful room goals in a mixed heating chain."""

from dataclasses import replace

import pytest

from custom_components.athb.calculation import calculate_runtime_snapshot
from custom_components.athb.config_flow import AthbOptionsFlow
from custom_components.athb.config_schema import target_demand_settings, validate_options
from custom_components.athb.sensor import ZoneTargetSensor
from tests.home_assistant.test_config_flow import _settings_entry
from tests.home_assistant.test_runtime import _runtime
from tests.virtual_installations.runner import _captured
from tests.virtual_installations.schema import load_scenarios


@pytest.mark.parametrize(("start", "stop"), [(0.5, 0.1), (0.7, 0.0)])
def test_legacy_destinations_keep_zone_thresholds(start, stop):
    assert target_demand_settings(
        {
            "heating_demand_activation_delta_c": start,
            "heating_demand_deactivation_delta_c": stop,
        },
        "one",
    ) == (True, start, stop)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("demand_buffer_one", 0),
        ("demand_buffer_one", "false"),
        ("demand_start_one", 0),
        ("demand_start_one", True),
        ("demand_stop_one", -0.1),
        ("demand_start_one", float("nan")),
    ],
)
def test_invalid_destination_settings_rejected(key, value):
    assert validate_options({key: value}) == {key: "invalid_option"}


def test_independent_threshold_pairs_validate_even_when_disabled():
    assert validate_options(
        {"demand_buffer_one": False, "demand_start_one": 0.2, "demand_stop_one": 0.2}
    ) == {"demand_stop_one": "invalid_option"}
    assert (
        validate_options(
            {
                "demand_start_one": 0.2,
                "demand_stop_one": 0.0,
                "demand_start_two": 0.8,
                "demand_stop_two": 0.2,
            }
        )
        == {}
    )


@pytest.mark.parametrize(("stale", "suppressed"), [(False, False), (True, False), (False, True)])
def test_only_buffered_destination_changes_and_other_guards_survive(stale, suppressed):
    result = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = result.targets[0]
    assert target.result is not None
    requested = target.result.normalized
    assert requested is not None
    valve = replace(
        target,
        target_uuid="valve",
        registry_identity="valve-id",
        suppression_reason="manual_override" if suppressed else None,
    )
    electric = replace(target, target_uuid="electric", registry_identity="electric-id")
    result = replace(
        result,
        targets=(valve, electric),
        primary_value_c=requested.normalized_room_c - 0.3,
        primary_temperature_stale=stale,
    )
    runtime = _runtime()
    runtime.entry.options.update(
        {"demand_buffer_valve": False, "demand_start_electric": 0.5, "demand_stop_electric": 0.1}
    )
    before_latch = dict(runtime.heating_demand_active)
    applied, transitions = runtime._apply_heating_demand_hysteresis(result)
    assert applied.targets[0] is valve
    assert applied.targets[0].suppression_reason == valve.suppression_reason
    assert applied.primary_temperature_stale == stale
    assert runtime.heating_demand_active.get("valve-id") == before_latch.get("valve-id")
    assert "valve-id" not in transitions
    assert runtime.control_enabled is False
    expected = (
        "manual_override"
        if suppressed
        else "primary_temperature_not_fresh"
        if stale
        else "buffer_disabled"
    )
    assert runtime.heating_demand_details["valve"]["reason"] == expected
    assert runtime.heating_demand_details["valve"]["state"] == (
        "blocked" if stale or suppressed else "bypassed"
    )
    assert runtime.heating_demand_details["valve"]["buffer_enabled"] is False
    if not stale:
        assert applied.targets[1].result.normalized.normalized_room_c <= result.primary_value_c
    else:
        assert applied.targets[1] is electric
    assert runtime.heating_demand_details["electric"]["buffer_enabled"] is True


def test_per_destination_start_threshold_and_latch_are_independent():
    result = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = result.targets[0]
    assert target.result is not None
    requested = target.result.normalized
    assert requested is not None
    first = replace(target, target_uuid="one", registry_identity="first-id")
    second = replace(target, target_uuid="two", registry_identity="second-id")
    runtime = _runtime()
    runtime.entry.options.update(
        {
            "demand_start_one": 0.2,
            "demand_stop_one": 0.0,
            "demand_start_two": 0.8,
            "demand_stop_two": 0.1,
        }
    )
    applied, transitions = runtime._apply_heating_demand_hysteresis(
        replace(result, targets=(first, second), primary_value_c=requested.requested_room_c - 0.5)
    )
    assert applied.targets[0].result.normalized is requested
    assert transitions["first-id"] is None
    assert runtime.heating_demand_details["one"]["active"] is True
    assert runtime.heating_demand_details["two"]["active"] is False


async def test_destination_settings_remain_drafts_and_preserve_other_destination(hass):
    entry = _settings_entry(
        hass,
        options={
            "comfort_strategy": "balanced",
            "demand_buffer_other": True,
            "demand_start_other": 0.8,
        },
    )
    flow = AthbOptionsFlow(entry)
    before = dict(entry.options)
    result = await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.0,
            "target_step_override": 0.1,
            "heating_demand_buffer": False,
            "heating_demand_start": 0.5,
            "heating_demand_stop": 0.0,
        }
    )
    assert result.get("errors", {}) == {}
    assert flow._pending_options["demand_buffer_target-stable"] is False
    assert flow._pending_options["demand_start_other"] == 0.8
    assert dict(entry.options) == before
    flow._calibration_index = 0
    draft = dict(flow._pending_options)
    invalid = await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.0,
            "heating_demand_start": 0.2,
            "heating_demand_stop": 0.2,
        }
    )
    assert invalid["errors"] == {"heating_demand_stop": "invalid_option"}
    assert flow._pending_options == draft


def test_live_room_goal_matches_policy_while_buffered_actuators_follow_room(hass):
    result = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = result.targets[0]
    assert target.result is not None
    assert target.result.normalized is not None
    runtime = _runtime()
    sensor = ZoneTargetSensor(runtime, "current")
    sensor._restored_native_value = 18.0
    runtime.hass = hass
    reference = target.result.policy.heating_c
    for shortfall in (0.3, 0.2, 0.1):
        applied, _ = runtime._apply_heating_demand_hysteresis(
            replace(result, primary_value_c=target.result.normalized.normalized_room_c - shortfall)
        )
        runtime.publish(runtime._observable_values(applied))
        assert sensor.native_value == reference
        assert sensor.extra_state_attributes["data_quality"] != "restored_stale"


async def test_unchanged_destination_keeps_inheritance_and_custom_pair_can_be_cleared(hass):
    flow = AthbOptionsFlow(_settings_entry(hass))
    await flow.async_step_target_calibration({"calibration_offset_c": 0.0})
    assert not any(key.startswith("demand_") for key in flow._pending_options)
    flow._pending_options["heating_demand_activation_delta_c"] = 0.8
    assert target_demand_settings(flow._pending_options, "target-stable") == (True, 0.8, 0.1)
    flow._calibration_index = 0
    await flow.async_step_target_calibration(
        {"calibration_offset_c": 0.0, "heating_demand_start": 0.2, "heating_demand_stop": 0.0}
    )
    assert flow._pending_options["demand_start_target-stable"] == 0.2
    assert flow._pending_options["demand_stop_target-stable"] == 0.0
    flow._calibration_index = 0
    await flow.async_step_target_calibration(
        {"calibration_offset_c": 0.0, "heating_demand_start": 0.8, "heating_demand_stop": 0.1}
    )
    assert not any(key.startswith("demand_") for key in flow._pending_options)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"demand_buffer_one": "false", "demand_start_one": "bad"}, (True, 0.5, 0.1)),
        ({"demand_buffer_one": 0, "demand_stop_one": None}, (True, 0.5, 0.1)),
        ({"demand_buffer_one": False, "demand_start_one": float("nan")}, (False, 0.5, 0.1)),
        ({"heating_demand_activation_delta_c": 0.2, "demand_stop_one": 0.3}, (True, 0.2, 0.1)),
        (
            {"heating_demand_activation_delta_c": 0.1, "heating_demand_deactivation_delta_c": 0.5},
            (True, 0.1, 0.0),
        ),
    ],
)
def test_malformed_partial_settings_never_invert_or_crash_runtime(options, expected):
    assert target_demand_settings(options, "one") == expected


@pytest.mark.parametrize("boost", ["adaptive", "rapid"])
def test_disabled_buffer_preserves_boost_goal(boost):
    from tests.home_assistant.test_manual_heating import _snapshot

    result = calculate_runtime_snapshot(_snapshot(boost=boost))
    target = result.targets[0]
    runtime = _runtime()
    runtime.entry.options[f"demand_buffer_{target.target_uuid}"] = False
    applied, transitions = runtime._apply_heating_demand_hysteresis(result)
    assert applied.targets[0] is target
    assert transitions == {}
    assert runtime.heating_demand_details[target.target_uuid]["reason"] == "buffer_disabled"


def test_reenabled_buffer_resumes_latch_then_requires_start_after_normal_stop():
    result = calculate_runtime_snapshot(_captured(load_scenarios()[0]))
    target = result.targets[0]
    desired = target.result.normalized
    runtime = _runtime()
    runtime.heating_demand_active[target.registry_identity] = True
    runtime.entry.options[f"demand_buffer_{target.target_uuid}"] = False
    runtime._apply_heating_demand_hysteresis(result)
    assert runtime.heating_demand_active[target.registry_identity] is True
    runtime.entry.options[f"demand_buffer_{target.target_uuid}"] = True
    runtime._apply_heating_demand_hysteresis(
        replace(result, primary_value_c=desired.normalized_room_c)
    )
    assert runtime.heating_demand_active[target.registry_identity] is False
    runtime._apply_heating_demand_hysteresis(
        replace(result, primary_value_c=desired.normalized_room_c - 0.1)
    )
    assert runtime.heating_demand_active[target.registry_identity] is False


@pytest.mark.parametrize("scenario", ["occupied", "unoccupied"])
def test_other_room_sensors_keep_their_existing_restore_contract(scenario):
    runtime = _runtime()
    runtime.publish({"data_quality": "current", "target_scenarios": {"one": {}}})
    sensor = ZoneTargetSensor(runtime, scenario)
    sensor._restored_native_value = 18.5
    assert sensor.native_value == 18.5


def test_cooling_room_goal_is_unrounded_policy_value_and_stale_quality_is_retained():
    runtime = _runtime()
    runtime.publish(
        {
            "data_quality": "stale",
            "target_scenarios": {
                "one": {
                    "direction": "cooling_only",
                    "current": {"room": {"temperature": 24.236}, "actuator": {"temperature": 24.5}},
                }
            },
            "effective_targets": {"one": {"temperature": 24.5}},
        }
    )
    sensor = ZoneTargetSensor(runtime, "current")
    assert sensor.native_value == 24.236
    # No per-device context required to establish the scalar value.
    assert runtime.values["data_quality"] == "stale"


async def test_near_equal_thresholds_inherit_and_rejected_fields_round_trip(hass):
    flow = AthbOptionsFlow(
        _settings_entry(hass, options={"heating_demand_activation_delta_c": 0.3})
    )
    await flow.async_step_target_calibration(
        {"calibration_offset_c": 0.0, "heating_demand_start": 0.1 + 0.2, "heating_demand_stop": 0.1}
    )
    assert not any(key.startswith("demand_") for key in flow._pending_options)
    flow._calibration_index = 0
    error = await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.5,
            "target_step_override": 0.1,
            "heating_demand_start": 0.2,
            "heating_demand_stop": 0.2,
        }
    )
    fields = {key.schema: key.default() for key in error["data_schema"].schema}
    assert fields["calibration_offset_c"] == 0.5
    assert fields["target_step_override"] == 0.1
    assert fields["heating_demand_stop"] == 0.2

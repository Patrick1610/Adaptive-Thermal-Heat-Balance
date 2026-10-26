"""Per-destination precision without changing default device grids or target identities."""

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.athb.calculation import calculate_runtime_snapshot
from custom_components.athb.config_flow import AthbOptionsFlow
from custom_components.athb.config_schema import validate_options
from custom_components.athb.core.climate import (
    GridOptions,
    normalize_scalar_target,
    resolve_capability,
)
from custom_components.athb.core.contracts import ActuationDirection
from tests.home_assistant.test_config_flow import _settings_entry
from tests.home_assistant.test_manual_heating import _snapshot
from tests.home_assistant.test_runtime import _start_delivery_runtime


@pytest.mark.parametrize("value", [0, -0.1, True, "0.1", float("nan"), float("inf"), 0.001, 6])
def test_invalid_stored_step_is_rejected(value):
    assert validate_options({"target_step_target-1": value}) == {
        "target_step_target-1": "invalid_option"
    }


@pytest.mark.parametrize("value", [0.01, 0.1, 0.5, 1, 5])
def test_valid_stored_step(value):
    assert validate_options({"target_step_target-1": value}) == {}


@pytest.mark.parametrize(("override", "expected"), [(None, 0.5), (0.1, 0.1)])
@pytest.mark.parametrize("fallback", [False, True])
def test_normal_and_stale_rh_manual_calculations_use_destination_override(
    override, expected, fallback
):
    base = _snapshot(preheat=True)
    base = replace(
        base,
        options={
            **base.options,
            **({} if override is None else {"target_step_target-1": override}),
        },
    )
    if not fallback:
        base = replace(base, declared_relative_humidity_pct=50.0)
    result = calculate_runtime_snapshot(base)
    assert result.manual_heating_fallback is fallback
    assert result.targets[0].result.normalized.grid.step_ha == expected


def test_consecutive_tenths_keep_limits_and_other_device_default():
    capability = _snapshot().targets[0].capability
    for requested in (19.6, 19.7, 19.8):
        exact = normalize_scalar_target(
            requested_room_c=requested,
            direction=ActuationDirection.HEATING_ONLY,
            snapshot=capability,
            options=GridOptions(15, 26, step_override=0.1),
        )
        assert exact.normalized_ha == pytest.approx(requested)
    bounded = normalize_scalar_target(
        requested_room_c=35,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=capability,
        options=GridOptions(15, 26, step_override=0.1),
    )
    assert bounded.normalized_ha == 26
    automatic = normalize_scalar_target(
        requested_room_c=19.6,
        direction=ActuationDirection.HEATING_ONLY,
        snapshot=capability,
        options=GridOptions(15, 26),
    )
    assert automatic.grid.step_ha == 0.5


async def test_step_settings_are_drafts_and_zero_restores_automatic(hass: HomeAssistant):
    entry = _settings_entry(hass)
    flow = AthbOptionsFlow(entry)
    target = flow._wizard_targets[0]
    key = f"target_step_{target['target_uuid']}"
    original = dict(entry.options)
    await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.0,
            "target_step_override": 0.1,
        }
    )
    assert flow._pending_options[key] == 0.1
    assert dict(entry.options) == original
    flow._calibration_index = 0
    await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.0,
            "target_step_override": 0.0,
        }
    )
    assert key not in flow._pending_options
    assert dict(entry.options) == original


async def test_runtime_sends_and_acknowledges_finer_than_advertised_grid(hass: HomeAssistant):
    runtime, calls = await _start_delivery_runtime(hass, outcome="ack", control_enabled=False)
    hass.config_entries.async_update_entry(
        runtime.entry,
        options={
            **runtime.entry.options,
            "target_step_target-1": 0.1,
            "fallback_heating_c": 19.6,
        },
    )
    await runtime.async_set_control_enabled(True)
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()
    assert calls
    assert any(round(float(c.data["temperature"]) * 10) % 5 for c in calls)
    assert (
        hass.states.get("climate.target").attributes["temperature"] == calls[-1].data["temperature"]
    )
    assert runtime.values["command_outcomes"]["target-1"] == "own_context_match"
    await runtime.async_unload()


@pytest.mark.parametrize("value", [True, -1, 0.001, float("nan"), 6])
async def test_calibration_page_rejects_invalid_override_without_altering_draft(hass, value):
    flow = AthbOptionsFlow(_settings_entry(hass))
    before = dict(flow._pending_options)
    result = await flow.async_step_target_calibration(
        {
            "calibration_offset_c": 0.5,
            "target_step_override": value,
        }
    )
    assert result["errors"] == {"target_step_override": "invalid_option"}
    assert flow._pending_options == before


@pytest.mark.parametrize("boost", ["adaptive", "rapid"])
def test_boost_manual_fallback_uses_override(boost):
    base = _snapshot(boost=boost)
    result = calculate_runtime_snapshot(
        replace(
            base,
            options={
                **base.options,
                "target_step_target-1": 0.1,
            },
        )
    )
    assert result.manual_heating_fallback
    assert result.targets[0].result.normalized.grid.step_ha == 0.1


async def test_idle_and_stale_safety_targets_keep_fine_grid(hass):
    runtime, _calls = await _start_delivery_runtime(hass, outcome="ack", control_enabled=False)
    try:
        hass.config_entries.async_update_entry(
            runtime.entry,
            options={
                **runtime.entry.options,
                "target_step_target-1": 0.1,
                "fallback_heating_c": 19.6,
            },
        )
        base = _snapshot(preheat=True)
        target = calculate_runtime_snapshot(base).targets[0]
        capability = replace(target.capability, scalar_target_ha=23)
        safety = runtime._stale_safety_target(
            "target-1",
            capability,
            resolve_capability(capability),
            20,
        )
        assert safety.grid.step_ha == 0.1
        assert safety.normalized_ha == pytest.approx(19.6)
        idle = runtime._idle_heating_target(
            target,
            target.result.normalized,
            current_temperature_c=19.67,
        )
        assert idle.grid.step_ha == 0.1
        assert idle.normalized_ha == pytest.approx(19.6)
    finally:
        await runtime.async_unload()

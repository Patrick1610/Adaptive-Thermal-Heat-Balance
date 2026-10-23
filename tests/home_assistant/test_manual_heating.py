"""Bounded manual heating never invents missing RH or reuses an idle goal."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time
from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.athb import runtime as runtime_module
from custom_components.athb.adapters.storage import (
    ZoneCommandPersistence,
    load_control_state,
    serialize_control_state,
)
from custom_components.athb.calculation import (
    CachedHeatingReference,
    CapturedZoneSnapshot,
    calculate_runtime_snapshot,
    result_values,
)
from custom_components.athb.core.climate import NormalizedScalarTarget
from custom_components.athb.core.contracts import BoostMode
from custom_components.athb.core.history import HistoryQuality, RunningMeanResult
from custom_components.athb.core.ownership import Ownership
from custom_components.athb.core.policy import PolicyFailure, build_manual_heating_fallback
from tests.control.test_storage import MemoryBackend
from tests.home_assistant.test_calculation import NOW, _state, _target
from tests.home_assistant.test_runtime import _runtime, _start_delivery_runtime


def _snapshot(*, boost="off", preheat=False, cache=True, **changes):
    return replace(
        CapturedZoneSnapshot(
            NOW,
            _state("sensor.room", "18", "°C"),
            None,
            None,
            _state("sensor.outdoor", "5", "°C"),
            None,
            5.0,
            "complete_history",
            "balanced",
            "comfort",
            {"minimum_control_temperature": 15.0, "maximum_control_temperature": 26.0},
            (_target(),),
            boost_mode=boost,
            preheat_active=preheat,
            effective_occupancy_on=preheat,
            cached_heating_references=(
                CachedHeatingReference(
                    "target-1", "registry-1", 21.0, 23.0, NOW - timedelta(hours=2), 17.0
                ),
            )
            if cache
            else (),
        ),
        **changes,
    )


@pytest.mark.parametrize(
    ("boost", "preheat", "expected"),
    [("rapid", False, 26), ("adaptive", False, 22), ("off", True, 21)],
)
@pytest.mark.parametrize("rh", [None, "unavailable", "nan", "999", "stale"])
def test_manual_heating_uses_occupied_reference_not_idle_or_fake_rh(boost, preheat, expected, rh):
    humidity = None if rh is None else _state("sensor.rh", "50" if rh == "stale" else rh, "%")
    if rh == "stale":
        humidity = replace(humidity, observed_at=NOW - timedelta(hours=2))
    result = calculate_runtime_snapshot(
        _snapshot(boost=boost, preheat=preheat, relative_humidity=humidity)
    )
    target = result.targets[0]
    assert result.manual_heating_fallback
    assert result.hold_condition in {"primary_rh_invalid", "primary_rh_stale"}
    assert result.relative_humidity_pct is None
    assert target.result is not None
    assert target.result.current is None
    assert target.result.roots is None
    assert target.result.evaluation_count == 0
    assert isinstance(target.result.normalized, NormalizedScalarTarget)
    assert target.result.normalized.normalized_actuator_c == expected
    assert target.result.policy.occupied_heating_c == 21
    values = result_values(result)
    detail = values["effective_target_details"]["target-1"]
    assert detail["mode"] == "manual_heating_fallback"
    assert detail["reference"] == "last_valid_occupied"
    assert detail["reference_at"] == (NOW - timedelta(hours=2)).isoformat()
    assert values["input_status"] == result.hold_condition


@pytest.mark.parametrize(("boost", "preheat"), [("off", False), ("adaptive", False), ("off", True)])
def test_missing_cache_never_invents_an_active_comfort_goal(boost, preheat):
    result = calculate_runtime_snapshot(_snapshot(boost=boost, preheat=preheat, cache=False))
    assert result.targets[0].result is None
    assert result.targets[0].suppression_reason is not None


def test_rapid_without_a_prior_calculation_uses_only_the_configured_limit():
    result = calculate_runtime_snapshot(_snapshot(boost="rapid", cache=False))
    target = result.targets[0]
    assert target.result.normalized.normalized_actuator_c == 26
    assert target.result.policy.occupied_heating_c is None
    assert target.result.policy.governing_heating == "configured_heating_limit"


@pytest.mark.parametrize("raw", ["unavailable", "nan", "999"])
def test_manual_rh_fallback_cannot_bypass_invalid_primary(raw):
    result = calculate_runtime_snapshot(
        _snapshot(boost="rapid", primary=_state("sensor.room", raw, "°C"))
    )
    assert not result.manual_heating_fallback
    assert result.targets[0].result is None


def test_manual_rh_fallback_cannot_bypass_stale_primary_or_invalid_speed():
    stale = replace(_state("sensor.room", "18", "°C"), observed_at=NOW - timedelta(hours=2))
    for snapshot in [
        _snapshot(boost="rapid", primary=stale),
        _snapshot(boost="rapid", options={"air_speed_mode": "measured"}),
        _snapshot(boost="rapid", declared_relative_humidity_pct=999),
    ]:
        result = calculate_runtime_snapshot(snapshot)
        assert not result.manual_heating_fallback
        assert result.targets[0].result is None


@pytest.mark.parametrize(("mode", "features"), [("cool", 1), ("heat_cool", 2), ("auto", 1)])
def test_manual_heating_keeps_rh_required_for_cooling_and_range(mode, features):
    result = calculate_runtime_snapshot(
        _snapshot(boost="rapid", targets=(_target(mode=mode, features=features),))
    )
    assert not result.manual_heating_fallback
    assert result.targets[0].result is None


@pytest.mark.parametrize("mode", ["off", "unavailable"])
def test_manual_heating_keeps_capability_guards(mode):
    result = calculate_runtime_snapshot(_snapshot(boost="rapid", targets=(_target(mode=mode),)))
    assert result.targets[0].result is None
    assert result.targets[0].suppression_reason is not None


def test_manual_heating_reapplies_current_user_device_calibration_and_grid_bounds():
    target = _target()
    target = replace(target, capability=replace(target.capability, max_temp_ha=24.0))
    result = calculate_runtime_snapshot(
        _snapshot(
            boost="rapid",
            targets=(target,),
            options={
                "minimum_control_temperature": 15,
                "maximum_control_temperature": 23.5,
                "calibration_target-1": 0.7,
            },
        )
    )
    n = result.targets[0].result.normalized
    assert n.normalized_actuator_c == 23.5
    assert n.normalized_room_c == pytest.approx(22.8)


def test_rapid_holds_cached_boost_goal_once_reached_and_does_not_stack_boost():
    for _repeated in range(3):
        result = calculate_runtime_snapshot(
            _snapshot(boost="rapid", primary=_state("sensor.room", "22.1", "°C"))
        )
        p = result.targets[0].result.policy
        assert p.boost_phase == "hold"
        assert p.rapid_boost_reached
        assert p.heating_c == 22
        assert p.occupied_heating_c == 21


def test_valid_rh_recovers_normal_calculation_and_leaves_the_manual_cache_unchanged():
    result = calculate_runtime_snapshot(
        _snapshot(boost="rapid", relative_humidity=_state("sensor.rh", "50", "%"))
    )
    assert not result.manual_heating_fallback
    assert result.targets[0].result.current is not None
    assert result.hold_condition is None


@pytest.mark.parametrize(("profile", "expected"), [("eco", 17), ("comfort", 18)])
def test_expired_manual_heating_returns_downwards_to_nonboost_policy(profile, expected):
    result = calculate_runtime_snapshot(_snapshot(withdraw_manual_heating=True, profile=profile))
    assert result.targets[0].result.normalized.normalized_actuator_c == expected
    assert result.targets[0].result.policy.governing_heating == "manual_heating_return"


def test_expired_rapid_without_reference_returns_to_explicit_minimum():
    result = calculate_runtime_snapshot(_snapshot(withdraw_manual_heating=True, cache=False))
    assert result.targets[0].result.normalized.normalized_actuator_c == 16  # device minimum


def test_manual_return_never_raises_an_observed_goal_below_current_bounds():
    t = _target()
    t = replace(t, capability=replace(t.capability, scalar_target_ha=15))
    result = calculate_runtime_snapshot(_snapshot(withdraw_manual_heating=True, targets=(t,)))
    assert result.targets[0].result is None
    assert result.targets[0].suppression_reason == "manual_heating_return_would_increase"


@pytest.mark.parametrize(
    "cache_change",
    [
        {"registry_identity": "another"},
        {"observed_at": NOW + timedelta(seconds=1)},
        {"occupied_heating_c": float("nan")},
        {"occupied_heating_c": True},
    ],
)
def test_untrusted_cache_cannot_drive_preheat(cache_change):
    s = _snapshot(preheat=True)
    ref = replace(s.cached_heating_references[0], **cache_change)
    result = calculate_runtime_snapshot(replace(s, cached_heating_references=(ref,)))
    assert result.targets[0].result is None


@pytest.mark.parametrize(
    "values",
    [
        {"occupied_heating_c": float("inf")},
        {"neutral_c": float("nan")},
        {"boost_delta_c": -1},
        {"boost_delta_c": True},
    ],
)
def test_manual_policy_rejects_invalid_numbers(values):
    args = dict(
        occupied_heating_c=21.0,
        neutral_c=23.0,
        boost_mode=BoostMode.ADAPTIVE,
        current_air_temperature_c=18.0,
        maximum_heating_c=26.0,
        boost_delta_c=1.0,
        rapid_boost_reached=False,
    )
    result = build_manual_heating_fallback(**{**args, **values})
    assert isinstance(result, PolicyFailure)


async def _settle(hass, runtime):
    runtime.async_request_snapshot()
    await runtime.controller.async_wait_idle()
    await hass.async_block_till_done()


@pytest.fixture
def manual_clock():
    with freeze_time() as clock:
        yield clock


async def _live(hass, monkeypatch, manual_clock):
    monkeypatch.setattr(
        runtime_module.OutdoorHistoryCollector,
        "result",
        lambda *a, **k: RunningMeanResult(5.0, HistoryQuality.COMPLETE, 7, 1.0, None, ()),
    )
    runtime, calls = await _start_delivery_runtime(hass, outcome="ack", measured_rh=True)
    assert runtime.last_valid_values["target_scenarios"]["target-1"]["occupied"]["room"]
    # Let the regular command interval elapse without waiting on wall time.
    manual_clock.tick(timedelta(seconds=61))
    hass.states.async_set("sensor.humidity", "unavailable")
    await _settle(hass, runtime)
    calls.clear()
    return runtime, calls


@pytest.mark.parametrize("kind", ["rapid", "adaptive", "preheat"])
async def test_live_manual_heating_uses_cache_and_preserves_its_age(
    hass, monkeypatch, manual_clock, kind
):
    runtime, _calls = await _live(hass, monkeypatch, manual_clock)
    cache, observed_at = deepcopy(runtime.last_valid_values), runtime.last_valid_at
    try:
        if kind == "preheat":
            await runtime.async_set_preheat_active(True)
        else:
            await runtime.async_set_boost_mode(kind)
        await _settle(hass, runtime)
        assert runtime.values["manual_heating_fallback"]
        assert runtime.values["input_status"] == "primary_rh_invalid"
        assert (
            runtime.values["effective_target_details"]["target-1"]["reference_at"]
            == observed_at.isoformat()
        )
        assert (
            runtime.values["effective_target_details"]["target-1"]["mode"]
            == "manual_heating_fallback"
        )
        assert runtime.values["control_eligible"]
        assert runtime.manual_heating_fallback_pending
        assert runtime.persistence.state.manual_heating_fallback_pending
        assert runtime.last_valid_values == cache
        assert runtime.last_valid_at == observed_at
        actual = hass.states.get("climate.target").attributes["temperature"]
        if kind == "rapid":
            assert actual == 26
        elif kind == "adaptive":
            assert actual > 20
            assert actual < 26
        else:
            # Preheat selects comfort; it does not force demand above the
            # ordinary start threshold when the room is already warm enough.
            assert runtime.values["heating_demand_hysteresis"]["target-1"]["active"] is False
            assert actual <= 20
    finally:
        await runtime.async_unload()


async def test_live_rapid_expiry_with_missing_rh_withdraws_high_target(
    hass, monkeypatch, manual_clock
):
    runtime, calls = await _live(hass, monkeypatch, manual_clock)
    try:
        await runtime.async_set_boost_mode("rapid")
        await _settle(hass, runtime)
        assert calls[-1].data["temperature"] == 26
        expiry = runtime.values["boost_expiry"]
        manual_clock.move_to(expiry + timedelta(seconds=1))
        # A fresh temperature is still mandatory; do not turn stale-primary
        # withdrawal into an exception to HeatGuard.
        hass.states.async_set("sensor.room", "20.01", {"unit_of_measurement": "°C"})
        async_fire_time_changed(hass, expiry + timedelta(seconds=1))
        await hass.async_block_till_done()
        await _settle(hass, runtime)
        assert runtime.boost_mode == "off"
        assert calls[-1].data["temperature"] < 26, {
            key: runtime.values.get(key)
            for key in (
                "input_status",
                "command_outcomes",
                "source_states",
                "effective_target_details",
            )
        }
        assert (
            runtime.values["effective_target_details"]["target-1"]["reference"]
            == "manual_heating_return"
        )
        assert runtime.manual_heating_fallback_pending  # until valid RH resumes
    finally:
        await runtime.async_unload()


async def test_live_preheat_stop_returns_to_unoccupied_goal(hass, monkeypatch, manual_clock):
    runtime, calls = await _live(hass, monkeypatch, manual_clock)
    try:
        hass.config_entries.async_update_entry(
            runtime.entry,
            options={**runtime.entry.options, "occupancy_entity": "binary_sensor.occupied"},
        )
        hass.states.async_set("binary_sensor.occupied", "off")
        await runtime.async_set_preheat_active(True)
        await _settle(hass, runtime)
        active = hass.states.get("climate.target").attributes["temperature"]
        manual_clock.tick(timedelta(seconds=61))
        await runtime.async_set_preheat_active(False)
        await _settle(hass, runtime)
        assert not runtime.preheat_active
        assert calls[-1].data["temperature"] < active
        assert (
            runtime.values["effective_target_details"]["target-1"]["reference"]
            == "manual_heating_return"
        )
    finally:
        await runtime.async_unload()


@pytest.mark.parametrize("guard", ["disabled", "override", "off"])
async def test_live_manual_fallback_cannot_bypass_write_guards(
    hass, monkeypatch, manual_clock, guard
):
    runtime, calls = await _live(hass, monkeypatch, manual_clock)
    try:
        if guard == "disabled":
            await runtime.async_set_control_enabled(False)
        else:
            state = hass.states.get("climate.target")
            hass.states.async_set(
                "climate.target",
                "off" if guard == "off" else "heat",
                {**state.attributes, "temperature": 19.0},
                context=Context(user_id="human"),
            )
            await hass.async_block_till_done()
        calls.clear()
        await runtime.async_set_boost_mode("rapid")
        await _settle(hass, runtime)
        assert not calls
        if guard == "override":
            assert runtime.ownership["registry-1"].ownership is Ownership.MANUAL_OVERRIDE
    finally:
        await runtime.async_unload()


async def test_live_manual_journal_failure_blocks_heat_and_recovery_resumes(
    hass, monkeypatch, manual_clock
):
    runtime, calls = await _live(hass, monkeypatch, manual_clock)
    try:
        original = runtime.persistence.async_update_manual_heating_fallback
        monkeypatch.setattr(
            runtime.persistence,
            "async_update_manual_heating_fallback",
            AsyncMock(return_value=False),
        )
        await runtime.async_set_boost_mode("rapid")
        await _settle(hass, runtime)
        assert not calls
        assert runtime.values["command_outcomes"]["target-1"] == "manual_heating_storage_fault"
        monkeypatch.setattr(runtime.persistence, "async_update_manual_heating_fallback", original)
        await _settle(hass, runtime)
        assert calls[-1].data["temperature"] == 26
        assert runtime.manual_heating_fallback_pending
        hass.states.async_set("sensor.humidity", "50", {"unit_of_measurement": "%"})
        await _settle(hass, runtime)
        manual_clock.tick(timedelta(seconds=31))
        hass.states.async_set("sensor.humidity", "50.1", {"unit_of_measurement": "%"})
        await _settle(hass, runtime)
        assert not runtime.values["manual_heating_fallback"]
        assert not runtime.manual_heating_fallback_pending
        assert runtime.last_valid_at is not None
    finally:
        await runtime.async_unload()


async def test_manual_marker_rejects_request_changed_during_persistence(monkeypatch):
    runtime = _runtime()
    runtime.boost_mode = "rapid"
    snapshot_request = MagicMock()
    monkeypatch.setattr(runtime_module.ZoneRuntime, "async_request_snapshot", snapshot_request)
    result = calculate_runtime_snapshot(_snapshot(boost="rapid"))

    async def save(pending):
        runtime.boost_mode = "off"
        return True

    runtime.persistence = MagicMock()
    runtime.persistence.async_update_manual_heating_fallback = save
    assert not await runtime._async_prepare_manual_heating(result)
    assert runtime.manual_heating_fallback_pending
    snapshot_request.assert_called_once()
    assert not await runtime._async_prepare_manual_heating(result)
    assert snapshot_request.call_count == 2


@pytest.mark.parametrize(
    "damage", [None, "scenarios", "target", "identity", "occupied", "room", "value"]
)
def test_manual_cache_parser_is_identity_bound_and_rejects_invalid_fields(damage):
    runtime = _runtime()
    runtime.last_valid_at = NOW
    scenario = {
        "entity_id": "climate.target",
        "direction": "heating_only",
        "occupied": {"room": {"temperature": 21}},
        "unoccupied": {"room": {"temperature": 17}},
    }
    runtime.last_valid_values = {"thermal_neutral": 23, "target_scenarios": {"target-1": scenario}}
    if damage == "scenarios":
        runtime.last_valid_values["target_scenarios"] = []
    elif damage == "target":
        runtime.last_valid_values["target_scenarios"]["target-1"] = []
    elif damage == "identity":
        scenario["entity_id"] = "climate.another"
    elif damage == "occupied":
        scenario["occupied"] = []
    elif damage == "room":
        scenario["occupied"]["room"] = []
    elif damage == "value":
        scenario["occupied"]["room"]["temperature"] = True
    assert bool(runtime._cached_heating_references()) is (damage is None)
    runtime.last_valid_at = None
    assert runtime._cached_heating_references() == ()


def test_manual_journal_marker_is_compatible_and_retains_expiry_cleanup_across_restart():
    from custom_components.athb.adapters.storage import prepare_startup_recovery
    from tests.control.test_storage import _state as stored_state

    prior = replace(stored_state(clean=True), manual_heating_fallback_pending=True)
    loaded = load_control_state(serialize_control_state(prior))
    recovered = prepare_startup_recovery(
        loaded, run_id="new", configuration_fingerprint="changed", strategy="balanced"
    )
    assert recovered.state.manual_heating_fallback_pending
    raw = json.loads(serialize_control_state(prior))
    raw.pop("manual_heating_fallback_pending")
    assert load_control_state(json.dumps(raw)).state.manual_heating_fallback_pending is False
    raw["manual_heating_fallback_pending"] = "yes"
    assert load_control_state(json.dumps(raw)).state is None


async def test_manual_marker_persistence_verifies_writes_and_preserves_other_fields():
    backend = MemoryBackend()
    persistence = ZoneCommandPersistence(
        backend,
        run_id="run",
        configuration_fingerprint="config",
        strategy="balanced",
        target_identities=("registry-1",),
        control_enabled=True,
    )
    assert not await persistence.async_update_manual_heating_fallback(True)
    assert await persistence.async_start()
    before = persistence.state
    assert await persistence.async_update_manual_heating_fallback(True)
    assert persistence.state == replace(
        before,
        storage_generation=before.storage_generation + 1,
        manual_heating_fallback_pending=True,
    )
    assert load_control_state(backend.value).state.manual_heating_fallback_pending
    backend.save_error = True
    assert not await persistence.async_update_manual_heating_fallback(False)


async def test_restart_after_rapid_expiry_reconciles_then_withdraws_without_replay(
    hass, monkeypatch, manual_clock
):
    runtime, calls = await _live(hass, monkeypatch, manual_clock)
    await runtime.async_set_boost_mode("rapid")
    await _settle(hass, runtime)
    expiry = runtime.values["boost_expiry"]
    assert calls[-1].data["temperature"] == 26
    entry = runtime.entry
    await runtime.async_unload()
    manual_clock.move_to(expiry + timedelta(seconds=1))
    async_fire_time_changed(hass, expiry + timedelta(seconds=1))
    hass.states.async_set(
        "sensor.room",
        "20.01",
        {"unit_of_measurement": "°C"},
        timestamp=(expiry + timedelta(seconds=1)).timestamp(),
    )
    calls.clear()
    restarted = runtime_module.ZoneRuntime(hass, entry, "delivery-test", "balanced", "rapid", True)
    try:
        await restarted.async_start()
        await _settle(hass, restarted)
        assert restarted.boost_mode == "off"
        assert restarted.manual_heating_fallback_pending
        assert not calls  # a restored HA state is not a fresh physical report
        manual_clock.tick(timedelta(seconds=1))
        hass.states.async_set(
            "sensor.room",
            "20.02",
            {"unit_of_measurement": "°C"},
            timestamp=(expiry + timedelta(seconds=2)).timestamp(),
        )
        await _settle(hass, restarted)
        assert restarted.values["calculation"].manual_heating_fallback
        assert calls, {
            key: restarted.values.get(key)
            for key in (
                "input_status",
                "command_outcomes",
                "control_eligible",
                "ownership",
                "effective_target_details",
            )
        }
        assert all(call.data["temperature"] < 26 for call in calls)
        assert (
            restarted.values["effective_target_details"]["target-1"]["reference"]
            == "manual_heating_return"
        )
    finally:
        await restarted.async_unload()


def test_missing_neutral_retains_occupied_goal_without_guessing_a_boost_shift():
    snapshot = _snapshot(boost="adaptive")
    ref = replace(snapshot.cached_heating_references[0], neutral_c=None)
    result = calculate_runtime_snapshot(replace(snapshot, cached_heating_references=(ref,)))
    policy = result.targets[0].result.policy
    assert policy.heating_c == 21
    assert "boost_neutral_unavailable" in policy.limitations

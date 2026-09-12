"""Canonical VI-001 through VI-030 full-path qualification."""

from __future__ import annotations

import json

import pytest

from custom_components.athb.calculation import calculate_runtime_snapshot
from tests.virtual_installations.runner import _broker_run, _captured, run_scenario
from tests.virtual_installations.schema import FIXTURES, load_scenarios

SCENARIOS = load_scenarios()


def test_fixture_library_is_complete_unique_and_deterministic() -> None:
    assert [item["scenario_id"] for item in SCENARIOS] == [
        f"VI-{index:03d}" for index in range(1, 31)
    ]
    encoded = json.dumps(SCENARIOS, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert json.loads(encoded) == list(SCENARIOS)
    assert {path.name for path in FIXTURES.glob("*.json")} == {
        "living_room_base.json",
        "scenarios_v1.json",
        "schema_v1.json",
    }


@pytest.mark.parametrize(
    "scenario",
    SCENARIOS,
    ids=[item["scenario_id"] for item in SCENARIOS],
)
async def test_virtual_installation(
    scenario: dict[str, object], athb_report_results: list[object]
) -> None:
    result = await run_scenario(scenario)
    assert result.passed
    assert result.failed_assertions == ()
    athb_report_results.append(result)


async def test_max_setback_uses_command_minimum_through_full_broker_path() -> None:
    scenario = json.loads(json.dumps(SCENARIOS[0]))
    scenario["zone_configuration"].update(
        {
            "occupancy_state": "off",
            "eco_intensity": "deep",
            "inactive_heating_temperature": 16.0,
            "inactive_cooling_temperature": 29.0,
            "user_min_c": 17.0,
            "user_max_c": 30.0,
        }
    )

    calculation = calculate_runtime_snapshot(_captured(scenario))
    calls, reason, acknowledgement, ownership = await _broker_run(scenario, calculation)

    assert calls == [{"entity_id": "climate.living_room", "temperature": 17.0}]
    assert reason == "own_context_match"
    assert acknowledgement == "acknowledged"
    assert ownership == "owned"


@pytest.mark.parametrize(
    ("boost_mode", "expected_temperature", "expected_phase"),
    [("adaptive", 20.5, "adaptive"), ("rapid", 26.0, "rapid")],
)
async def test_boost_modes_use_full_numerical_policy_and_broker_path(
    boost_mode: str, expected_temperature: float, expected_phase: str
) -> None:
    scenario = json.loads(json.dumps(SCENARIOS[0]))
    scenario["zone_configuration"]["occupancy_state"] = "off"
    scenario["zone_configuration"]["boost_mode"] = boost_mode

    calculation = calculate_runtime_snapshot(_captured(scenario))
    policy = calculation.targets[0].result
    assert policy is not None
    assert policy.policy is not None
    assert policy.policy.profile.value == "eco"
    assert policy.policy.boost_phase == expected_phase
    assert "occupancy_setback" not in policy.policy.limitations

    calls, reason, acknowledgement, ownership = await _broker_run(scenario, calculation)
    assert calls == [{"entity_id": "climate.living_room", "temperature": expected_temperature}]
    assert reason == "own_context_match"
    assert acknowledgement == "acknowledged"
    assert ownership == "owned"

"""Canonical VI-001 through VI-030 full-path qualification."""

from __future__ import annotations

import json

import pytest

from tests.virtual_installations.runner import run_scenario
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

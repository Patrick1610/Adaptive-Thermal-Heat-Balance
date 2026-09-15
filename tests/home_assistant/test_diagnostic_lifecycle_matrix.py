"""Regression matrix derived from anonymized real Home Assistant diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.athb.config_flow import OPTION_DEFAULTS
from custom_components.athb.config_schema import validate_options

FIXTURE = Path(__file__).parents[1] / "fixtures" / "home_assistant_lifecycle_configs.json"


def test_realistic_diagnostic_matrix_is_valid_and_materially_diverse() -> None:
    scenarios = json.loads(FIXTURE.read_text())

    assert [item["name"] for item in scenarios] == [
        "living_room",
        "bathroom",
        "child_bedroom",
        "office",
        "master_bedroom",
        "hallway",
        "no_occupancy_zone",
    ]
    assert all(not validate_options({**OPTION_DEFAULTS, **item["options"]}) for item in scenarios)
    assert len({item["options"]["minimum_control_temperature"] for item in scenarios}) >= 4
    assert len({item["options"]["manual_override_minutes"] for item in scenarios}) >= 6
    assert {item["options"]["fallback_mode"] for item in scenarios} == {"fixed", "no_write"}
    assert {item["options"]["comfort_strategy"] for item in scenarios} == {
        "eco",
        "efficient",
        "balanced",
        "comfort",
        "near_neutral",
    }


def test_living_room_fixture_preserves_delivered_baseline_and_new_defaults() -> None:
    living = json.loads(FIXTURE.read_text())[0]

    assert living["template"] == "delivered_diagnostic"
    assert living["options"]["minimum_control_temperature"] == 15.0
    assert living["options"]["maximum_control_temperature"] == 30.0
    assert living["options"]["boost_delta_c"] == 2.0
    assert living["options"]["fallback_heating_c"] == 18.0
    assert living["options"]["fallback_cooling_c"] == 27.0
    assert living["options"]["primary_temperature_freshness_minutes"] == 30.0


def test_new_wizard_defaults_match_the_approved_safe_baseline() -> None:
    assert OPTION_DEFAULTS["minimum_control_temperature"] == 15.0
    assert OPTION_DEFAULTS["maximum_control_temperature"] == 30.0
    assert OPTION_DEFAULTS["boost_delta_c"] == 2.0
    assert OPTION_DEFAULTS["fallback_heating_c"] == 18.0
    assert OPTION_DEFAULTS["fallback_cooling_c"] == 27.0

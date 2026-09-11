"""Config validation for direct declarations, targets, and safety bounds."""

from __future__ import annotations

import pytest

from custom_components.athb.config_schema import validate_environment, validate_targets


def test_measured_and_declared_rh_are_mutually_exclusive_tagged_sources() -> None:
    measured = {
        "primary_temperature": "sensor.room",
        "outdoor_source": "sensor.outdoor",
        "rh_mode": "measured",
        "rh_entity": "sensor.rh",
    }
    assert validate_environment(measured) == {}
    declared = {
        "primary_temperature": "sensor.room",
        "outdoor_source": "sensor.outdoor",
        "rh_mode": "declared",
        "rh_declared": 47.0,
    }
    assert validate_environment(declared) == {}
    assert validate_environment({**measured, "rh_declared": 50})["rh_entity"] == "invalid_rh_source"
    assert (
        validate_environment({**declared, "rh_declared": True})["rh_declared"]
        == "invalid_rh_source"
    )
    assert (
        validate_environment({**declared, "rh_declared": 101})["rh_declared"] == "invalid_rh_source"
    )


def test_targets_are_distinct_bounded_and_cannot_self_reference() -> None:
    assert validate_targets(["climate.a", "climate.b"]) == ("climate.a", "climate.b")
    for targets in (
        [],
        ["climate.a"] * 2,
        [f"climate.{index}" for index in range(9)],
        ["climate.athb_x"],
    ):
        with pytest.raises(ValueError, match=r"targets|distinct|eight|self"):
            validate_targets(targets)

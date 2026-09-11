"""Config validation for direct declarations, targets, and safety bounds."""

from __future__ import annotations

import pytest

from custom_components.athb.config_schema import (
    validate_environment,
    validate_options,
    validate_targets,
)


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


def test_complete_runtime_options_validate_bounds_fallback_and_critical_locations() -> None:
    valid = {
        "minimum_control_temperature": 18.0,
        "maximum_control_temperature": 26.0,
        "fallback_mode": "fixed",
        "fallback_heating_c": 18.0,
        "fallback_cooling_c": 26.0,
        "met": 1.1,
        "air_speed_m_s": 0.1,
        "fixed_clothing_clo": 0.7,
        "lower_comfort_vote": -0.5,
        "upper_comfort_vote": 0.5,
        "manual_override_minutes": 120,
        "running_mean_alpha": 0.8,
        "calibration_target": -1.0,
        "critical_locations": [
            {"location_id": "seat", "entity_id": "sensor.seat", "mode": "heating"}
        ],
    }
    assert validate_options(valid) == {}
    invalid = {
        **valid,
        "minimum_control_temperature": 27.0,
        "manual_override_minutes": 10,
        "calibration_target": 4.0,
        "critical_locations": [
            {"location_id": "seat", "entity_id": "sensor.a", "mode": "heating"},
            {"location_id": "seat", "entity_id": "sensor.b", "mode": "both"},
        ],
    }
    errors = validate_options(invalid)
    assert errors["control_bounds"] == "invalid_control_bounds"
    assert errors["manual_override_minutes"] == "invalid_option"
    assert errors["calibration_target"] == "invalid_option"
    assert errors["critical_locations"] == "invalid_option"


def test_environment_reports_every_missing_or_conflicting_source() -> None:
    errors = validate_environment({"rh_mode": "unsupported"})
    assert errors == {
        "primary_temperature": "required",
        "outdoor_source": "required",
        "rh_mode": "invalid_rh_source",
    }
    assert (
        validate_environment(
            {
                "primary_temperature": "sensor.room",
                "outdoor_source": "sensor.outdoor",
                "rh_mode": "declared",
                "rh_declared": float("nan"),
                "rh_entity": "sensor.rh",
            }
        )["rh_declared"]
        == "invalid_rh_source"
    )


@pytest.mark.parametrize(
    "targets",
    [None, "climate.a", 1, {}, ["sensor.athb_result"]],
)
def test_target_container_and_all_self_references_fail(targets: object) -> None:
    with pytest.raises(ValueError, match=r"sequence|self"):
        validate_targets(targets)


@pytest.mark.parametrize(
    ("updates", "expected_key"),
    [
        ({"met": True}, "met"),
        ({"air_speed_m_s": float("nan")}, "air_speed_m_s"),
        ({"air_speed_mode": "measured"}, "air_speed_entity"),
        ({"air_speed_mode": "invented"}, "air_speed_mode"),
        ({"clothing_mode": "invented"}, "clothing_mode"),
        ({"clothing_mode": "fixed", "fixed_clothing_clo": 3.0}, "fixed_clothing_clo"),
        ({"eco_intensity": "extreme"}, "eco_intensity"),
        (
            {
                "inactive_heating_temperature": 22.0,
                "inactive_cooling_temperature": 20.0,
            },
            "inactive_cooling_temperature",
        ),
        ({"radiant_model": "surface", "surface_modelled": True}, "surface_f_rsi"),
        (
            {"minimum_control_temperature": 18.0, "maximum_control_temperature": 18.0},
            "control_bounds",
        ),
        ({"fallback_mode": "adaptive"}, "fallback_mode"),
        ({"fallback_heating_c": 17.0}, "fallback_heating_c"),
        ({"fallback_cooling_c": 27.0}, "fallback_cooling_c"),
        ({"fallback_heating_c": 25.0, "fallback_cooling_c": 24.0}, "fallback_cooling_c"),
        ({"auto_mapping": "guess"}, "auto_mapping"),
        ({"critical_locations": "bad"}, "critical_locations"),
        ({"critical_locations": [{}]}, "critical_locations"),
        (
            {
                "critical_locations": [
                    {"location_id": "x", "entity_id": "sensor.x", "mode": "guard"}
                ]
            },
            "critical_locations",
        ),
    ],
)
def test_each_runtime_option_failure_is_typed(
    updates: dict[str, object], expected_key: str
) -> None:
    assert validate_options(updates)[expected_key] in {
        "invalid_option",
        "invalid_control_bounds",
        "required",
    }


def test_no_write_fallback_skips_irrelevant_fixed_target_validation() -> None:
    assert (
        validate_options(
            {
                "fallback_mode": "no_write",
                "fallback_heating_c": -100,
                "fallback_cooling_c": 100,
                "critical_locations": [
                    {"location_id": "a", "entity_id": "sensor.a", "mode": "monitoring"},
                    {"location_id": "b", "entity_id": "sensor.b", "mode": "cooling"},
                ],
            }
        )
        == {}
    )

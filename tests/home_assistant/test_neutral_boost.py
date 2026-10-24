"""Directional neutral boost, executable approach and transactional options regressions."""

from dataclasses import replace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.athb.calculation import calculate_runtime_snapshot
from custom_components.athb.config_flow import AthbOptionsFlow
from custom_components.athb.config_schema import validate_options
from custom_components.athb.core.climate import (
    GridOptions,
    GridRoundingMode,
    NormalizedScalarTarget,
)
from custom_components.athb.core.contracts import (
    ActuationDirection,
    BoostMode,
    ControlProfile,
    RootFailure,
)
from custom_components.athb.core.pipeline import calculate_zone
from custom_components.athb.core.policy import PolicyTargets, build_adaptive_policy
from tests.core.test_pipeline import _input
from tests.core.test_policy import _roots
from tests.home_assistant.test_calculation import _state
from tests.home_assistant.test_config_flow import (
    _settings_entry,
)
from tests.home_assistant.test_manual_heating import _snapshot


@pytest.mark.parametrize("offset", [-1.0, 0.0, 1.0])
@pytest.mark.parametrize(
    "direction", [ActuationDirection.HEATING_ONLY, ActuationDirection.COOLING_ONLY]
)
def test_offset_is_signed_in_the_conditioning_direction(offset, direction):
    result = build_adaptive_policy(
        roots=_roots(),
        critical_demands=(),
        direction=direction,
        profile=ControlProfile.COMFORT,
        boost_mode=BoostMode.ADAPTIVE,
        boost_delta_c=offset,
        explicit_transition=True,
    )
    assert isinstance(result, PolicyTargets)
    assert (
        result.heating_c if direction is ActuationDirection.HEATING_ONLY else result.cooling_c
    ) == (21 + offset if direction is ActuationDirection.HEATING_ONLY else 21 - offset)


@pytest.mark.parametrize(
    ("direction", "current", "after"),
    [
        (ActuationDirection.HEATING_ONLY, 20.49, 20.5),
        (ActuationDirection.COOLING_ONLY, 21.51, 21.5),
    ],
)
def test_rapid_approach_has_an_exact_configurable_one_way_boundary(direction, current, after):
    args = dict(
        roots=_roots(),
        critical_demands=(),
        direction=direction,
        profile=ControlProfile.COMFORT,
        boost_mode=BoostMode.RAPID,
        rapid_boost_approach_c=0.5,
        explicit_transition=True,
    )
    rapid = build_adaptive_policy(**args, current_air_temperature_c=current)
    approach = build_adaptive_policy(**args, current_air_temperature_c=after)
    assert rapid.boost_phase == "rapid"
    assert approach.boost_phase == "approach"
    assert approach.rapid_boost_reached
    held = build_adaptive_policy(
        **args, current_air_temperature_c=current, rapid_boost_reached=approach.rapid_boost_reached
    )
    assert held.boost_phase == "approach"
    assert (held.heating_c, held.cooling_c) == (approach.heating_c, approach.cooling_c)


def test_high_humidity_eco_root_does_not_block_valid_neutral_boost():
    base = replace(
        _input(),
        air_temperature_c=21.19,
        relative_humidity_pct=86.33,
        running_mean_c=10.872081291719349,
        strategy="eco",
    )
    normal = calculate_zone(base)
    assert normal.suppression_reason == "missing_required_root:heating_control"
    boosted = calculate_zone(replace(base, boost_mode=BoostMode.ADAPTIVE, boost_delta_c=1))
    assert isinstance(boosted.roots.heating_control, RootFailure)
    assert boosted.suppression_reason is None
    assert boosted.policy.boost_target_heating_c == 23
    assert boosted.normalized.normalized_room_c == 23


def test_rapid_checks_rounded_bounded_room_goal_not_unreachable_raw_goal():
    base = replace(
        _input(),
        air_temperature_c=21.19,
        relative_humidity_pct=86.33,
        running_mean_c=10.872081291719349,
        strategy="eco",
        boost_mode=BoostMode.RAPID,
        boost_delta_c=1,
        rapid_boost_approach_c=1,
        grid=GridOptions(15, 21.5, rounding_mode=GridRoundingMode.FLOOR),
    )
    result = calculate_zone(base)
    assert result.policy.boost_target_heating_c == 21.5
    assert result.policy.boost_phase == "approach"
    assert result.normalized.normalized_room_c == 21.5


def test_preheat_can_use_cached_occupied_goal_when_low_eco_root_is_unsolvable():
    snapshot = _snapshot(
        preheat=True,
        primary=_state("sensor.room", "21.19", "°C"),
        relative_humidity=_state("sensor.rh", "86.33", "%"),
        running_mean_c=10.872081291719349,
        strategy="eco",
    )
    result = calculate_runtime_snapshot(snapshot)
    assert result.manual_heating_fallback
    assert result.hold_condition == "missing_required_root:heating_control"
    assert isinstance(result.targets[0].result.normalized, NormalizedScalarTarget)
    assert result.targets[0].result.normalized.normalized_room_c == 21
    assert (
        result.targets[0].manual_reference_at
        == snapshot.cached_heating_references[0].observed_at.isoformat()
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("boost_delta_c", -3.1),
        ("boost_delta_c", 3.1),
        ("rapid_boost_approach_c", 0),
        ("rapid_boost_approach_c", True),
        ("rapid_boost_approach_c", float("nan")),
    ],
)
def test_new_boost_fields_are_bounded_finite_and_not_boolean(key, value):
    assert key in validate_options({key: value})


async def test_multistep_settings_keep_drafts_without_page_actions(
    hass: HomeAssistant,
):
    entry = _settings_entry(hass)
    flow = AthbOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_boost_settings({"boost_delta_c": 1, "rapid_boost_approach_c": 0.7})
    result = await flow.async_step_comfort(
        {"comfort_strategy": "comfort", "radiant_model": "mold_indicator"}
    )
    assert result["step_id"] == "radiant"
    assert result["last_step"] is False
    schema = result["data_schema"]
    assert {str(marker.schema) for marker in schema.schema} == {"mold_indicator_entity"}
    with pytest.raises(vol.MultipleInvalid) as error:
        schema({})
    assert len(error.value.errors) == 1
    assert isinstance(error.value.errors[0], vol.RequiredFieldInvalid)
    result = await flow.async_step_radiant({"mold_indicator_entity": "sensor.mold_indicator"})
    assert result["type"] is FlowResultType.MENU
    assert flow._pending_options["comfort_strategy"] == "comfort"
    assert flow._pending_options["radiant_model"] == "mold_indicator"
    assert flow._pending_options["boost_delta_c"] == 1
    assert flow._pending_options["mold_indicator_entity"] == "sensor.mold_indicator"
    assert "boost_delta_c" not in entry.options
    assert "mold_indicator_entity" not in entry.options


@pytest.mark.parametrize("section", tuple(AthbOptionsFlow._SECTIONS))
async def test_settings_sections_have_no_page_action(hass: HomeAssistant, section: str):
    flow = AthbOptionsFlow(_settings_entry(hass))
    flow.hass = hass
    result = await getattr(flow, f"async_step_{section}")()
    assert result["type"] is FlowResultType.FORM
    assert result["last_step"] is False
    assert "section_action" not in {str(marker.schema) for marker in result["data_schema"].schema}


async def test_final_save_applies_all_drafts_once_and_keeps_identities(hass: HomeAssistant):
    entry = _settings_entry(hass, options={"control_enabled": False, "custom_preserved": "keep"})
    targets = entry.data["targets"]
    flow = AthbOptionsFlow(entry)
    flow.hass = hass
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await flow.async_step_boost_settings({"boost_delta_c": 1, "rapid_boost_approach_c": 1})
        result = await flow.async_step_control_limits(
            {
                "minimum_control_temperature": 15,
                "maximum_control_temperature": 35,
                "manual_override_minutes": 120,
            }
        )
        assert result["type"] is FlowResultType.MENU
        reload.assert_not_called()
        assert entry.options == {"control_enabled": False, "custom_preserved": "keep"}
        saved = await flow.async_step_finalize()
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        reload.assert_called_once_with(entry.entry_id)
    assert entry.data["targets"] == targets
    assert entry.options["maximum_control_temperature"] == 35
    assert entry.options["boost_delta_c"] == 1
    assert entry.options["custom_preserved"] == "keep"


async def test_abandoning_draft_does_not_change_operational_entry(hass: HomeAssistant):
    entry = _settings_entry(hass)
    prior = dict(entry.options)
    flow = AthbOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_boost_settings({"boost_delta_c": 1})
    # A new flow after closing has only the saved operational values.
    reopened = AthbOptionsFlow(entry)
    assert reopened._pending_options["boost_delta_c"] == 0
    assert dict(entry.options) == prior


async def test_final_save_rejects_incomplete_environment_without_partial_writes(
    hass: HomeAssistant,
):
    entry = _settings_entry(hass)
    flow = AthbOptionsFlow(entry)
    flow.hass = hass
    flow._pending_data.pop("primary_temperature")
    flow._pending_options["boost_delta_c"] = 1
    result = await flow.async_step_finalize()
    assert result["step_id"] == "save_errors"
    assert "boost_delta_c" not in entry.options
    assert entry.data["primary_temperature"] == "sensor.room"

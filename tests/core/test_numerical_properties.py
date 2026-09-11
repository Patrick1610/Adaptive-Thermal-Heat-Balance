"""Pinned-oracle conformance and deterministic numerical property tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from custom_components.athb.core.athb_engine import evaluate_athb, relative_air_speed
from custom_components.athb.core.contracts import (
    AUTOMATIC_CLOTHING,
    AthbInputs,
    AthbSuccess,
    FixedClothing,
    NumericalFailure,
    RootFailureCode,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "numerical"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _forward_cases() -> Iterator[dict[str, Any]]:
    fixture = _load("athb_forward_v1.json")
    yield from fixture["named_cases"]
    yield from fixture["dense_grid"]["cases"]
    yield from fixture["seeded_random"]["cases"]


def _evaluate_fixture_inputs(inputs: dict[str, Any]) -> AthbSuccess | NumericalFailure:
    speed = inputs["speed"]
    speed_value = float(speed["value_m_s"])
    if speed["kind"] == "ambient":
        converted = relative_air_speed(speed_value, float(inputs["met"]))
        if isinstance(converted, NumericalFailure):
            return converted
        speed_value = converted
    clothing_specification = inputs["clothing"]
    clothing = (
        AUTOMATIC_CLOTHING
        if clothing_specification["kind"] == "automatic"
        else FixedClothing(float(clothing_specification["clo"]))
    )
    return evaluate_athb(
        AthbInputs(
            tdb_c=float(inputs["tdb_c"]),
            tr_c=float(inputs["tr_c"]),
            relative_air_speed_m_s=speed_value,
            rh_pct=float(inputs["rh_pct"]),
            met=float(inputs["met"]),
            running_mean_c=float(inputs["running_mean_c"]),
            clothing=clothing,
        )
    )


@pytest.mark.parametrize("case", list(_forward_cases()), ids=lambda case: case["case_id"])
def test_all_248_forward_vectors_match_pinned_oracle(case: dict[str, Any]) -> None:
    result = _evaluate_fixture_inputs(case["inputs"])
    expected = case["expected"]

    assert isinstance(result, AthbSuccess)
    assert result.sensation_vote == pytest.approx(expected["unrounded_vote"], abs=1e-7)
    assert result.public_sensation_vote == pytest.approx(expected["public_vote"], abs=0.00051)
    assert result.adapted_met == pytest.approx(expected["adapted_met"], abs=1e-12)
    assert result.effective_clo == pytest.approx(expected["effective_clo"], abs=1e-12)
    assert result.relative_air_speed_m_s == expected["relative_air_speed_m_s"]
    assert result.thermal_load_w_m2 == pytest.approx(expected["thermal_load_w_m2"], abs=1e-7)


def test_quantization_boundary_fixtures_exercise_pinned_binary64_paths() -> None:
    fixture = _load("athb_forward_v1.json")
    assert fixture["quantization_boundary_case_ids"] == [
        "relative-air-speed-numpy-tie-even",
        "public-vote-numpy-tie-even",
        "public-vote-upstream-binary64-straddle",
    ]
    cases = {case["case_id"]: case for case in fixture["named_cases"]}

    relative_case = cases["relative-air-speed-numpy-tie-even"]
    assert relative_case["expected"]["relative_air_speed_m_s"] == 0.616
    assert round(0.6165, 3) == 0.617
    relative_result = _evaluate_fixture_inputs(relative_case["inputs"])
    assert isinstance(relative_result, AthbSuccess)
    assert relative_result.relative_air_speed_m_s == 0.616

    public_case = cases["public-vote-numpy-tie-even"]
    public_result = _evaluate_fixture_inputs(public_case["inputs"])
    assert isinstance(public_result, AthbSuccess)
    assert public_result.sensation_vote == public_case["expected"]["unrounded_vote"]
    assert round(-1.1865, 3) == -1.187
    assert public_result.public_sensation_vote == -1.186
    assert public_case["expected"]["public_vote"] == -1.186

    straddle_case = cases["public-vote-upstream-binary64-straddle"]
    straddle_result = _evaluate_fixture_inputs(straddle_case["inputs"])
    assert isinstance(straddle_result, AthbSuccess)
    assert straddle_result.thermal_load_w_m2 == -55.793504353973056
    assert straddle_result.sensation_vote == -1.2854999999999959
    assert straddle_result.sensation_vote == straddle_case["expected"]["unrounded_vote"]
    assert straddle_result.public_sensation_vote == -1.285
    assert straddle_case["expected"]["public_vote"] == -1.285


def test_displayed_current_sensation_anchors_match_within_one_microvote() -> None:
    expected = {
        "baseline-current-sensation": -0.17308845704988404,
        "below-comfort-band": -0.65306988911957,
        "humid-current-sensation": 0.5639479489801329,
        "near-balanced-target": -0.23344521023117604,
        "winter-current-sensation": 0.20671480116953517,
    }
    actual = {
        case["case_id"]: _evaluate_fixture_inputs(case["inputs"])
        for case in _load("athb_forward_v1.json")["named_cases"]
        if case["case_id"] in expected
    }

    assert set(actual) == set(expected)
    for case_id, result in actual.items():
        assert isinstance(result, AthbSuccess)
        assert result.sensation_vote == pytest.approx(expected[case_id], abs=1e-6)


def test_independent_root_fixture_successes_re_evaluate_at_requested_votes() -> None:
    fixture = _load("athb_roots_v1.json")
    checked_successes = 0
    checked_failures = 0

    for scenario in fixture["scenarios"]:
        current = _evaluate_fixture_inputs(scenario["current_inputs"])
        assert isinstance(current, AthbSuccess)
        assert current.sensation_vote == pytest.approx(
            scenario["current_expected"]["unrounded_vote"],
            abs=1e-7,
        )
        for root in scenario["roots"].values():
            if "failure" in root:
                assert root["failure"] in {failure.value for failure in RootFailureCode}
                assert root["evaluation_count"] > 0
                checked_failures += 1
                continue
            result = _evaluate_fixture_inputs(root["candidate_inputs"])
            assert isinstance(result, AthbSuccess)
            assert result.sensation_vote == pytest.approx(root["requested_vote"], abs=1e-7)
            assert result.sensation_vote == pytest.approx(root["unrounded_vote"], abs=1e-7)
            assert abs(root["residual_vote"]) <= 1e-7
            assert root["bracket_width_c"] <= 1e-10
            checked_successes += 1

    assert checked_successes == 102
    assert checked_failures == 3


def test_strategy_and_special_root_anchors_are_frozen() -> None:
    scenarios = {
        scenario["scenario_id"]: scenario for scenario in _load("athb_roots_v1.json")["scenarios"]
    }

    assert scenarios["G_BASE_B"]["roots"]["heating_control"]["room_temperature_c"] == pytest.approx(
        19.3627090708,
        abs=1e-9,
    )
    assert scenarios["G_BASE_E"]["roots"]["heating_control"]["room_temperature_c"] == pytest.approx(
        18.532021554,
        abs=1e-9,
    )
    assert scenarios["G_BASE_C"]["roots"]["cooling_control"]["room_temperature_c"] == pytest.approx(
        22.662014326,
        abs=1e-9,
    )
    assert scenarios["G_SURFACE16_B"]["roots"]["heating_control"][
        "room_temperature_c"
    ] == pytest.approx(19.798353506, abs=1e-9)
    assert scenarios["G_SURFACE14_B"]["roots"]["heating_control"][
        "room_temperature_c"
    ] == pytest.approx(20.050001249, abs=1e-9)
    assert scenarios["FIXED_MRT16_B"]["roots"]["heating_control"][
        "room_temperature_c"
    ] == pytest.approx(22.249377457, abs=1e-9)
    humid = scenarios["G_HUMID_B"]["roots"]
    assert humid["heating_control"]["failure"] == "moisture_limited_no_solution"
    assert humid["cooling_control"]["room_temperature_c"] == pytest.approx(
        25.6607812993,
        abs=1e-9,
    )


def test_selected_formula_is_not_interchangeable_with_inspected_comf_terms() -> None:
    case = next(
        case
        for case in _load("athb_forward_v1.json")["named_cases"]
        if case["case_id"] == "published-forward-anchor"
    )
    selected = _evaluate_fixture_inputs(case["inputs"])
    assert isinstance(selected, AthbSuccess)
    load = selected.thermal_load_w_m2
    adapted_met = selected.adapted_met
    running_mean = float(case["inputs"]["running_mean_c"])
    inspected_comf_variant = (
        selected.sensation_vote
        + 0.002971073 * load * adapted_met
        + 0.0002264348 * load * running_mean
    )

    assert abs(inspected_comf_variant - selected.sensation_vote) > 0.01


def test_repeated_evaluation_is_bitwise_deterministic() -> None:
    case = list(_forward_cases())[137]
    first = _evaluate_fixture_inputs(case["inputs"])

    assert isinstance(first, AthbSuccess)
    for _ in range(100):
        assert _evaluate_fixture_inputs(case["inputs"]) == first

"""Golden, property, failure, and directional tests for inverse solving."""

from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

from custom_components.athb.core.athb_engine import relative_air_speed
from custom_components.athb.core.contracts import (
    AUTOMATIC_CLOTHING,
    ActuationDirection,
    AthbSuccess,
    ComfortStrategy,
    MeasuredMeanRadiantTemperature,
    MeasuredRelativeHumidity,
    MeasuredSurfaceTemperature,
    ModelledSurfaceTemperature,
    NumericalFailure,
    NumericalFailureCode,
    RootFailure,
    RootFailureCode,
    RootName,
    RootSet,
    RootSuccess,
)
from custom_components.athb.core.inverse import (
    CandidateFailure,
    CandidatePoint,
    DirectionalEligibility,
    EvaluationBudget,
    InverseLocationContext,
    StrategyVoteFailure,
    StrategyVotes,
    directional_root_eligibility,
    evaluate_current_location,
    root_results_in_order,
    solve_five_roots,
    solve_requested_roots,
    strategy_votes,
)
from custom_components.athb.core.psychrometrics import (
    MoistureState,
    candidate_relative_humidity,
    moisture_state,
)
from custom_components.athb.core.radiant import (
    DirectRadiantModel,
    SurfaceCompositeRadiantModel,
    SurfaceContribution,
    UniformRadiantModel,
)

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "numerical" / "athb_roots_v1.json").read_text(encoding="utf-8")
)


def _strategy_for(scenario: dict[str, Any]) -> ComfortStrategy:
    heating_vote = scenario["requested_votes"]["heating_control"]
    return {
        -0.35: ComfortStrategy.EFFICIENT,
        -0.25: ComfortStrategy.BALANCED,
        -0.15: ComfortStrategy.COMFORT,
        -0.5: ComfortStrategy.BALANCED,
    }[heating_vote]


def _scenario_context(scenario: dict[str, Any]) -> tuple[InverseLocationContext, StrategyVotes]:
    current = scenario["current"]
    primary_moisture = moisture_state(current["tdb_c"], MeasuredRelativeHumidity(current["rh_pct"]))
    assert isinstance(primary_moisture, MoistureState)
    delta = scenario["local_delta_c"]
    local_temperature = current["tdb_c"] - delta
    if delta:
        local_humidity = candidate_relative_humidity(primary_moisture, local_temperature)
        assert not hasattr(local_humidity, "code")
        moisture = MoistureState(
            local_temperature,
            local_humidity.relative_humidity_pct,
            primary_moisture.vapor_pressure_pa,
            primary_moisture.provenance,
        )
    else:
        moisture = primary_moisture

    radiant_data = scenario["radiant"]
    if radiant_data["kind"] == "moving_uniform":
        radiant = UniformRadiantModel()
    elif radiant_data["kind"] == "fixed":
        radiant = DirectRadiantModel(MeasuredMeanRadiantTemperature(radiant_data["tr_c"]))
    else:
        source = (
            ModelledSurfaceTemperature(radiant_data["surface_temperature_c"])
            if scenario["scenario_id"] == "G_SURFACE14_B"
            else MeasuredSurfaceTemperature(radiant_data["surface_temperature_c"])
        )
        radiant = SurfaceCompositeRadiantModel(
            (SurfaceContribution(source, radiant_data["view_factor"]),)
        )
    speed = relative_air_speed(scenario["speed"]["value_m_s"], scenario["met"])
    assert isinstance(speed, float)
    votes = strategy_votes(
        _strategy_for(scenario),
        lower_vote=scenario["requested_votes"]["lower_comfort"],
        upper_vote=scenario["requested_votes"]["upper_comfort"],
    )
    assert isinstance(votes, StrategyVotes)
    return (
        InverseLocationContext(
            current["tdb_c"],
            local_temperature,
            moisture,
            radiant,
            speed,
            scenario["met"],
            scenario["running_mean_c"],
            AUTOMATIC_CLOTHING,
            delta,
        ),
        votes,
    )


@pytest.mark.parametrize(
    "scenario",
    FIXTURE["scenarios"],
    ids=[item["scenario_id"] for item in FIXTURE["scenarios"]],
)
def test_production_inverse_matches_all_checked_in_root_goldens(
    scenario: dict[str, Any],
) -> None:
    context, votes = _scenario_context(scenario)
    current = evaluate_current_location(context)
    assert isinstance(current, AthbSuccess)
    assert current.sensation_vote == pytest.approx(
        scenario["current_expected"]["unrounded_vote"], abs=1e-12
    )

    roots = solve_five_roots(context, votes)
    actual_by_name = {result.name.value: result for result in root_results_in_order(roots)}
    for name, expected in scenario["roots"].items():
        actual = actual_by_name[name]
        if "failure" in expected:
            assert isinstance(actual, RootFailure)
            assert actual.failure.value == expected["failure"]
        else:
            assert isinstance(actual, RootSuccess)
            assert actual.mapped_room_temperature_c == pytest.approx(
                expected["room_temperature_c"], abs=0.005
            )
            assert actual.local_candidate_temperature_c == pytest.approx(
                expected["local_candidate_temperature_c"], abs=0.005
            )
            assert abs(actual.residual) <= 0.002
            assert actual.bracket_width_c <= 0.005


def test_strategy_votes_are_solved_in_sensation_space_not_temperature_midpoints() -> None:
    scenario = next(item for item in FIXTURE["scenarios"] if item["scenario_id"] == "G_WIDE_B")
    context, votes = _scenario_context(scenario)
    assert votes.heating_control == -0.5
    assert votes.cooling_control == 0.5
    roots = solve_five_roots(context, votes)
    assert isinstance(roots.heating_control, RootSuccess)
    assert isinstance(roots.cooling_control, RootSuccess)
    assert roots.heating_control.mapped_room_temperature_c == pytest.approx(17.281547, abs=0.005)
    assert roots.heating_control.mapped_room_temperature_c != pytest.approx(17.251964, abs=0.01)
    assert roots.cooling_control.mapped_room_temperature_c == pytest.approx(25.518575, abs=0.005)
    assert roots.cooling_control.mapped_room_temperature_c != pytest.approx(25.490230, abs=0.01)


@pytest.mark.parametrize(
    ("strategy", "fraction", "heating", "cooling"),
    [
        (ComfortStrategy.ECO, 0.1, -0.45, 0.45),
        (ComfortStrategy.EFFICIENT, 0.3, -0.35, 0.35),
        (ComfortStrategy.BALANCED, 0.5, -0.25, 0.25),
        (ComfortStrategy.COMFORT, 0.7, -0.15, 0.15),
        (ComfortStrategy.NEAR_NEUTRAL, 0.9, -0.05, 0.05),
    ],
)
def test_strategy_vote_fractions(
    strategy: ComfortStrategy, fraction: float, heating: float, cooling: float
) -> None:
    result = strategy_votes(strategy)
    assert isinstance(result, StrategyVotes)
    assert result.inward_fraction == fraction
    assert result.heating_control == pytest.approx(heating)
    assert result.cooling_control == pytest.approx(cooling)
    assert [name for name, _ in result.ordered()] == list(RootName)


def test_default_comfort_levels_have_uniform_sensation_steps() -> None:
    results = [strategy_votes(strategy) for strategy in ComfortStrategy]
    assert all(isinstance(result, StrategyVotes) for result in results)
    heating = [result.heating_control for result in results if isinstance(result, StrategyVotes)]
    cooling = [result.cooling_control for result in results if isinstance(result, StrategyVotes)]
    assert [right - left for left, right in pairwise(heating)] == pytest.approx([0.1] * 4)
    assert [right - left for left, right in pairwise(cooling)] == pytest.approx([-0.1] * 4)


def test_asymmetric_boundaries_change_votes_before_solving() -> None:
    result = strategy_votes(ComfortStrategy.BALANCED, lower_vote=-0.6, upper_vote=0.4)
    assert isinstance(result, StrategyVotes)
    assert result.heating_control == pytest.approx(-0.3)
    assert result.cooling_control == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("lower", "upper"),
    [(0.0, 0.5), (-0.5, 0.0), (1.0, -1.0), (math.nan, 0.5), (False, 0.5)],
)
def test_invalid_comfort_boundaries_are_typed(lower: float, upper: float) -> None:
    assert isinstance(
        strategy_votes(ComfortStrategy.BALANCED, lower_vote=lower, upper_vote=upper),
        StrategyVoteFailure,
    )


def test_nonnumeric_boundaries_and_unknown_strategy_are_typed() -> None:
    assert isinstance(
        strategy_votes(
            ComfortStrategy.BALANCED,
            lower_vote=cast(Any, object()),
            upper_vote=0.5,
        ),
        StrategyVoteFailure,
    )
    assert isinstance(
        strategy_votes(cast(Any, "unknown")),
        StrategyVoteFailure,
    )


def _linear_evaluator(offset: float = 0.0):
    def evaluate(value: float, budget: EvaluationBudget) -> CandidatePoint | CandidateFailure:
        if not budget.consume():
            return CandidateFailure(RootFailureCode.EVALUATION_BUDGET_EXCEEDED, "budget")
        return CandidatePoint(value, value, value + offset)

    return evaluate


def test_generic_solver_reports_below_above_and_never_returns_endpoint_as_root() -> None:
    votes = cast(
        StrategyVotes,
        strategy_votes(ComfortStrategy.BALANCED, lower_vote=-2.0, upper_vote=2.0),
    )
    roots = solve_requested_roots(
        votes=votes,
        evaluate=_linear_evaluator(),
        search_low_c=-0.4,
        search_high_c=0.4,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.lower_comfort, RootFailure)
    assert roots.lower_comfort.failure is RootFailureCode.BELOW_SEARCH_DOMAIN
    assert isinstance(roots.upper_comfort, RootFailure)
    assert roots.upper_comfort.failure is RootFailureCode.ABOVE_SEARCH_DOMAIN
    assert isinstance(roots.thermal_neutral, RootSuccess)
    assert roots.thermal_neutral.mapped_room_temperature_c == 0.0


def test_generic_solver_detects_non_monotonic_samples() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))

    def decreasing(value: float, budget: EvaluationBudget) -> CandidatePoint:
        assert budget.consume()
        return CandidatePoint(value, value, -value)

    roots = solve_requested_roots(
        votes=votes,
        evaluate=decreasing,
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert all(
        isinstance(result, RootFailure) and result.failure is RootFailureCode.NON_MONOTONIC
        for result in root_results_in_order(roots)
    )


def test_generic_solver_detects_multiple_brackets_without_large_monotonic_drop() -> None:
    votes = StrategyVotes(-2e-5, 0.0, 0.0, 1e-5, 2e-5, ComfortStrategy.BALANCED, 0.5)

    def oscillating(value: float, budget: EvaluationBudget) -> CandidatePoint:
        assert budget.consume()
        return CandidatePoint(value, value, 1e-5 * math.sin(value * 12.0))

    roots = solve_requested_roots(
        votes=votes,
        evaluate=oscillating,
        search_low_c=0.1,
        search_high_c=2.0,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.thermal_neutral, RootFailure)
    assert roots.thermal_neutral.failure is RootFailureCode.MULTIPLE_BRACKETS


def test_generic_solver_detects_repeated_exact_roots() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))

    def flat(_value: float, budget: EvaluationBudget) -> CandidatePoint:
        assert budget.consume()
        return CandidatePoint(0.0, 0.0, 0.0)

    roots = solve_requested_roots(
        votes=votes,
        evaluate=flat,
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.thermal_neutral, RootFailure)
    assert roots.thermal_neutral.failure is RootFailureCode.MULTIPLE_BRACKETS


def test_exact_sample_root_still_requires_a_successful_final_check() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))
    zero_calls = 0

    def fail_recheck(value: float, budget: EvaluationBudget) -> CandidatePoint | CandidateFailure:
        nonlocal zero_calls
        assert budget.consume()
        if value == 0.0:
            zero_calls += 1
            if zero_calls == 2:
                return CandidateFailure(RootFailureCode.NON_FINITE, "changed")
        return CandidatePoint(value, value, value)

    roots = solve_requested_roots(
        votes=votes,
        evaluate=fail_recheck,
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.thermal_neutral, RootFailure)
    assert roots.thermal_neutral.failure is RootFailureCode.NON_FINITE


def test_nonfinite_requested_vote_fails_only_that_root() -> None:
    votes = StrategyVotes(-0.5, -0.25, math.nan, 0.25, 0.5, ComfortStrategy.BALANCED, 0.5)
    roots = solve_requested_roots(
        votes=votes,
        evaluate=_linear_evaluator(),
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.thermal_neutral, RootFailure)
    assert roots.thermal_neutral.failure is RootFailureCode.NON_FINITE
    assert isinstance(roots.heating_control, RootSuccess)


def test_generic_solver_propagates_candidate_failure_and_budget_limit() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))

    def nonfinite(value: float, budget: EvaluationBudget) -> CandidatePoint | CandidateFailure:
        assert budget.consume()
        if value > 0.0:
            return CandidateFailure(RootFailureCode.NON_FINITE, "not finite")
        return CandidatePoint(value, value, value)

    roots = solve_requested_roots(
        votes=votes,
        evaluate=nonfinite,
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert all(
        isinstance(result, RootFailure) and result.failure is RootFailureCode.NON_FINITE
        for result in root_results_in_order(roots)
    )
    exhausted = solve_requested_roots(
        votes=votes,
        evaluate=_linear_evaluator(),
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(limit=5),
    )
    assert all(
        isinstance(result, RootFailure)
        and result.failure is RootFailureCode.EVALUATION_BUDGET_EXCEEDED
        for result in root_results_in_order(exhausted)
    )


def test_generic_solver_rejects_nonfinite_candidate_payload() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))

    def nonfinite_payload(value: float, budget: EvaluationBudget) -> CandidatePoint:
        assert budget.consume()
        return CandidatePoint(value, value, math.nan)

    roots = solve_requested_roots(
        votes=votes,
        evaluate=nonfinite_payload,
        search_low_c=-1.0,
        search_high_c=1.0,
        budget=EvaluationBudget(),
    )
    assert all(
        isinstance(result, RootFailure) and result.failure is RootFailureCode.NON_FINITE
        for result in root_results_in_order(roots)
    )


def test_generic_solver_enforces_iteration_limit() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))
    roots = solve_requested_roots(
        votes=votes,
        evaluate=_linear_evaluator(offset=-0.123456789),
        search_low_c=-1e20,
        search_high_c=1e20,
        budget=EvaluationBudget(),
    )
    assert isinstance(roots.thermal_neutral, RootFailure)
    assert roots.thermal_neutral.failure is RootFailureCode.ITERATION_LIMIT


def test_invalid_search_interval_is_typed_for_every_root() -> None:
    votes = cast(StrategyVotes, strategy_votes(ComfortStrategy.BALANCED))
    for low, high, expected in (
        (math.nan, 1.0, RootFailureCode.NON_FINITE),
        (1.0, 1.0, RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN),
    ):
        roots = solve_requested_roots(
            votes=votes,
            evaluate=_linear_evaluator(),
            search_low_c=low,
            search_high_c=high,
            budget=EvaluationBudget(),
        )
        assert all(
            isinstance(result, RootFailure) and result.failure is expected
            for result in root_results_in_order(roots)
        )


def test_directional_root_eligibility_uses_only_relevant_roots() -> None:
    scenario = next(item for item in FIXTURE["scenarios"] if item["scenario_id"] == "G_HUMID_B")
    context, votes = _scenario_context(scenario)
    current = evaluate_current_location(context)
    roots = solve_five_roots(context, votes)
    cooling = directional_root_eligibility(
        current=current, roots=roots, direction=ActuationDirection.COOLING_ONLY
    )
    heating = directional_root_eligibility(
        current=current, roots=roots, direction=ActuationDirection.HEATING_ONLY
    )
    ranged = directional_root_eligibility(
        current=current, roots=roots, direction=ActuationDirection.RANGED
    )
    assert cooling == DirectionalEligibility(True, (RootName.COOLING_CONTROL,), ())
    assert not heating.eligible
    assert heating.reasons == ("missing_required_root:heating_control",)
    assert not ranged.eligible


def test_ranged_eligibility_requires_order_and_gap_but_outer_roots_are_irrelevant() -> None:
    success_h = RootSuccess(RootName.HEATING_CONTROL, -0.25, 20.0, 20.0, 0.0, 0.0, 1)
    success_c = RootSuccess(RootName.COOLING_CONTROL, 0.25, 20.5, 20.5, 0.0, 0.0, 1)
    failure = RootFailure(RootName.LOWER_COMFORT, -0.5, RootFailureCode.NO_BRACKET, 1, "diagnostic")
    roots = RootSet(
        failure,
        success_h,
        RootFailure(RootName.THERMAL_NEUTRAL, 0.0, RootFailureCode.NO_BRACKET, 1, "diagnostic"),
        success_c,
        RootFailure(RootName.UPPER_COMFORT, 0.5, RootFailureCode.NO_BRACKET, 1, "diagnostic"),
    )
    current = AthbSuccess(-0.1, -0.1, 1.0, 1.0, 0.1, -1.0, ())
    accepted = directional_root_eligibility(
        current=current,
        roots=roots,
        direction=ActuationDirection.RANGED,
        minimum_range_gap_c=0.5,
    )
    rejected = directional_root_eligibility(
        current=current,
        roots=roots,
        direction=ActuationDirection.RANGED,
        minimum_range_gap_c=1.0,
    )
    assert accepted.eligible
    assert not rejected.eligible
    assert rejected.reasons == ("invalid_control_band_order_or_gap",)


def test_invalid_current_vote_blocks_every_direction() -> None:
    scenario = FIXTURE["scenarios"][0]
    context, votes = _scenario_context(scenario)
    roots = solve_five_roots(context, votes)
    failure = NumericalFailure(NumericalFailureCode.NON_FINITE, None, "bad")
    result = directional_root_eligibility(
        current=failure, roots=roots, direction=ActuationDirection.HEATING_ONLY
    )
    assert not result.eligible
    assert result.reasons == ("current_athb_unavailable",)


def test_current_evaluation_enforces_budget_and_radiant_failures() -> None:
    context, _votes = _scenario_context(FIXTURE["scenarios"][0])
    exhausted = evaluate_current_location(context, budget=EvaluationBudget(limit=0))
    assert isinstance(exhausted, NumericalFailure)
    assert exhausted.code is NumericalFailureCode.EVALUATION_BUDGET_EXCEEDED

    bad_radiant = InverseLocationContext(
        context.current_room_air_temperature_c,
        context.current_local_air_temperature_c,
        context.moisture,
        DirectRadiantModel(MeasuredMeanRadiantTemperature(250.0)),
        context.relative_air_speed_m_s,
        context.met,
        context.running_mean_c,
        context.clothing,
    )
    invalid = evaluate_current_location(bad_radiant)
    assert isinstance(invalid, NumericalFailure)
    assert invalid.code is NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN


def test_candidate_maps_radiant_forward_and_moisture_failures() -> None:
    context, votes = _scenario_context(FIXTURE["scenarios"][0])
    bad_radiant = InverseLocationContext(
        context.current_room_air_temperature_c,
        context.current_local_air_temperature_c,
        context.moisture,
        DirectRadiantModel(MeasuredMeanRadiantTemperature(250.0)),
        context.relative_air_speed_m_s,
        context.met,
        context.running_mean_c,
        context.clothing,
    )
    radiant_roots = solve_five_roots(bad_radiant, votes)
    assert all(
        isinstance(result, RootFailure)
        and result.failure is RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN
        for result in root_results_in_order(radiant_roots)
    )
    bad_met = InverseLocationContext(
        context.current_room_air_temperature_c,
        context.current_local_air_temperature_c,
        context.moisture,
        context.radiant,
        context.relative_air_speed_m_s,
        3.0,
        context.running_mean_c,
        context.clothing,
    )
    forward_roots = solve_five_roots(bad_met, votes)
    assert all(
        isinstance(result, RootFailure)
        and result.failure is RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN
        for result in root_results_in_order(forward_roots)
    )
    bad_moisture = InverseLocationContext(
        context.current_room_air_temperature_c,
        context.current_local_air_temperature_c,
        MoistureState(20.0, 50.0, -1.0, context.moisture.provenance),
        context.radiant,
        context.relative_air_speed_m_s,
        context.met,
        context.running_mean_c,
        context.clothing,
    )
    moisture_roots = solve_five_roots(bad_moisture, votes)
    assert all(
        isinstance(result, RootFailure)
        and result.failure is RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN
        for result in root_results_in_order(moisture_roots)
    )


def test_nine_location_solver_budget_stays_below_hard_cap() -> None:
    context, votes = _scenario_context(FIXTURE["scenarios"][0])
    budget = EvaluationBudget()
    for delta in (0.0, -3.0, -2.0, -1.0, 0.5, 1.0, 1.5, 2.0, 3.0):
        candidate = InverseLocationContext(
            context.current_room_air_temperature_c,
            context.current_room_air_temperature_c - delta,
            context.moisture,
            context.radiant,
            context.relative_air_speed_m_s,
            context.met,
            context.running_mean_c,
            context.clothing,
            delta,
        )
        assert isinstance(evaluate_current_location(candidate, budget=budget), AthbSuccess)
        solve_five_roots(candidate, votes, budget=budget)
    assert budget.used < 3000

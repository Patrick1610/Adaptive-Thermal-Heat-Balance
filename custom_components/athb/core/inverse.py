"""Deterministic ATHB inverse solving in sensation space."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

from .athb_engine import evaluate_athb
from .contracts import (
    ActuationDirection,
    ApplicabilityReason,
    AthbInputs,
    AthbResult,
    AthbSuccess,
    Clothing,
    ComfortStrategy,
    DewPointStatus,
    MoistureFailureCode,
    NumericalFailure,
    NumericalFailureCode,
    RadiantFailureCode,
    RootFailure,
    RootFailureCode,
    RootName,
    RootResult,
    RootSet,
    RootSuccess,
)
from .psychrometrics import (
    MoistureFailure,
    MoistureState,
    candidate_relative_humidity,
    dew_or_frost_point,
)
from .radiant import RadiantFailure, RadiantResult

SEARCH_MIN_C = 5.0
SEARCH_MAX_C = 40.0
SAMPLE_COUNT = 33
MONOTONIC_DECREASE_TOLERANCE = 1e-4
ROOT_WIDTH_TOLERANCE_C = 0.005
ROOT_RESIDUAL_TOLERANCE = 0.002
MAX_ROOT_ITERATIONS = 48
DEFAULT_EVALUATION_BUDGET = 3000
PSYCHROMETRIC_SEARCH_MARGIN_C = 0.001

_STRATEGY_FRACTIONS = {
    ComfortStrategy.ECO: 0.10,
    ComfortStrategy.EFFICIENT: 0.30,
    ComfortStrategy.BALANCED: 0.50,
    ComfortStrategy.COMFORT: 0.70,
    ComfortStrategy.NEAR_NEUTRAL: 0.90,
}
_ROOT_ORDER = (
    RootName.LOWER_COMFORT,
    RootName.HEATING_CONTROL,
    RootName.THERMAL_NEUTRAL,
    RootName.COOLING_CONTROL,
    RootName.UPPER_COMFORT,
)


class RadiantModel(Protocol):
    """Candidate interface implemented by every physical radiant model."""

    def current_mrt(self, air_temperature_c: float) -> RadiantResult: ...

    def candidate_mrt(self, candidate_air_temperature_c: float) -> RadiantResult: ...


@dataclass(frozen=True, slots=True)
class StrategyVotes:
    """The five requested votes after applying one strategy fraction."""

    lower_comfort: float
    heating_control: float
    thermal_neutral: float
    cooling_control: float
    upper_comfort: float
    strategy: ComfortStrategy
    inward_fraction: float

    def ordered(self) -> tuple[tuple[RootName, float], ...]:
        return (
            (RootName.LOWER_COMFORT, self.lower_comfort),
            (RootName.HEATING_CONTROL, self.heating_control),
            (RootName.THERMAL_NEUTRAL, self.thermal_neutral),
            (RootName.COOLING_CONTROL, self.cooling_control),
            (RootName.UPPER_COMFORT, self.upper_comfort),
        )


@dataclass(frozen=True, slots=True)
class StrategyVoteFailure:
    """Invalid strategy/boundary configuration with no invented votes."""

    detail: str


type StrategyVoteResult = StrategyVotes | StrategyVoteFailure


@dataclass(slots=True)
class EvaluationBudget:
    """One shared deterministic ATHB-evaluation budget per zone snapshot."""

    limit: int = DEFAULT_EVALUATION_BUDGET
    used: int = 0

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass(frozen=True, slots=True)
class InverseLocationContext:
    """Frozen physical state for one primary or mapped critical location."""

    current_room_air_temperature_c: float
    current_local_air_temperature_c: float
    moisture: MoistureState
    radiant: RadiantModel
    relative_air_speed_m_s: float
    met: float
    running_mean_c: float
    clothing: Clothing
    local_delta_c: float = 0.0
    radiant_uses_room_coordinate: bool = False


@dataclass(frozen=True, slots=True)
class CandidatePoint:
    """One successful inverse candidate in room and local coordinates."""

    room_temperature_c: float
    local_temperature_c: float
    sensation_vote: float
    applicability_reasons: tuple[ApplicabilityReason, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    """A candidate failure mapped onto the inverse failure vocabulary."""

    failure: RootFailureCode
    detail: str


type CandidateResult = CandidatePoint | CandidateFailure
type CandidateEvaluator = Callable[[float, EvaluationBudget], CandidateResult]


@dataclass(frozen=True, slots=True)
class DirectionalEligibility:
    """Whether the available roots satisfy one actuator direction."""

    eligible: bool
    required_roots: tuple[RootName, ...]
    reasons: tuple[str, ...]


def strategy_votes(
    strategy: ComfortStrategy,
    *,
    lower_vote: float = -0.5,
    upper_vote: float = 0.5,
) -> StrategyVoteResult:
    """Apply the fixed strategy fraction directly in sensation space."""

    if isinstance(lower_vote, bool) or isinstance(upper_vote, bool):
        return StrategyVoteFailure("comfort boundaries must be numeric, finite, and not Boolean")
    try:
        lower = float(lower_vote)
        upper = float(upper_vote)
    except TypeError, ValueError, OverflowError:
        return StrategyVoteFailure("comfort boundaries must be numeric and finite")
    if not math.isfinite(lower) or not math.isfinite(upper):
        return StrategyVoteFailure("comfort boundaries must be finite")
    if not lower < 0.0 < upper:
        return StrategyVoteFailure("comfort boundaries must satisfy lower < 0 < upper")
    fraction = _STRATEGY_FRACTIONS.get(strategy)
    if fraction is None:
        return StrategyVoteFailure("unsupported comfort strategy")
    return StrategyVotes(
        lower,
        lower + fraction * (0.0 - lower),
        0.0,
        upper + fraction * (0.0 - upper),
        upper,
        strategy,
        fraction,
    )


def _map_forward_failure(failure: NumericalFailure) -> CandidateFailure:
    mapped = {
        NumericalFailureCode.NON_FINITE: RootFailureCode.NON_FINITE,
        NumericalFailureCode.HEAT_BALANCE_NON_CONVERGENCE: (
            RootFailureCode.HEAT_BALANCE_NON_CONVERGENCE
        ),
        NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN: (
            RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN
        ),
        NumericalFailureCode.EVALUATION_BUDGET_EXCEEDED: (
            RootFailureCode.EVALUATION_BUDGET_EXCEEDED
        ),
        NumericalFailureCode.BOOLEAN_INPUT: RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
        NumericalFailureCode.NON_NUMERIC: RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
    }
    return CandidateFailure(mapped[failure.code], failure.detail)


def _map_moisture_failure(failure: MoistureFailure) -> CandidateFailure:
    if failure.code is MoistureFailureCode.MOISTURE_LIMITED_NO_SOLUTION:
        return CandidateFailure(RootFailureCode.MOISTURE_LIMITED_NO_SOLUTION, failure.detail)
    if failure.code is MoistureFailureCode.NON_FINITE:
        return CandidateFailure(RootFailureCode.NON_FINITE, failure.detail)
    return CandidateFailure(RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN, failure.detail)


def _map_radiant_failure(failure: RadiantFailure) -> CandidateFailure:
    if failure.code in {
        RadiantFailureCode.NON_FINITE,
        RadiantFailureCode.NONPOSITIVE_RADICAND,
    }:
        return CandidateFailure(RootFailureCode.NON_FINITE, failure.detail)
    return CandidateFailure(RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN, failure.detail)


def _candidate_evaluator(context: InverseLocationContext) -> CandidateEvaluator:
    def evaluate(room_temperature_c: float, budget: EvaluationBudget) -> CandidateResult:
        if not budget.consume():
            return CandidateFailure(
                RootFailureCode.EVALUATION_BUDGET_EXCEEDED,
                "zone snapshot ATHB evaluation budget was exhausted",
            )
        local_temperature = room_temperature_c - context.local_delta_c
        humidity = candidate_relative_humidity(context.moisture, local_temperature)
        if isinstance(humidity, MoistureFailure):
            return _map_moisture_failure(humidity)
        radiant_coordinate = (
            room_temperature_c if context.radiant_uses_room_coordinate else local_temperature
        )
        radiant = context.radiant.candidate_mrt(radiant_coordinate)
        if isinstance(radiant, RadiantFailure):
            return _map_radiant_failure(radiant)
        result = evaluate_athb(
            AthbInputs(
                tdb_c=local_temperature,
                tr_c=radiant.mrt_c,
                relative_air_speed_m_s=context.relative_air_speed_m_s,
                rh_pct=humidity.relative_humidity_pct,
                met=context.met,
                running_mean_c=context.running_mean_c,
                clothing=context.clothing,
            )
        )
        if isinstance(result, NumericalFailure):
            return _map_forward_failure(result)
        return CandidatePoint(
            room_temperature_c,
            local_temperature,
            result.sensation_vote,
            result.applicability_reasons,
        )

    return evaluate


def evaluate_current_location(
    context: InverseLocationContext,
    *,
    budget: EvaluationBudget | None = None,
) -> AthbResult:
    """Evaluate current local conditions within an optional shared zone budget."""

    if budget is not None and not budget.consume():
        return NumericalFailure(
            NumericalFailureCode.EVALUATION_BUDGET_EXCEEDED,
            None,
            "zone snapshot ATHB evaluation budget was exhausted",
        )

    radiant_coordinate = (
        context.current_room_air_temperature_c
        if context.radiant_uses_room_coordinate
        else context.current_local_air_temperature_c
    )
    radiant = context.radiant.current_mrt(radiant_coordinate)
    if isinstance(radiant, RadiantFailure):
        return NumericalFailure(
            NumericalFailureCode.NON_FINITE
            if radiant.code
            in {RadiantFailureCode.NON_FINITE, RadiantFailureCode.NONPOSITIVE_RADICAND}
            else NumericalFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
            radiant.field,
            radiant.detail,
        )
    return evaluate_athb(
        AthbInputs(
            tdb_c=context.current_local_air_temperature_c,
            tr_c=radiant.mrt_c,
            relative_air_speed_m_s=context.relative_air_speed_m_s,
            rh_pct=context.moisture.starting_relative_humidity_pct,
            met=context.met,
            running_mean_c=context.running_mean_c,
            clothing=context.clothing,
        )
    )


def _root_failure(
    name: RootName,
    vote: float,
    failure: RootFailureCode,
    budget: EvaluationBudget,
    detail: str,
) -> RootFailure:
    return RootFailure(name, vote, failure, budget.used, detail)


def _solve_one_root(
    *,
    name: RootName,
    requested_vote: float,
    samples: tuple[CandidatePoint, ...],
    evaluate: CandidateEvaluator,
    budget: EvaluationBudget,
    moisture_restricted: bool,
) -> RootResult:
    if not math.isfinite(requested_vote):
        return _root_failure(
            name,
            requested_vote,
            RootFailureCode.NON_FINITE,
            budget,
            "requested sensation vote must be finite",
        )
    exact = [point for point in samples if point.sensation_vote == requested_vote]
    brackets = [
        (left, right)
        for left, right in pairwise(samples)
        if (left.sensation_vote - requested_vote) * (right.sensation_vote - requested_vote) < 0.0
    ]
    if exact:
        if len(exact) > 1 or brackets:
            return _root_failure(
                name,
                requested_vote,
                RootFailureCode.MULTIPLE_BRACKETS,
                budget,
                "requested vote had multiple exact or sign-changing brackets",
            )
        selected = exact[0]
        final = evaluate(selected.room_temperature_c, budget)
        if isinstance(final, CandidateFailure):
            return _root_failure(name, requested_vote, final.failure, budget, final.detail)
        residual = final.sensation_vote - requested_vote
        if abs(residual) > ROOT_RESIDUAL_TOLERANCE:
            return _root_failure(
                name,
                requested_vote,
                RootFailureCode.ITERATION_LIMIT,
                budget,
                "exact sampled candidate failed the final residual check",
            )
        return RootSuccess(
            name,
            requested_vote,
            final.room_temperature_c,
            final.local_temperature_c,
            residual,
            0.0,
            budget.used,
            final.applicability_reasons,
        )
    if len(brackets) > 1:
        return _root_failure(
            name,
            requested_vote,
            RootFailureCode.MULTIPLE_BRACKETS,
            budget,
            "requested vote had multiple sign-changing brackets",
        )
    if not brackets:
        first_vote = samples[0].sensation_vote
        last_vote = samples[-1].sensation_vote
        if requested_vote < first_vote:
            code = (
                RootFailureCode.MOISTURE_LIMITED_NO_SOLUTION
                if moisture_restricted
                else RootFailureCode.BELOW_SEARCH_DOMAIN
            )
            return _root_failure(
                name,
                requested_vote,
                code,
                budget,
                "requested vote lies below the usable search interval",
            )
        if requested_vote > last_vote:
            return _root_failure(
                name,
                requested_vote,
                RootFailureCode.ABOVE_SEARCH_DOMAIN,
                budget,
                "requested vote lies above the usable search interval",
            )
        return _root_failure(
            name,
            requested_vote,
            RootFailureCode.NO_BRACKET,
            budget,
            "no unique adjacent sign-changing bracket was found",
        )

    left, right = brackets[0]
    iterations = 0
    while right.room_temperature_c - left.room_temperature_c > ROOT_WIDTH_TOLERANCE_C:
        if iterations >= MAX_ROOT_ITERATIONS:
            return _root_failure(
                name,
                requested_vote,
                RootFailureCode.ITERATION_LIMIT,
                budget,
                "root bracket did not converge within 48 iterations",
            )
        midpoint_temperature = (left.room_temperature_c + right.room_temperature_c) / 2.0
        midpoint = evaluate(midpoint_temperature, budget)
        if isinstance(midpoint, CandidateFailure):
            return _root_failure(name, requested_vote, midpoint.failure, budget, midpoint.detail)
        midpoint_residual = midpoint.sensation_vote - requested_vote
        if midpoint_residual < 0.0:
            left = midpoint
        elif midpoint_residual > 0.0:
            right = midpoint
        else:
            left = midpoint
            right = midpoint
        iterations += 1

    selected_temperature = (left.room_temperature_c + right.room_temperature_c) / 2.0
    final = evaluate(selected_temperature, budget)
    if isinstance(final, CandidateFailure):
        return _root_failure(name, requested_vote, final.failure, budget, final.detail)
    width = right.room_temperature_c - left.room_temperature_c
    residual = final.sensation_vote - requested_vote
    if width > ROOT_WIDTH_TOLERANCE_C or abs(residual) > ROOT_RESIDUAL_TOLERANCE:
        return _root_failure(
            name,
            requested_vote,
            RootFailureCode.ITERATION_LIMIT,
            budget,
            "root failed the final width or residual requirement",
        )
    return RootSuccess(
        name,
        requested_vote,
        final.room_temperature_c,
        final.local_temperature_c,
        residual,
        width,
        budget.used,
        final.applicability_reasons,
    )


def solve_requested_roots(
    *,
    votes: StrategyVotes,
    evaluate: CandidateEvaluator,
    search_low_c: float,
    search_high_c: float,
    budget: EvaluationBudget,
    moisture_restricted: bool = False,
) -> RootSet:
    """Solve five votes using one shared 33-point scan and stable root order."""

    if not math.isfinite(search_low_c) or not math.isfinite(search_high_c):
        interval_failure = CandidateFailure(
            RootFailureCode.NON_FINITE, "search interval must be finite"
        )
        interval_results: dict[RootName, RootResult] = {
            name: _root_failure(
                name, vote, interval_failure.failure, budget, interval_failure.detail
            )
            for name, vote in votes.ordered()
        }
        return _root_set(interval_results)
    if search_low_c >= search_high_c:
        empty_results: dict[RootName, RootResult] = {
            name: _root_failure(
                name,
                vote,
                RootFailureCode.OUTSIDE_ENGINEERING_DOMAIN,
                budget,
                "search interval is empty after physical-domain intersection",
            )
            for name, vote in votes.ordered()
        }
        return _root_set(empty_results)

    step = (search_high_c - search_low_c) / (SAMPLE_COUNT - 1)
    sampled: list[CandidatePoint] = []
    sample_failure: CandidateFailure | None = None
    for index in range(SAMPLE_COUNT):
        point = evaluate(search_low_c + index * step, budget)
        if isinstance(point, CandidateFailure):
            sample_failure = point
            break
        sampled.append(point)
    if sample_failure is not None:
        failed_sample_results: dict[RootName, RootResult] = {
            name: _root_failure(name, vote, sample_failure.failure, budget, sample_failure.detail)
            for name, vote in votes.ordered()
        }
        return _root_set(failed_sample_results)
    samples = tuple(sampled)
    if any(
        not math.isfinite(point.room_temperature_c)
        or not math.isfinite(point.local_temperature_c)
        or not math.isfinite(point.sensation_vote)
        for point in samples
    ):
        nonfinite_results: dict[RootName, RootResult] = {
            name: _root_failure(
                name,
                vote,
                RootFailureCode.NON_FINITE,
                budget,
                "candidate evaluation returned a non-finite value",
            )
            for name, vote in votes.ordered()
        }
        return _root_set(nonfinite_results)
    if any(
        right.sensation_vote < left.sensation_vote - MONOTONIC_DECREASE_TOLERANCE
        for left, right in pairwise(samples)
    ):
        monotonic_results: dict[RootName, RootResult] = {
            name: _root_failure(
                name,
                vote,
                RootFailureCode.NON_MONOTONIC,
                budget,
                "sampled sensation decreased by more than 1e-4",
            )
            for name, vote in votes.ordered()
        }
        return _root_set(monotonic_results)

    results: dict[RootName, RootResult] = {}
    for name, vote in votes.ordered():
        if budget.used >= budget.limit:
            results[name] = _root_failure(
                name,
                vote,
                RootFailureCode.EVALUATION_BUDGET_EXCEEDED,
                budget,
                "zone snapshot ATHB evaluation budget was exhausted",
            )
            continue
        results[name] = _solve_one_root(
            name=name,
            requested_vote=vote,
            samples=samples,
            evaluate=evaluate,
            budget=budget,
            moisture_restricted=moisture_restricted,
        )
    return _root_set(results)


def _root_set(results: Mapping[RootName, RootResult]) -> RootSet:
    return RootSet(
        results[RootName.LOWER_COMFORT],
        results[RootName.HEATING_CONTROL],
        results[RootName.THERMAL_NEUTRAL],
        results[RootName.COOLING_CONTROL],
        results[RootName.UPPER_COMFORT],
    )


def solve_five_roots(
    context: InverseLocationContext,
    votes: StrategyVotes,
    *,
    budget: EvaluationBudget | None = None,
) -> RootSet:
    """Intersect physical domains and solve all five semantic roots."""

    shared_budget = budget if budget is not None else EvaluationBudget()
    low = max(SEARCH_MIN_C, SEARCH_MIN_C + context.local_delta_c)
    high = min(SEARCH_MAX_C, SEARCH_MAX_C + context.local_delta_c)
    unrestricted_low = low
    dew_point = dew_or_frost_point(context.moisture)
    if isinstance(dew_point, MoistureFailure):
        moisture_results: dict[RootName, RootResult] = {
            name: _root_failure(
                name,
                vote,
                _map_moisture_failure(dew_point).failure,
                shared_budget,
                dew_point.detail,
            )
            for name, vote in votes.ordered()
        }
        return _root_set(moisture_results)
    if dew_point.status is DewPointStatus.SOLVED and dew_point.temperature_c is not None:
        low = max(
            low,
            dew_point.temperature_c + PSYCHROMETRIC_SEARCH_MARGIN_C + context.local_delta_c,
        )
    return solve_requested_roots(
        votes=votes,
        evaluate=_candidate_evaluator(context),
        search_low_c=low,
        search_high_c=high,
        budget=shared_budget,
        moisture_restricted=low > unrestricted_low + 1e-12,
    )


def directional_root_eligibility(
    *,
    current: AthbResult,
    roots: RootSet,
    direction: ActuationDirection,
    minimum_range_gap_c: float = 0.0,
) -> DirectionalEligibility:
    """Apply the clarification's directional root requirements exactly."""

    required = {
        ActuationDirection.HEATING_ONLY: (RootName.HEATING_CONTROL,),
        ActuationDirection.COOLING_ONLY: (RootName.COOLING_CONTROL,),
        ActuationDirection.RANGED: (RootName.HEATING_CONTROL, RootName.COOLING_CONTROL),
    }[direction]
    reasons: list[str] = []
    if not isinstance(current, AthbSuccess):
        reasons.append("current_athb_unavailable")
    root_by_name = {
        RootName.LOWER_COMFORT: roots.lower_comfort,
        RootName.HEATING_CONTROL: roots.heating_control,
        RootName.THERMAL_NEUTRAL: roots.thermal_neutral,
        RootName.COOLING_CONTROL: roots.cooling_control,
        RootName.UPPER_COMFORT: roots.upper_comfort,
    }
    for name in required:
        result = root_by_name[name]
        if not isinstance(result, RootSuccess):
            reasons.append(f"missing_required_root:{name.value}")
    if direction is ActuationDirection.RANGED and not reasons:
        heating = root_by_name[RootName.HEATING_CONTROL]
        cooling = root_by_name[RootName.COOLING_CONTROL]
        if (
            isinstance(heating, RootSuccess)
            and isinstance(cooling, RootSuccess)
            and cooling.mapped_room_temperature_c - heating.mapped_room_temperature_c
            < max(0.0, minimum_range_gap_c)
        ):
            reasons.append("invalid_control_band_order_or_gap")
    return DirectionalEligibility(not reasons, required, tuple(reasons))


def root_results_in_order(roots: RootSet) -> tuple[RootResult, ...]:
    """Return the stable semantic root sequence for diagnostics and tests."""

    by_name = {
        RootName.LOWER_COMFORT: roots.lower_comfort,
        RootName.HEATING_CONTROL: roots.heating_control,
        RootName.THERMAL_NEUTRAL: roots.thermal_neutral,
        RootName.COOLING_CONTROL: roots.cooling_control,
        RootName.UPPER_COMFORT: roots.upper_comfort,
    }
    return tuple(by_name[name] for name in _ROOT_ORDER)

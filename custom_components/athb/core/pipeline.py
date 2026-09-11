"""End-to-end pure zone calculation through normalization."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .athb_engine import relative_air_speed
from .climate import (
    ClimateCapabilitySnapshot,
    ClimateFailure,
    GridOptions,
    NormalizedRangeTarget,
    NormalizedScalarTarget,
    normalize_range_target,
    normalize_scalar_target,
)
from .contracts import (
    AUTOMATIC_CLOTHING,
    ActuationDirection,
    ApplicabilityReason,
    AthbResult,
    AthbSuccess,
    Clothing,
    ComfortStrategy,
    ControlProfile,
    CriticalEligibilityMode,
    DeclaredRelativeHumidity,
    EcoIntensity,
    MeasuredRelativeHumidity,
    NumericalFailure,
    RootName,
    RootSet,
    RootSuccess,
)
from .inverse import (
    DirectionalEligibility,
    EvaluationBudget,
    InverseLocationContext,
    RadiantModel,
    StrategyVoteFailure,
    StrategyVotes,
    directional_root_eligibility,
    evaluate_current_location,
    solve_five_roots,
    strategy_votes,
)
from .locations import CriticalLocationSolution, PreparedCriticalLocation, solve_critical_location
from .policy import (
    CriticalDemand,
    PolicyFailure,
    PolicyTargets,
    build_adaptive_policy,
    build_fixed_fallback,
    cooling_dewpoint_eligible,
)
from .psychrometrics import MoistureFailure, dew_or_frost_point, moisture_state
from .radiant import UniformRadiantModel


@dataclass(frozen=True, slots=True)
class ZoneCalculationInput:
    air_temperature_c: float
    relative_humidity_pct: float
    relative_humidity_declared: bool
    running_mean_c: float | None
    strategy: ComfortStrategy
    direction: ActuationDirection
    profile: ControlProfile
    climate: ClimateCapabilitySnapshot
    grid: GridOptions
    met: float = 1.1
    air_speed_m_s: float | None = 0.1
    clothing: Clothing = AUTOMATIC_CLOTHING
    radiant: RadiantModel = field(default_factory=UniformRadiantModel)
    critical_locations: tuple[PreparedCriticalLocation, ...] = ()
    lower_comfort_vote: float = -0.5
    upper_comfort_vote: float = 0.5
    fallback_heating_c: float = 18.0
    fallback_cooling_c: float = 26.0
    fallback_no_write: bool = False
    reject_extrapolation: bool = False
    eco_heating_setback_c: float = 2.0
    eco_cooling_setback_c: float = 2.0
    eco_intensity: EcoIntensity = EcoIntensity.CUSTOM
    inactive_heating_c: float = 18.0
    inactive_cooling_c: float = 26.0
    boost_delta_c: float = 1.0
    previous_requested: tuple[float | None, float | None] = (None, None)
    elapsed_since_previous_seconds: float = 0.0
    explicit_transition: bool = False
    failure_hold_elapsed: bool = False
    fixed_fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ZoneCalculationResult:
    current: AthbResult | None
    roots: RootSet | None
    policy: PolicyTargets | None
    normalized: NormalizedScalarTarget | NormalizedRangeTarget | None
    suppression_reason: str | None
    evaluation_count: int
    votes: StrategyVotes | None = None
    eligibility: DirectionalEligibility | None = None
    critical_locations: tuple[CriticalLocationSolution, ...] = ()
    hold_condition: str | None = None


def _fixed_fallback(
    inputs: ZoneCalculationInput,
    *,
    current: AthbResult | None = None,
    roots: RootSet | None = None,
    evaluation_count: int = 0,
    votes: StrategyVotes | None = None,
    eligibility: DirectionalEligibility | None = None,
    hold_condition: str | None = None,
) -> ZoneCalculationResult:
    fallback = build_fixed_fallback(
        direction=inputs.direction,
        primary_air_valid=True,
        primary_rh_valid=True,
        fallback_policy_no_write=inputs.fallback_no_write,
        heating_c=inputs.fallback_heating_c,
        cooling_c=inputs.fallback_cooling_c,
        minimum_range_gap_c=inputs.grid.minimum_range_gap_c,
    )
    result = _normalize_policy(
        inputs, current, roots, fallback, evaluation_count, votes, eligibility, ()
    )
    return replace(result, hold_condition=hold_condition)


def _roots_are_extrapolated(roots: RootSet, names: tuple[RootName, ...]) -> bool:
    return any(
        isinstance(root := getattr(roots, name.value), RootSuccess)
        and ApplicabilityReason.EXTRAPOLATED in root.applicability_reasons
        for name in names
    )


def _critical_required_roots(
    mode: CriticalEligibilityMode, direction: ActuationDirection
) -> tuple[RootName, ...]:
    names: list[RootName] = []
    if direction in {ActuationDirection.HEATING_ONLY, ActuationDirection.RANGED} and mode in {
        CriticalEligibilityMode.HEATING,
        CriticalEligibilityMode.BOTH,
    }:
        names.append(RootName.HEATING_CONTROL)
    if direction in {ActuationDirection.COOLING_ONLY, ActuationDirection.RANGED} and mode in {
        CriticalEligibilityMode.COOLING,
        CriticalEligibilityMode.BOTH,
    }:
        names.append(RootName.COOLING_CONTROL)
    return tuple(names)


def calculate_zone(inputs: ZoneCalculationInput) -> ZoneCalculationResult:
    """Run observations/history through ATHB, roots, policy, bounds, and grid."""

    rh_source = (
        DeclaredRelativeHumidity(inputs.relative_humidity_pct)
        if inputs.relative_humidity_declared
        else MeasuredRelativeHumidity(inputs.relative_humidity_pct)
    )
    moisture = moisture_state(inputs.air_temperature_c, rh_source)
    if isinstance(moisture, MoistureFailure):
        return ZoneCalculationResult(None, None, None, None, moisture.code.value, 0)
    if inputs.running_mean_c is None:
        return _fixed_fallback(inputs, hold_condition="running_mean_unavailable")
    if inputs.fixed_fallback_reason is not None:
        return _fixed_fallback(inputs, hold_condition=inputs.fixed_fallback_reason)
    if inputs.air_speed_m_s is None:
        return ZoneCalculationResult(None, None, None, None, "air_speed_invalid", 0)
    speed = relative_air_speed(inputs.air_speed_m_s, inputs.met)
    if isinstance(speed, NumericalFailure):
        return ZoneCalculationResult(speed, None, None, None, speed.code.value, 0)
    votes = strategy_votes(
        inputs.strategy,
        lower_vote=inputs.lower_comfort_vote,
        upper_vote=inputs.upper_comfort_vote,
    )
    if isinstance(votes, StrategyVoteFailure):
        return ZoneCalculationResult(None, None, None, None, "invalid_strategy", 0)
    context = InverseLocationContext(
        inputs.air_temperature_c,
        inputs.air_temperature_c,
        moisture,
        inputs.radiant,
        speed,
        inputs.met,
        inputs.running_mean_c,
        inputs.clothing,
    )
    budget = EvaluationBudget()
    current = evaluate_current_location(context, budget=budget)
    roots = solve_five_roots(context, votes, budget=budget)
    eligibility = directional_root_eligibility(
        current=current,
        roots=roots,
        direction=inputs.direction,
        minimum_range_gap_c=inputs.grid.minimum_range_gap_c,
    )
    if not eligibility.eligible:
        reason = eligibility.reasons[0]
        if inputs.failure_hold_elapsed:
            return _fixed_fallback(
                inputs,
                current=current,
                roots=roots,
                evaluation_count=budget.used,
                votes=votes,
                eligibility=eligibility,
                hold_condition=reason,
            )
        return ZoneCalculationResult(
            current,
            roots,
            None,
            None,
            reason,
            budget.used,
            votes,
            eligibility,
            hold_condition=reason,
        )
    assert isinstance(current, AthbSuccess)
    critical_solutions = tuple(
        solve_critical_location(location, votes, budget=budget)
        for location in inputs.critical_locations
    )
    extrapolated_decision = (
        ApplicabilityReason.EXTRAPOLATED in current.applicability_reasons
        or _roots_are_extrapolated(roots, eligibility.required_roots)
        or any(
            location.control_eligible
            and _roots_are_extrapolated(
                location.roots,
                _critical_required_roots(location.mode, inputs.direction),
            )
            for location in critical_solutions
        )
    )
    if inputs.reject_extrapolation and extrapolated_decision:
        if inputs.failure_hold_elapsed:
            return _fixed_fallback(
                inputs,
                current=current,
                roots=roots,
                evaluation_count=budget.used,
                votes=votes,
                eligibility=eligibility,
                hold_condition="extrapolation_rejected",
            )
        return ZoneCalculationResult(
            current,
            roots,
            None,
            None,
            "extrapolation_rejected",
            budget.used,
            votes,
            eligibility,
            hold_condition="extrapolation_rejected",
        )
    critical_demands = tuple(
        CriticalDemand(
            location.location_id,
            location.mode,
            location.roots.heating_control,
            location.roots.cooling_control,
            location.control_eligible,
        )
        for location in critical_solutions
    )
    policy = build_adaptive_policy(
        roots=roots,
        critical_demands=critical_demands,
        direction=inputs.direction,
        profile=inputs.profile,
        eco_intensity=inputs.eco_intensity,
        minimum_range_gap_c=inputs.grid.minimum_range_gap_c,
        eco_heating_setback_c=inputs.eco_heating_setback_c,
        eco_cooling_setback_c=inputs.eco_cooling_setback_c,
        inactive_heating_c=inputs.inactive_heating_c,
        inactive_cooling_c=inputs.inactive_cooling_c,
        boost_delta_c=inputs.boost_delta_c,
        previous_requested=inputs.previous_requested,
        elapsed_since_previous_seconds=inputs.elapsed_since_previous_seconds,
        explicit_transition=inputs.explicit_transition,
    )
    return _normalize_policy(
        inputs,
        current,
        roots,
        policy,
        budget.used,
        votes,
        eligibility,
        critical_solutions,
    )


def _normalize_policy(
    inputs: ZoneCalculationInput,
    current: AthbResult | None,
    roots: RootSet | None,
    policy: PolicyTargets | PolicyFailure,
    evaluation_count: int,
    votes: StrategyVotes | None,
    eligibility: DirectionalEligibility | None,
    critical_locations: tuple[CriticalLocationSolution, ...],
) -> ZoneCalculationResult:
    if isinstance(policy, PolicyFailure):
        return ZoneCalculationResult(
            current,
            roots,
            None,
            None,
            policy.reason,
            evaluation_count,
            votes,
            eligibility,
            critical_locations,
        )
    normalized: NormalizedScalarTarget | NormalizedRangeTarget | ClimateFailure
    if inputs.direction is ActuationDirection.HEATING_ONLY:
        if policy.heating_c is None:
            return ZoneCalculationResult(
                current,
                roots,
                policy,
                None,
                "missing_heating_target",
                evaluation_count,
                votes,
                eligibility,
                critical_locations,
            )
        normalized = normalize_scalar_target(
            requested_room_c=policy.heating_c,
            direction=inputs.direction,
            snapshot=inputs.climate,
            options=inputs.grid,
        )
    elif inputs.direction is ActuationDirection.COOLING_ONLY:
        if policy.cooling_c is None:
            return ZoneCalculationResult(
                current,
                roots,
                policy,
                None,
                "missing_cooling_target",
                evaluation_count,
                votes,
                eligibility,
                critical_locations,
            )
        normalized = normalize_scalar_target(
            requested_room_c=policy.cooling_c,
            direction=inputs.direction,
            snapshot=inputs.climate,
            options=inputs.grid,
        )
    else:
        if policy.heating_c is None or policy.cooling_c is None:
            return ZoneCalculationResult(
                current,
                roots,
                policy,
                None,
                "missing_range_target",
                evaluation_count,
                votes,
                eligibility,
                critical_locations,
            )
        normalized = normalize_range_target(
            requested_heating_room_c=policy.heating_c,
            requested_cooling_room_c=policy.cooling_c,
            snapshot=inputs.climate,
            options=inputs.grid,
        )
    if isinstance(normalized, ClimateFailure):
        return ZoneCalculationResult(
            current,
            roots,
            policy,
            None,
            normalized.reason,
            evaluation_count,
            votes,
            eligibility,
            critical_locations,
        )
    if policy.fallback and inputs.direction in {
        ActuationDirection.COOLING_ONLY,
        ActuationDirection.RANGED,
    }:
        fallback_moisture = moisture_state(
            inputs.air_temperature_c,
            DeclaredRelativeHumidity(inputs.relative_humidity_pct)
            if inputs.relative_humidity_declared
            else MeasuredRelativeHumidity(inputs.relative_humidity_pct),
        )
        dewpoint = (
            fallback_moisture
            if isinstance(fallback_moisture, MoistureFailure)
            else dew_or_frost_point(fallback_moisture)
        )
        if not isinstance(dewpoint, MoistureFailure) and dewpoint.temperature_c is not None:
            cooling_room_c = (
                normalized.normalized_room_c
                if isinstance(normalized, NormalizedScalarTarget)
                else normalized.cooling.normalized_room_c
            )
            dewpoint_check = cooling_dewpoint_eligible(
                normalized_room_c=cooling_room_c,
                dewpoint_constraint_c=dewpoint.temperature_c,
            )
            if not dewpoint_check.eligible:
                return ZoneCalculationResult(
                    current,
                    roots,
                    policy,
                    None,
                    dewpoint_check.reason,
                    evaluation_count,
                    votes,
                    eligibility,
                    critical_locations,
                )
    return ZoneCalculationResult(
        current,
        roots,
        policy,
        normalized,
        None,
        evaluation_count,
        votes,
        eligibility,
        critical_locations,
    )
